"""Тесты недельной рассылки прогульщикам (часть B).

Проверяется порог (3 прогула), окно (7 дней), расписание запуска
(воскресенье 19:00) и однократность за день через метку в ``meta``.
"""

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from bot import db
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import lesson_reminder_service as lrs
from tests.test_handlers_dispatch import FakeBot

USER_1 = 8901       # 3 прогула — беспокоим
USER_2 = 8902       # 2 прогула — не беспокоим
USER_3 = 8903       # 4 отсутствия + опоздание = 5 — беспокоим
USER_4 = 8904       # всё «present» — не беспокоим

GROUP = "25КАД"

# Воскресенье и понедельник (для проверки расписания запуска).
SUNDAY = date(2026, 10, 4)
MONDAY = date(2026, 10, 5)


class TruantBot(FakeBot):
    """FakeBot, умеющий падать с TelegramForbiddenError для части чатов."""

    def __init__(self, forbidden: set | None = None) -> None:
        super().__init__()
        self.forbidden = forbidden or set()

    async def __call__(self, method, request_timeout=None):
        from aiogram.exceptions import TelegramForbiddenError

        name = type(method).__name__
        if name == "SendMessage":
            chat_id = getattr(method, "chat_id", None)
            if chat_id in self.forbidden:
                raise TelegramForbiddenError(method=method, message="blocked")
        return await super().__call__(method, request_timeout)


def _add_mark(c, tg_id: int, date_iso: str, status: str,
              para: int = 1) -> None:
    """Вставить отметку посещаемости."""
    c.execute(
        "INSERT INTO attendance (group_name, date_iso, para, tg_id, full_name,"
        " status, marked_by, marked_at, method)"
        " VALUES (?, ?, ?, ?, 'Тест Студент', ?, 1,"
        " '2026-10-01T10:00:00+07:00', 'starosta')",
        (GROUP, date_iso, para, tg_id, status),
    )


