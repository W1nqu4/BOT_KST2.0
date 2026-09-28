"""Тесты закрепления расписания в чате (bot.services.pin_service).

Проверяются определение времени последней пары по звонкам (будни и суббота)
и открепление после уроков. Сеть и Telegram не используются: Bot подменяется
заглушкой, ``_sleep`` — чтобы цикл не ждал 5 минут.
"""

import asyncio
from datetime import date, datetime, time
from pathlib import Path

import pytest

from bot import db
from bot.db import get_connection
from bot.migrations import apply_migrations
from bot.services import cache_service
from bot.services import pin_service as ps
from tests.test_handlers_dispatch import FakeBot

GROUP = "26КАД"

# Понедельник: у 26КАД пары 2, 3, 4 (максимум 4 → 16:35).
MONDAY = date(2026, 9, 28)
# Воскресенье — пар нет.
SUNDAY = date(2026, 9, 27)
# Суббота — звонки короче, 3 пара кончается в 14:20.
SATURDAY = date(2026, 10, 3)

CHAT_ID = -1001234567890


class PinBot(FakeBot):
    """FakeBot, который помнит вызовы pin/unpin и умеет «ошибаться»."""

    def __init__(self, fail_pin: bool = False, fail_unpin: bool = False) -> None:
        super().__init__()
        self.pinned: list[dict] = []
        self.unpinned: list[dict] = []
        self.fail_pin = fail_pin
        self.fail_unpin = fail_unpin

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "PinChatMessage":
            if self.fail_pin:
                from aiogram.exceptions import TelegramBadRequest

                raise TelegramBadRequest(method=method,
                                         message="not enough rights")
            self.pinned.append({
                "chat_id": getattr(method, "chat_id", None),
                "message_id": getattr(method, "message_id", None),
                "disable_notification": getattr(
                    method, "disable_notification", None
                ),
            })
        if name == "UnpinChatMessage":
            if self.fail_unpin:
                raise RuntimeError("message is not pinned")
            self.unpinned.append({
                "chat_id": getattr(method, "chat_id", None),
                "message_id": getattr(method, "message_id", None),
            })
        return await super().__call__(method, request_timeout)


async def _no_sleep(seconds: float) -> None:
    """Заглушка паузы."""
    return None


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями."""
    c = get_connection(tmp_path / "pin.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def conn_with_schedule(conn, parsed_schedule):
    """БД с реальным расписанием (нужно, чтобы определить пары дня)."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


def _link_chat(conn, chat_id: int = CHAT_ID, group: str = GROUP) -> None:
    """Привязать чат к группе."""
    db.add_group_chat(conn, chat_id, "КСТ", "supergroup", group, 111)


def _pin(conn, chat_id: int = CHAT_ID, message_id: int = 777,
         day: date = MONDAY) -> None:
    """Отметить закрепление в БД."""
    db.set_pinned_message(conn, chat_id, message_id, day.isoformat())
# --- last_lesson_end_time ---

@pytest.mark.parametrize(("day", "paras", "expected"), [
    (MONDAY, [1, 2, 3], time(14, 50)),        # будни: 3 пара кончается 14:50
    (SATURDAY, [1, 2, 3], time(14, 20)),      # суббота: 3 пара кончается 14:20
    (MONDAY, [1, 2, 3, 4, 5], time(18, 5)),   # последняя — 5 пара
    (MONDAY, [5], time(18, 5)),
    (MONDAY, [1], time(10, 35)),
    (MONDAY, [], None),                        # пар нет
    (SATURDAY, [1, 2, 3, 5], None),            # 5 пары в субботу нет
    (SATURDAY, [5], None),
    (MONDAY, [9], None),                       # неизвестный номер пары
])
def test_last_lesson_end_time(day: date, paras: list[int],
                              expected: time | None) -> None:
    """Время окончания последней пары по звонкам нужного дня."""
    assert ps.last_lesson_end_time(day, paras) == expected


def test_last_lesson_end_time_uses_max_para() -> None:
    """Берётся именно максимальный номер, а не последний в списке."""
    assert ps.last_lesson_end_time(MONDAY, [5, 2]) == time(18, 5)


def test_last_lesson_end_time_weekday_vs_saturday() -> None:
    """Одна и та же пара в будни и в субботу кончается в разное время."""
    assert ps.last_lesson_end_time(MONDAY, [3]) == time(14, 50)
    assert ps.last_lesson_end_time(SATURDAY, [3]) == time(14, 20)


# --- unpin_due_chats: сценарии ---

async def test_stale_date_is_unpinned(conn_with_schedule) -> None:
    """Закрепление на прошлую дату снимается независимо от пар."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule, day=SUNDAY)
    bot = PinBot()

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 10, 0)
    )

    assert removed == 1
    assert bot.unpinned == [{"chat_id": CHAT_ID, "message_id": 777}]
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is None


async def test_future_date_is_kept(conn_with_schedule) -> None:
    """Закреплено расписание на ЗАВТРА → не трогаем (пары ещё не начались).

    Регресс-тест: вечерняя рассылка закрепляет расписание на следующий
    учебный день, поэтому «дата не сегодня» НЕ означает «устарело».
    Раньше такое закрепление снималось через 5 минут после отправки.
    """
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule, day=date(2026, 9, 29))   # завтра относительно 28.09
    bot = PinBot()

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 18, 0)
    )

    assert removed == 0, "закрепление на будущую дату снимать нельзя"
    assert bot.unpinned == []
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is not None


async def test_pin_for_tomorrow_survives_until_its_day_ends(
        conn_with_schedule) -> None:
    """Расписание на завтра живо весь завтрашний день и снимается после пар."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule, day=date(2026, 9, 29))
    bot = PinBot()

    # Утром своей даты — не трогаем.
    assert await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 29, 9, 0)
    ) == 0

    # После последней пары (3 пара кончается 14:50) — снимаем.
    assert await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 29, 15, 0)
    ) == 1


