"""Тесты напоминаний о дедлайнах (bot.services.notify_service).

Проверяют дедупликацию через ``sent_notifications`` и деактивацию
пользователя, который заблокировал бота. Реальная отправка не выполняется:
Bot подменяется заглушкой.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import deadline_service as dl
from bot.services import notify_service as ns
from tests.test_handlers_dispatch import FakeBot

USER_ID = 555
TODAY = date(2026, 9, 27)


async def _no_sleep(seconds: float) -> None:
    """Заглушка паузы (тесты не должны ждать)."""
    return None


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и пользователем."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, '26КАД', '2026-09-01T00:00:00+07:00')",
            (USER_ID,),
        )
    yield c
    c.close()


# --- deadline_message ---

def test_message_day_of() -> None:
    text = ns.deadline_message({
        "task": "Лаба", "subject": "Химия",
        "deadline_date": "2026-09-27", "days_left": 0,
    })
    assert "🟠 <b>Дедлайн сегодня</b>" in text
    assert "<b>Лаба</b> — Химия, 27.09" in text


def test_message_day_before() -> None:
    text = ns.deadline_message({
        "task": "Лаба", "subject": "Химия",
        "deadline_date": "2026-09-28", "days_left": 1,
    })
    assert "🟡 <b>Дедлайн завтра</b>" in text
    assert "28.09" in text


def test_message_empty_for_other_days() -> None:
    """Для срока дальше завтра напоминания нет."""
    assert ns.deadline_message({
        "task": "X", "subject": "Y",
        "deadline_date": "2026-10-10", "days_left": 13,
    }) == ""


def test_message_escapes_html() -> None:
    text = ns.deadline_message({
        "task": "<b>взлом</b>", "subject": "<i>предмет</i>",
        "deadline_date": "2026-09-27", "days_left": 0,
    })
    assert "<b>взлом</b>" not in text
# --- check_deadlines_once ---

async def test_notify_sends_for_today_and_tomorrow(conn) -> None:
    """Отправляются оба напоминания: сегодня и завтра."""
    dl.add(conn, USER_ID, "Химия", "", "сегодня сдать", "2026-09-27")
    dl.add(conn, USER_ID, "Физика", "", "завтра сдать", "2026-09-28")
    bot = FakeBot()

    sent = await ns.check_deadlines_once(conn, bot, today=TODAY, throttle=False)

    assert sent == 2
    texts = [m["text"] for m in bot.sent if m["text"]]
    assert any("Дедлайн сегодня" in t for t in texts)
    assert any("Дедлайн завтра" in t for t in texts)


async def test_notify_deduplicates_within_same_day(conn) -> None:
    """Повторный вызов в тот же день ничего не отправляет."""
    dl.add(conn, USER_ID, "Химия", "", "сегодня", "2026-09-27")
    bot = FakeBot()

    first = await ns.check_deadlines_once(conn, bot, today=TODAY, throttle=False)
    second = await ns.check_deadlines_once(conn, bot, today=TODAY, throttle=False)

    assert first == 1
    assert second == 0, "дублей быть не должно"
    rows = conn.execute(
        "SELECT group_name, signature FROM sent_notifications"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["group_name"] == ns.DEADLINE_GROUP
    assert rows[0]["signature"].startswith("deadline:")


async def test_notify_day_of_and_day_before_are_separate(conn) -> None:
    """Один дедлайн за день и в день срока — два разных уведомления."""
    deadline_id = dl.add(conn, USER_ID, "Химия", "", "сдать", "2026-09-28")
    bot = FakeBot()

    before = await ns.check_deadlines_once(
        conn, bot, today=date(2026, 9, 27), throttle=False)
    after = await ns.check_deadlines_once(
        conn, bot, today=date(2026, 9, 28), throttle=False)

    assert before == 1
    assert after == 1
    signatures = {
        row["signature"] for row in conn.execute(
            "SELECT signature FROM sent_notifications"
        ).fetchall()
    }
    assert f"deadline:{deadline_id}:{ns.KIND_DAY_BEFORE}" in signatures
    assert f"deadline:{deadline_id}:{ns.KIND_DAY_OF}" in signatures


async def test_notify_skips_deleted(conn) -> None:
    """Удалённые дедлайны не напоминаются."""
    deadline_id = dl.add(conn, USER_ID, "Химия", "", "сдать", "2026-09-27")
    dl.soft_delete(conn, deadline_id, USER_ID)
    bot = FakeBot()

    assert await ns.check_deadlines_once(
        conn, bot, today=TODAY, throttle=False) == 0


async def test_notify_no_deadlines_sends_nothing(conn) -> None:
    bot = FakeBot()
    assert await ns.check_deadlines_once(
        conn, bot, today=TODAY, throttle=False) == 0
    assert bot.sent == []


async def test_notify_deactivates_blocked_user(conn) -> None:
    """Ошибка отправки → users.is_active = 0, отправка не помечается."""
    dl.add(conn, USER_ID, "Химия", "", "сдать", "2026-09-27")

    class BlockedBot(FakeBot):
        """Bot, который всегда отвечает ошибкой (бот заблокирован)."""

        async def __call__(self, method, request_timeout=None):
            raise RuntimeError("Forbidden: bot was blocked by the user")

    sent = await ns.check_deadlines_once(
        conn, BlockedBot(), today=TODAY, throttle=False)

    assert sent == 0
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM sent_notifications"
    ).fetchone()[0] == 0


async def test_check_deadlines_once_without_explicit_today(conn) -> None:
    """Вызов без ``today`` не падает (регресс: TIMEZONE был не определён).

    Именно этот путь выполняется в фоновом цикле, поэтому он обязан
    работать без явной даты. Тест ловит обращение к несуществующему имени
    в вычислении текущей даты.
    """
    dl.add(conn, USER_ID, "Химия", "", "сдать", "2099-01-01")
    bot = FakeBot()

    sent = await ns.check_deadlines_once(conn, bot, throttle=False)
    assert sent == 0, "далёкий срок не напоминается, но вызов проходит"


async def test_check_deadlines_once_sends_without_explicit_today(conn) -> None:
    """Без явной даты находятся дедлайны на сегодня (путь фонового цикла)."""
    today = datetime.now(ns.KRASNOYARSK).date()
    dl.add(conn, USER_ID, "Химия", "", "сдать", today.isoformat())
    bot = FakeBot()

    sent = await ns.check_deadlines_once(conn, bot, throttle=False)
    assert sent == 1, "напоминание на сегодня должно уйти"


async def test_process_substitutions_without_explicit_target(conn) -> None:
    """Рассылка замен на вычисленную дату работает (путь фонового цикла)."""
    _add_user(conn, USER_ID)
    target = ns.next_school_day(datetime.now(ns.KRASNOYARSK).date())
    _seed(conn, [dict(SUB_FULL, date_iso=target.isoformat())])
    bot = FakeBot()

    sent = await ns._process_substitutions(conn, bot, target, throttle=False)
    assert sent == 1


async def test_notify_loop_survives_errors(conn, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл; отмена пролетает наружу."""
    calls: list[int] = []

    async def fake_check(connection, bot, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой")
        raise asyncio.CancelledError

    monkeypatch.setattr(ns, "check_deadlines_once", fake_check)
    monkeypatch.setattr(ns, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ns.deadline_notify_loop(conn, FakeBot(), interval=3600)

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


# ==========================================================================
# Рассылка замен (шаг 10)
# ==========================================================================

TARGET = date(2026, 9, 28)          # понедельник

SUB_FULL = {
    "group": "26КАД", "date_iso": "2026-09-28", "para": 2,
    "old_subject": "ОД.03 История", "new_subject": "ОД.07 Математика",
    "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
    "is_cancelled": False, "is_self_study": False,
}


@pytest.fixture()
def conn_with_users(tmp_path: Path):
    """БД с активным пользователем группы 26КАД."""
    c = get_connection(tmp_path / "notify.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, '26КАД', '2026-09-01T00:00:00+07:00')", (USER_ID,),
        )
    yield c
    c.close()


# --- substitution_signature ---

def test_signature_stable() -> None:
    """Две идентичные замены дают одну подпись."""
    assert ns.substitution_signature(SUB_FULL) == ns.substitution_signature(
        dict(SUB_FULL)
    )


@pytest.mark.parametrize("field,value", [
    ("para", 3),
    ("old_subject", "Другое"),
    ("new_subject", "Другое"),
    ("teacher", "Другой"),
    ("room", "999"),
    ("date_iso", "2026-09-29"),
])
def test_signature_changes_on_field_change(field: str, value) -> None:
    """Изменение любого поля меняет подпись."""
    changed = dict(SUB_FULL, **{field: value})
    assert ns.substitution_signature(changed) != ns.substitution_signature(SUB_FULL)


# --- next_school_day ---

@pytest.mark.parametrize(("raw", "expected"), [
    (date(2026, 9, 24), date(2026, 9, 25)),   # чт → пт
    (date(2026, 9, 25), date(2026, 9, 26)),   # пт → сб
    (date(2026, 9, 26), date(2026, 9, 28)),   # сб → пн (вс пропущено)
    (date(2026, 9, 27), date(2026, 9, 28)),   # вс → пн
])
def test_next_school_day(raw: date, expected: date) -> None:
    assert ns.next_school_day(raw) == expected


def test_next_school_day_never_sunday() -> None:
    """Никогда не возвращает воскресенье (весь месяц)."""
    day = date(2026, 9, 1)
    while day.month == 9:
        assert ns.next_school_day(day).weekday() != 6
        day += timedelta(days=1)


# --- окно рассылки ---

@pytest.mark.parametrize(("hh", "mm", "expected"), [
    (9, 0, False),
    (15, 29, False),
    (15, 30, True),
    (18, 0, True),
    (23, 0, True),
    (23, 1, False),
])
def test_notify_window(hh: int, mm: int, expected: bool) -> None:
    moment = datetime(2026, 9, 28, hh, mm)
    assert ns.in_notify_window(moment) is expected
# --- рендер ---

def test_render_notification_header_and_footer() -> None:
    texts = ns.render_substitution_notification("26КАД", [SUB_FULL], TARGET)
    assert len(texts) == 1
    body = texts[0]
    assert "🔔 <b>Замены на завтра</b>" in body
    assert "26КАД · Понедельник, 28.09" in body
    assert "Загляни в «Расписание»" in body


def test_render_card_format() -> None:
    """Карточка: зачёркнутый старый предмет, новый, ФИО, кабинет, время."""
    body = ns.render_substitution_notification("26КАД", [SUB_FULL], TARGET)[0]
    assert "🔁 <b>2 пара</b>" in body
    assert "<s>ОД.03 История</s>" in body
    assert "<b>ОД.07 Математика</b>" in body
    assert "👤 Кудрявцева Полина Алексеевна" in body
    assert "🚪 307А" in body
    assert "⏰ 10:45-12:20" in body


@pytest.mark.parametrize(("flags", "icon"), [
    ({"is_cancelled": False, "is_self_study": False}, "🔁"),
    ({"is_cancelled": True, "is_self_study": False}, "❌"),
    ({"is_cancelled": False, "is_self_study": True}, "📖"),
])
def test_render_icons(flags: dict, icon: str) -> None:
    body = ns.render_substitution_notification(
        "26КАД", [dict(SUB_FULL, **flags)], TARGET
    )[0]
    assert icon in body


def test_render_escapes_html() -> None:
    """Опасные символы в предмете экранируются."""
    sub = dict(SUB_FULL, old_subject="<script>alert(1)</script>",
               new_subject="Математика & физика", teacher="<b>злой</b>")
    body = ns.render_substitution_notification("26КАД", [sub], TARGET)[0]
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "Математика &amp; физика" in body
    assert "&lt;b&gt;злой&lt;/b&gt;" in body


def test_render_no_subs_returns_empty() -> None:
    assert ns.render_substitution_notification("26КАД", [], TARGET) == []


SATURDAY = date(2026, 10, 3)


def test_render_card_saturday_uses_saturday_time() -> None:
    """Замена в субботу: время по субботним звонкам (3 пара — 12:50-14:20)."""
    sub = dict(SUB_FULL, para=3, date_iso=SATURDAY.isoformat())
    body = ns.render_substitution_notification("26КАД", [sub], SATURDAY)[0]
    assert "⏰ 12:50-14:20" in body
    assert "13:15-14:50" not in body


def test_render_card_weekday_uses_weekday_time() -> None:
    """Замена в будни: время по будничным звонкам (регресс не сломан)."""
    sub = dict(SUB_FULL, para=3, date_iso="2026-10-02")   # пятница
    body = ns.render_substitution_notification(
        "26КАД", [sub], date(2026, 10, 2)
    )[0]
    assert "⏰ 13:15-14:50" in body


# --- split_blocks ---

def test_split_short_message_single_part() -> None:
    parts = ns.split_blocks("H", ["a", "b"], "F")
    assert len(parts) == 1
    assert parts[0].startswith("H")
    assert parts[0].endswith("F")


def test_split_long_message_into_parts() -> None:
    """Длинный список разбивается на 2+ части по границам карточек."""
    header = "🔔 <b>Замены на завтра</b>"
    blocks = [f"🔁 <b>{i} пара</b>\n<s>Старый предмет {i}</s>\n<b>Новый {i}</b>"
              for i in range(1, 61)]
    footer = "Загляни в «Расписание»"
    parts = ns.split_blocks(header, blocks, footer, limit=500)

    assert len(parts) >= 2
    for part in parts:
        assert header in part, "в каждой части должна быть шапка"
    # Карточки не разорваны и не продублированы.
    for block in blocks:
        assert sum(block in part for part in parts) == 1


def test_split_blocks_empty() -> None:
    assert ns.split_blocks("H", [], "F") == []


def test_split_blocks_footer_when_no_room() -> None:
    """Если футер не влезает — он уходит отдельным сообщением."""
    parts = ns.split_blocks("H", ["x" * 400], "y" * 300, limit=450)
    assert any("y" * 300 == part for part in parts)
# --- _process_substitutions ---

def _add_user(conn, tg_id: int, group: str = "26КАД",
              active: int = 1) -> None:
    """Добавить или обновить пользователя в БД."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, is_active, created_at)"
            " VALUES (?, ?, ?, 'x')"
            " ON CONFLICT(tg_id) DO UPDATE SET"
            "   group_name = excluded.group_name,"
            "   is_active = excluded.is_active",
            (tg_id, group, active),
        )


def _seed(conn, subs: list[dict]) -> None:
    """Положить замены в кэш."""
    from bot.services import cache_service

    cache_service.save_substitutions(conn, subs)


async def test_process_sends_to_both_users_once(conn_with_users) -> None:
    """Двое в группе, одна замена → оба получили ровно один раз."""
    _add_user(conn_with_users, USER_ID)
    _add_user(conn_with_users, 556)
    _seed(conn_with_users, [SUB_FULL])

    bot = FakeBot()
    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 2
    assert len(bot.sent) == 2
    recipients = {m.get("chat_id") for m in bot.sent}
    assert recipients == {USER_ID, 556}


async def test_process_deduplicates_on_second_run(conn_with_users) -> None:
    """Повторный запуск: сообщений нет (дедуп по подписи)."""
    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [SUB_FULL])

    bot1 = FakeBot()
    first = await ns._process_substitutions(conn_with_users, bot1, TARGET,
                                            throttle=False)
    bot2 = FakeBot()
    second = await ns._process_substitutions(conn_with_users, bot2, TARGET,
                                             throttle=False)

    assert first == 1
    assert second == 0
    assert bot2.sent == []


async def test_process_deactivates_blocked_user(conn_with_users) -> None:
    """TelegramForbiddenError → is_active=0, повторно не пишем."""
    from aiogram.exceptions import TelegramForbiddenError

    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [SUB_FULL])

    class BlockedBot(FakeBot):
        """Bot, который отвечает «бот заблокирован»."""

        async def __call__(self, method, request_timeout=None):
            raise TelegramForbiddenError(method=method, message="blocked")

    sent = await ns._process_substitutions(conn_with_users, BlockedBot(),
                                           TARGET, throttle=False)

    assert sent == 0
    active = conn_with_users.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 0

    bot = FakeBot()
    assert await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False) == 0
    assert bot.sent == []


async def test_process_no_users_in_group(conn_with_users) -> None:
    """Замена есть, подписчиков в группе нет → не отправляем, но помечаем."""
    # Замена для группы, в которой никто не зарегистрирован.
    _seed(conn_with_users, [dict(SUB_FULL, group="26ЗЗЗ")])
    bot = FakeBot()

    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 0
    assert bot.sent == []
    # Помечаем отправленным, чтобы новый студент не получил старые замены:
    # рассылка идёт по группам из users, где есть активные подписчики,
    # поэтому в этом случае «26ЗЗЗ» даже не попадёт в список групп.
    assert conn_with_users.execute(
        "SELECT COUNT(*) FROM sent_notifications"
    ).fetchone()[0] == 0


async def test_process_ignores_other_dates(conn_with_users) -> None:
    """Замены на другую дату не рассылаются."""
    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [dict(SUB_FULL, date_iso="2026-10-01")])
    bot = FakeBot()

    assert await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False) == 0


async def test_process_ignores_inactive_users(conn_with_users) -> None:
    """Неактивные пользователи не получают рассылку."""
    _add_user(conn_with_users, USER_ID, active=0)
    _seed(conn_with_users, [SUB_FULL])
    bot = FakeBot()

    assert await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False) == 0


async def test_process_only_new_substitutions_sent(conn_with_users) -> None:
    """Ранее отправленная замена не повторяется, новая — уходит."""
    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [SUB_FULL])
    bot1 = FakeBot()
    await ns._process_substitutions(conn_with_users, bot1, TARGET,
                                    throttle=False)

    new_sub = dict(SUB_FULL, para=3, old_subject="ОД.01 Русский язык",
                   new_subject="ОД.13 Биология")
    _seed(conn_with_users, [SUB_FULL, new_sub])

    bot2 = FakeBot()
    sent = await ns._process_substitutions(conn_with_users, bot2, TARGET,
                                           throttle=False)

    assert sent == 1
    assert "ОД.13 Биология" in bot2.sent[0].get("text", "")
# --- notify_substitutions_loop ---

async def test_loop_sleeps_outside_window(conn_with_users, monkeypatch) -> None:
    """Вне окна 15:30–23:00 цикл спит и замены не рассылает."""
    processed: list[int] = []

    async def fake_process(connection, bot, target, **kwargs):
        processed.append(1)
        return 0

    slept: list[int] = []

    async def record_sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(ns, "_process_substitutions", fake_process)
    monkeypatch.setattr(ns, "_sleep", record_sleep)

    morning = datetime(2026, 9, 28, 10, 0)      # 10:00 — вне окна
    with pytest.raises(asyncio.CancelledError):
        await ns.notify_substitutions_loop(
            conn_with_users, FakeBot(), now_provider=lambda: morning
        )

    assert processed == [], "вне окна рассылки быть не должно"
    assert slept == [ns.OUTSIDE_WINDOW_SLEEP]


async def test_loop_processes_inside_window(conn_with_users, monkeypatch) -> None:
    """В окне цикл рассылает замены на следующий учебный день."""
    targets: list[date] = []

    async def fake_process(connection, bot, target, **kwargs):
        targets.append(target)
        raise asyncio.CancelledError

    monkeypatch.setattr(ns, "_process_substitutions", fake_process)
    monkeypatch.setattr(ns, "_sleep", _no_sleep)

    evening = datetime(2026, 9, 27, 18, 0)      # вс, 18:00 — в окне
    with pytest.raises(asyncio.CancelledError):
        await ns.notify_substitutions_loop(
            conn_with_users, FakeBot(), now_provider=lambda: evening
        )

    assert targets == [date(2026, 9, 28)], "вс → следующий учебный день = пн"


async def test_loop_survives_errors(conn_with_users, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: следующий проход выполняется."""
    calls: list[int] = []

    async def failing_process(connection, bot, target, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой рассылки")
        raise asyncio.CancelledError

    monkeypatch.setattr(ns, "_process_substitutions", failing_process)
    monkeypatch.setattr(ns, "_sleep", _no_sleep)

    evening = datetime(2026, 9, 28, 18, 0)
    with pytest.raises(asyncio.CancelledError):
        await ns.notify_substitutions_loop(
            conn_with_users, FakeBot(), now_provider=lambda: evening
        )

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"
    assert len(calls) == 2