def _add_user(c, tg_id: int) -> None:
    """Зарегистрировать пользователя (для проверки is_active)."""
    c.execute(
        "INSERT INTO users (tg_id, group_name, full_name, is_active, created_at)"
        " VALUES (?, ?, 'Тест Студент', 1, '2026-09-01T00:00:00+07:00')",
        (tg_id, GROUP),
    )


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с отметками за неделю перед воскресеньем 04.10.2026."""
    c = get_connection(tmp_path / "truant.db")
    apply_migrations(c)
    with transaction(c):
        for tg_id in (USER_1, USER_2, USER_3, USER_4):
            _add_user(c, tg_id)

        # Окно: 27.09–04.10 (today − 7 … today).
        for day in ("2026-09-29", "2026-10-01", "2026-10-02"):
            _add_mark(c, USER_1, day, "absent", para=1)      # ровно 3
        for day in ("2026-09-29", "2026-10-01"):
            _add_mark(c, USER_2, day, "absent", para=1)      # 2 — мало
        for day in ("2026-09-28", "2026-09-30", "2026-10-01", "2026-10-02"):
            _add_mark(c, USER_3, day, "absent", para=1)      # 4 + late = 5
        _add_mark(c, USER_3, "2026-10-03", "late", para=2)
        for day in ("2026-09-29", "2026-10-01", "2026-10-02"):
            _add_mark(c, USER_4, day, "present", para=1)     # прогулов нет
    yield c
    c.close()


def _texts(bot: FakeBot) -> list[str]:
    """Тексты отправленных сообщений."""
    return [m["text"] for m in bot.sent if m["text"]]


def _recipients(bot: FakeBot) -> set[int]:
    """Кому отправляли сообщения."""
    return {m["chat_id"] for m in bot.sent if m.get("chat_id") is not None}
# --- порог и окно ---

async def test_three_absences_gets_warning(conn) -> None:
    """Студент с 3 прогулами получает предупреждение."""
    bot = TruantBot()

    sent = await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert sent == 2, "USER_1 (3 прогула) и USER_3 (5)"
    assert USER_1 in _recipients(bot)


async def test_two_absences_not_warned(conn) -> None:
    """С 2 прогулами не беспокоим."""
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert USER_2 not in _recipients(bot)


async def test_present_marks_not_counted(conn) -> None:
    """Посещения прогулом не считаются."""
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert USER_4 not in _recipients(bot)


async def test_late_counts_as_truant(conn) -> None:
    """Опоздание входит в счёт прогулов (у USER_3 их 5)."""
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    text = next(m["text"] for m in bot.sent if m.get("chat_id") == USER_3)
    assert "5 прогулов" in text


async def test_old_marks_outside_window_ignored(conn) -> None:
    """Отметки старше 7 дней в счёт не идут."""
    with transaction(conn):
        _add_mark(conn, USER_2, "2026-09-01", "absent", para=1)
        _add_mark(conn, USER_2, "2026-09-02", "absent", para=2)
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert USER_2 not in _recipients(bot), "старые прогулы вне окна"


async def test_boundary_day_included(conn) -> None:
    """Отметка ровно на границе окна (today − 7) учитывается."""
    with transaction(conn):
        conn.execute("DELETE FROM attendance WHERE tg_id = ?", (USER_4,))
        _add_mark(conn, USER_4, "2026-09-27", "absent", para=1)
        _add_mark(conn, USER_4, "2026-09-29", "absent", para=1)
        _add_mark(conn, USER_4, "2026-10-01", "absent", para=1)
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert USER_4 in _recipients(bot)


async def test_excused_not_counted(conn) -> None:
    """Уважительная причина прогулом не считается."""
    with transaction(conn):
        conn.execute("DELETE FROM attendance WHERE tg_id = ?", (USER_4,))
        for day in ("2026-09-29", "2026-10-01", "2026-10-02"):
            _add_mark(conn, USER_4, day, "excused", para=1)
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert USER_4 not in _recipients(bot)


# --- текст ---

async def test_warning_text(conn) -> None:
    """В тексте число прогулов и ссылка на /my_attendance."""
    bot = TruantBot()

    await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    text = next(m["text"] for m in bot.sent if m.get("chat_id") == USER_1)
    assert "3 прогула за неделю" in text
    assert "/my_attendance" in text
    assert text.startswith("⚠️")


@pytest.mark.parametrize(("count", "word"), [
    (3, "прогула"), (4, "прогула"), (5, "прогулов"),
    (11, "прогулов"), (21, "прогул"),
])
def test_plural_forms(count: int, word: str) -> None:
    """Число согласуется со словом (иначе «3 прогулов»)."""
    assert f"{count} {word} за неделю" in lrs.truant_text(count)


async def test_forbidden_deactivates_user(conn) -> None:
    """TelegramForbiddenError → is_active = 0, остальные получили."""
    bot = TruantBot(forbidden={USER_1})

    sent = await lrs.send_truant_reminders(conn, bot, today=SUNDAY)

    assert sent == 1, "дошло до USER_3"
    row = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_1,)
    ).fetchone()
    assert row["is_active"] == 0
# --- цикл: когда и сколько раз ---

async def _no_sleep(seconds: float) -> None:
    """Пауза-заглушка: цикл крутится без ожидания."""
    return None


async def test_loop_sends_on_sunday_19(conn, monkeypatch) -> None:
    """Воскресенье 19:00 — рассылка уходит."""
    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", stopping_sleep)
    bot = TruantBot()

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(
            conn, bot, now_provider=lambda: datetime(2026, 10, 4, 19, 3),
        )

    assert len(_recipients(bot)) == 2
    assert db.get_meta(conn, lrs.META_LAST_WEEKLY_TRUANT) == "2026-10-04"


async def test_loop_does_nothing_on_monday(conn, monkeypatch) -> None:
    """Понедельник (и вообще не воскресенье) — рассылки нет."""
    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", stopping_sleep)
    bot = TruantBot()

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(
            conn, bot, now_provider=lambda: datetime(2026, 10, 5, 19, 3),
        )

    assert _texts(bot) == []
    assert db.get_meta(conn, lrs.META_LAST_WEEKLY_TRUANT) is None


async def test_loop_does_nothing_sunday_morning(conn, monkeypatch) -> None:
    """Воскресенье, но не 19:00 — ждём нужного часа."""
    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", stopping_sleep)
    bot = TruantBot()

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(
            conn, bot, now_provider=lambda: datetime(2026, 10, 4, 12, 0),
        )

    assert _texts(bot) == []


async def test_loop_second_run_same_day_sends_nothing(conn, monkeypatch) -> None:
    """Повторный запуск в тот же день — 0 отправок (метка в meta)."""
    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", stopping_sleep)
    moment = lambda: datetime(2026, 10, 4, 19, 10)   # noqa: E731

    bot = TruantBot()
    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(conn, bot, now_provider=moment)
    first = len(_recipients(bot))
    bot.sent.clear()

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(conn, bot, now_provider=moment)

    assert first == 2
    assert _texts(bot) == [], "в тот же день повторно не шлём"


async def test_loop_runs_once_inside_hour(conn, monkeypatch) -> None:
    """Внутри часа несколько проходов — одна рассылка (метка ставится)."""
    calls: list[int] = []

    async def counting_sleep(seconds: float) -> None:
        calls.append(1)
        if len(calls) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", counting_sleep)
    bot = TruantBot()

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(
            conn, bot, now_provider=lambda: datetime(2026, 10, 4, 19, 5),
        )

    assert len(_recipients(bot)) == 2, "три прохода, одна рассылка"
    assert len(calls) == 3


async def test_loop_survives_errors(conn, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: следующий проход выполняется."""
    calls: list[int] = []

    class FakeDb:
        """Заглушка db: первый проход падает, второй отменяется."""

        @staticmethod
        def get_meta(connection, key):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("БД занята")
            raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "db", FakeDb)
    monkeypatch.setattr(lrs, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(
            conn, TruantBot(),
            now_provider=lambda: datetime(2026, 10, 4, 19, 0),
        )

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_loop_uses_interval(conn, monkeypatch) -> None:
    """Пауза между проходами — 15 минут."""
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", recording_sleep)

    with pytest.raises(asyncio.CancelledError):
        await lrs.weekly_truant_loop(conn, TruantBot())

    assert slept == [lrs.WEEKLY_TICK_SECONDS] == [15 * 60]


def test_weekly_config_values() -> None:
    """Час и день рассылки — воскресенье 19:00."""
    assert lrs.WEEKLY_TRUANT_WEEKDAY == 6
    assert lrs.WEEKLY_TRUANT_HOUR == 19
    assert lrs.TRUANT_THRESHOLD == 3
    assert lrs.TRUANT_WINDOW_DAYS == 7


def test_existing_notify_loop_untouched() -> None:
    """Существующая рассылка замен не тронута (ТЗ: не трогать)."""
    from bot.services import notify_service

    assert hasattr(notify_service, "notify_substitutions_loop")