async def test_lessons_over_unpins(conn_with_schedule) -> None:
    """Пары кончились (+запас) → открепляем."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule)
    bot = PinBot()

    # Последняя пара понедельника кончается 16:35, запас 5 минут.
    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 18, 0)
    )

    assert removed == 1
    assert bot.unpinned[0]["message_id"] == 777
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is None


async def test_lessons_still_running_keeps_pin(conn_with_schedule) -> None:
    """Пары ещё идут → закрепление не трогаем."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule)
    bot = PinBot()

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 12, 0)
    )

    assert removed == 0
    assert bot.unpinned == []
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is not None


async def test_just_after_last_lesson_keeps_pin(conn_with_schedule) -> None:
    """Сразу после последней пары запас ещё не вышел → не открепляем."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule)
    bot = PinBot()

    # 16:35 + 5 = 16:40; в 16:38 ещё рано.
    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 16, 38)
    )

    assert removed == 0
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is not None


async def test_no_lessons_today_unpins_immediately(
        conn_with_schedule) -> None:
    """Пар на сегодня нет → открепляем сразу (например, воскресенье)."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule, day=SUNDAY)
    bot = PinBot()

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 27, 9, 0)
    )

    assert removed == 1
    assert bot.unpinned[0]["message_id"] == 777
async def test_chat_without_pin_is_untouched(conn_with_schedule) -> None:
    """Чат без закрепления не трогаем — ни pin, ни unpin."""
    _link_chat(conn_with_schedule)
    bot = PinBot()

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 20, 0)
    )

    assert removed == 0
    assert bot.unpinned == []
    assert bot.pinned == []


async def test_saturday_lessons_end_earlier(conn_with_schedule) -> None:
    """В субботу пары кончаются раньше — открепляем по субботним звонкам."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule, day=SATURDAY)
    bot = PinBot()

    # Суббота: пары кончаются 14:20; в 14:30 уже можно откреплять.
    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 10, 3, 14, 30)
    )

    assert removed == 1


async def test_unpin_failure_still_clears_db(conn_with_schedule) -> None:
    """Ошибка unpin: отметку всё равно снимаем, иначе цикл зациклится."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule)
    bot = PinBot(fail_unpin=True)

    removed = await ps.unpin_due_chats(
        conn_with_schedule, bot, datetime(2026, 9, 28, 18, 0)
    )

    assert removed == 0, "Telegram не подтвердил"
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is None, \
        "отметку всё равно снимаем"


# --- цикл ---

async def test_loop_unpins_and_stops(conn_with_schedule, monkeypatch) -> None:
    """Цикл делает проход и корректно отменяется."""
    _link_chat(conn_with_schedule)
    _pin(conn_with_schedule)
    bot = PinBot()

    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(ps, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ps.unpin_after_lessons_loop(
            conn_with_schedule, bot,
            now_provider=lambda: datetime(2026, 9, 28, 18, 0),
        )

    assert bot.unpinned, "цикл должен открепить перед выходом"


async def test_loop_survives_errors(conn_with_schedule, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: следующий проход выполняется."""
    calls: list[int] = []

    class FakeDb:
        """Заглушка db: первый проход падает, второй отменяется."""

        @staticmethod
        def get_all_group_chats(connection):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("БД занята")
            raise asyncio.CancelledError

    monkeypatch.setattr(ps, "db", FakeDb)
    monkeypatch.setattr(ps, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ps.unpin_after_lessons_loop(conn_with_schedule, PinBot())

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_loop_uses_interval(conn_with_schedule, monkeypatch) -> None:
    """Пауза между проходами — 5 минут."""
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(ps, "_sleep", recording_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ps.unpin_after_lessons_loop(conn_with_schedule, PinBot())

    assert slept == [ps.UNPIN_INTERVAL_SECONDS] == [5 * 60]


def test_unpin_task_registered_in_main() -> None:
    """Задача открепления подключена в main как фоновая."""
    import inspect

    import bot.main as main_module

    source = inspect.getsource(main_module.build_background_tasks)
    assert "unpin_after_lessons_loop" in source


def test_removed_chat_has_no_pin(conn) -> None:
    """После отвязки чата закрепления нет (запись удалена целиком)."""
    _link_chat(conn)
    _pin(conn)
    db.remove_group_chat(conn, CHAT_ID)

    assert db.get_pinned_message(conn, CHAT_ID) is None


def test_pinned_message_helpers(conn) -> None:
    """set/get/clear отметки о закреплении."""
    _link_chat(conn)
    assert db.get_pinned_message(conn, CHAT_ID) is None

    assert db.set_pinned_message(conn, CHAT_ID, 555, MONDAY.isoformat()) is True
    pinned = db.get_pinned_message(conn, CHAT_ID)
    assert pinned["pinned_message_id"] == 555
    assert pinned["pinned_date_iso"] == MONDAY.isoformat()

    assert db.clear_pinned_message(conn, CHAT_ID) is True
    assert db.get_pinned_message(conn, CHAT_ID) is None

    # Неизвестный чат — False, без исключения.
    assert db.set_pinned_message(conn, 424242, 1, "2026-09-28") is False