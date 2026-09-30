"""Тесты напоминаний о дедлайнах (bot.services.notify_service).

Проверяют дедупликацию через ``sent_notifications`` и деактивацию
пользователя, который заблокировал бота. Реальная отправка не выполняется:
Bot подменяется заглушкой.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot import db
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


@pytest.fixture()
def conn_chat(conn_with_users, parsed_schedule):
    """БД с пользователем и РЕАЛЬНЫМ расписанием.

    Нужна тестам полного расписания в чат: без кэша занятий в чат уходила бы
    только «замена вне плана», и проверить карточки дня было бы нельзя.
    """
    from bot.services import cache_service

    cache_service.save_schedule(conn_with_users, parsed_schedule)
    return conn_with_users


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


def _drop_personal_users(conn) -> None:
    """Убрать личных подписчиков: тест проверяет только чаты.

    Фикстура ``conn_with_users`` создаёт пользователя в 26КАД, поэтому для
    сценариев «только чат» его нужно удалить — иначе рассылка уйдёт и в личку
    и счётчики будут другими.
    """
    with transaction(conn):
        conn.execute("DELETE FROM users")


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


# --- рассылка в чаты групп (шаг 2) ---

async def test_process_sends_to_two_users_and_chat(conn_with_users) -> None:
    """Новая замена: 2 личных подписчика + 1 чат получили."""
    _drop_personal_users(conn_with_users)
    _add_user(conn_with_users, USER_ID)
    _add_user(conn_with_users, 556)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "КСТ 26КАД", "supergroup",
                      "26КАД", USER_ID)

    bot = FakeBot()
    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 3, "двое личных + один чат"
    # Отбираем только отправки сообщений: в ``sent`` попадают и вызовы
    # закрепления (PinChatMessage), у которых нет текста.
    recipients = [m.get("chat_id") for m in bot.sent
                  if m["method"] == "SendMessage"]
    assert sorted(recipients) == sorted([USER_ID, 556, -100500])


async def test_process_chat_gets_full_schedule_not_cards(
        conn_chat) -> None:
    """В чат уходит ПОЛНОЕ расписание, а не карточки замен."""
    _drop_personal_users(conn_chat)
    _seed(conn_chat, [SUB_FULL])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = FakeBot()

    await ns._process_substitutions(conn_chat, bot, TARGET, throttle=False)

    chat_messages = [m["text"] for m in bot.sent
                     if m.get("chat_id") == -100500 and m["text"]]
    assert len(chat_messages) == 1
    body = chat_messages[0]

    # Шапка группового формата с пометкой про замены и чётностью дня.
    assert "🔔 <b>Расписание на завтра · 26КАД</b>" in body
    assert "Понедельник, 28.09.2026" in body
    assert "Число 28 →" in body
    assert "@kst24_bot" in body

    # Полное расписание дня: плановые пары тоже присутствуют (📚), а не только
    # заменённая. В личку при этом уходят исключительно карточки замен.
    assert "📚 <b>" in body, "должны быть плановые пары дня"
    assert "🔁 <b>2 пара</b>" in body, "замена помечена иконкой 🔁"
    assert body.count("пара</b>") >= 3, "в понедельник несколько пар"


async def test_process_chat_full_schedule_marked_sent(
        conn_chat) -> None:
    """После отправки проставляется дата последнего полного расписания."""
    _drop_personal_users(conn_chat)
    _seed(conn_chat, [SUB_FULL])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)

    await ns._process_substitutions(conn_chat, FakeBot(), TARGET,
                                    throttle=False)

    link = db.get_group_chat(conn_chat, -100500)
    assert link["last_full_schedule_sent_date"] == TARGET.isoformat()


async def test_process_chat_no_resend_same_day_new_subs(
        conn_chat) -> None:
    """Вторая новая замена в тот же день → расписание НЕ отправляется снова."""
    _drop_personal_users(conn_chat)
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)

    # Первый цикл: замена на пару 2.
    _seed(conn_chat, [SUB_FULL])
    first_bot = FakeBot()
    first = await ns._process_substitutions(conn_chat, first_bot,
                                            TARGET, throttle=False)
    assert first == 1

    # Второй цикл: пришла ещё одна замена на тот же день.
    another = dict(SUB_FULL, para=3, old_subject="ОД.03 История",
                   new_subject="ОД.12 Химия")
    _seed(conn_chat, [SUB_FULL, another])
    second_bot = FakeBot()
    second = await ns._process_substitutions(conn_chat, second_bot,
                                             TARGET, throttle=False)

    assert second == 0, "полное расписание на ту же дату повторять нельзя"
    assert second_bot.sent == []


async def test_process_chat_sends_full_schedule_next_day(
        conn_chat) -> None:
    """Новая замена на СЛЕДУЮЩИЙ день → расписание отправляется снова."""
    _drop_personal_users(conn_chat)
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)

    _seed(conn_chat, [SUB_FULL])
    await ns._process_substitutions(conn_chat, FakeBot(), TARGET,
                                    throttle=False)

    next_day = TARGET + timedelta(days=1)
    _seed(conn_chat, [SUB_FULL,
                      dict(SUB_FULL, date_iso=next_day.isoformat())])
    bot = FakeBot()
    sent = await ns._process_substitutions(conn_chat, bot, next_day,
                                           throttle=False)

    assert sent == 1, "на новую дату расписание должно уйти"
    assert bot.sent[0]["chat_id"] == -100500
    assert db.get_group_chat(
        conn_chat, -100500
    )["last_full_schedule_sent_date"] == next_day.isoformat()


async def test_process_chat_empty_day_message(conn_chat) -> None:
    """Пар нет → «🎉 На завтра пар нет» с группой и датой.

    Проверяем на реальном расписании: у 26КАД в воскресенье пар нет.
    Замен на этот день не подкладываем — иначе «замена вне плана» из
    :func:`apply_substitutions` добавила бы пару и день перестал быть пустым.
    """
    _drop_personal_users(conn_chat)
    empty_day = date(2026, 9, 27)          # воскресенье, пар нет
    assert empty_day.weekday() == 6

    texts = ns.build_full_schedule_for_chat(conn_chat, "26КАД", empty_day, True)
    assert len(texts) == 1
    body = texts[0]
    assert "🎉 <b>На завтра пар нет</b> · 26КАД" in body
    assert "Воскресенье, 27.09.2026" in body


async def test_process_chat_only_without_personal_subscribers(
        conn_with_users) -> None:
    """Чат привязан, личных подписчиков нет → рассылка всё равно идёт."""
    _drop_personal_users(conn_with_users)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = FakeBot()

    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 1
    assert bot.sent[0]["chat_id"] == -100500


async def test_process_removes_chat_on_forbidden(conn_with_users) -> None:
    """TelegramForbiddenError в чате → chat_id удалён из БД."""
    from aiogram.exceptions import TelegramForbiddenError

    _drop_personal_users(conn_with_users)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)

    class ForbiddenBot(FakeBot):
        """Bot, который в чате отвечает «запрещено»."""

        async def __call__(self, method, request_timeout=None):
            if getattr(method, "chat_id", None) == -100500:
                raise TelegramForbiddenError(method=method, message="no rights")
            return await super().__call__(method, request_timeout)

    sent = await ns._process_substitutions(conn_with_users, ForbiddenBot(),
                                           TARGET, throttle=False)

    assert sent == 0
    assert db.get_group_chat(conn_with_users, -100500) is None, \
        "мёртвая привязка должна быть удалена"


async def test_process_chat_dedup_second_run(conn_with_users) -> None:
    """Повторный запуск → никому: ни личке, ни чату (общий дедуп)."""
    _drop_personal_users(conn_with_users)
    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)

    first_bot = FakeBot()
    first = await ns._process_substitutions(conn_with_users, first_bot,
                                            TARGET, throttle=False)
    second_bot = FakeBot()
    second = await ns._process_substitutions(conn_with_users, second_bot,
                                             TARGET, throttle=False)

    assert first == 2
    assert second == 0
    assert second_bot.sent == []


async def test_process_chats_of_other_groups_not_notified(
        conn_with_users) -> None:
    """Чат другой группы не получает замены этой группы."""
    _drop_personal_users(conn_with_users)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100777, "КСТ 26МЭГ", "supergroup",
                      "26МЭГ", USER_ID)
    bot = FakeBot()

    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 0
    assert bot.sent == []


async def test_process_group_without_notifications_disabled_chat(
        conn_with_users) -> None:
    """Чат с выключенными уведомлениями не получает рассылку."""
    _drop_personal_users(conn_with_users)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    with transaction(conn_with_users):
        conn_with_users.execute(
            "UPDATE group_chats SET notifications_enabled = 0 WHERE chat_id = ?",
            (-100500,),
        )
    bot = FakeBot()

    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 0
    assert bot.sent == []


async def test_process_multiple_chats_same_group(conn_with_users) -> None:
    """Одна группа в нескольких чатах — рассылка идёт во все."""
    _drop_personal_users(conn_with_users)
    _seed(conn_with_users, [SUB_FULL])
    db.add_group_chat(conn_with_users, -100500, "Чат A", "supergroup",
                      "26КАД", USER_ID)
    db.add_group_chat(conn_with_users, -100600, "Чат B", "group",
                      "26КАД", USER_ID)
    bot = FakeBot()

    sent = await ns._process_substitutions(conn_with_users, bot, TARGET,
                                           throttle=False)

    assert sent == 2
    assert {m["chat_id"] for m in bot.sent} == {-100500, -100600}


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
    """Ранее отправленная замена не повторяется — и новая тоже.

    Проверяем поведение из ТЗ: за вечер лист замен может обновиться несколько
    раз, но студент получает ОДНО полное расписание на дату. Дедуп идёт по
    ``(tg_id, дата)``, а не по подписи замены, поэтому новая замена в тот же
    день повторной отправки не вызывает.
    """
    _add_user(conn_with_users, USER_ID)
    _seed(conn_with_users, [SUB_FULL])
    bot1 = FakeBot()
    await ns._process_substitutions(conn_with_users, bot1, TARGET,
                                    throttle=False)
    assert bot1.sent, "первое расписание ушло"

    new_sub = dict(SUB_FULL, para=3, old_subject="ОД.01 Русский язык",
                   new_subject="ОД.13 Биология")
    _seed(conn_with_users, [SUB_FULL, new_sub])

    bot2 = FakeBot()
    sent = await ns._process_substitutions(conn_with_users, bot2, TARGET,
                                           throttle=False)

    assert sent == 0, "расписание на эту дату уже отправлено"
    assert bot2.sent == []
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
# --- закрепление расписания в чате (шаг 4) ---

class PinBot(FakeBot):
    """FakeBot, который помнит вызовы pin/unpin и умеет «ошибаться»."""

    def __init__(self, fail_pin: bool = False) -> None:
        super().__init__()
        self.pinned: list[dict] = []
        self.unpinned: list[dict] = []
        self.fail_pin = fail_pin

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
            self.unpinned.append({
                "chat_id": getattr(method, "chat_id", None),
                "message_id": getattr(method, "message_id", None),
            })
        return await super().__call__(method, request_timeout)


async def test_chat_schedule_is_pinned_silently(conn_chat) -> None:
    """Отправка расписания в чат → pin вызван тихо (disable_notification)."""
    _drop_personal_users(conn_chat)
    _seed(conn_chat, [SUB_FULL])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = PinBot()

    await ns._process_substitutions(conn_chat, bot, TARGET, throttle=False)

    assert len(bot.pinned) == 1
    pin = bot.pinned[0]
    assert pin["chat_id"] == -100500
    assert pin["disable_notification"] is True, "участников не уведомляем"
    pinned = db.get_pinned_message(conn_chat, -100500)
    assert pinned is not None
    assert pinned["pinned_date_iso"] == TARGET.isoformat()
    assert pinned["pinned_message_id"] == pin["message_id"]


async def test_old_pin_removed_before_new(conn_chat) -> None:
    """Перед новым закреплением снимается старое."""
    _drop_personal_users(conn_chat)
    _seed(conn_chat, [SUB_FULL])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    db.set_pinned_message(conn_chat, -100500, 4242, "2026-09-27")
    bot = PinBot()

    await ns._process_substitutions(conn_chat, bot, TARGET, throttle=False)

    assert bot.unpinned == [{"chat_id": -100500, "message_id": 4242}]
    assert len(bot.pinned) == 1
    pinned = db.get_pinned_message(conn_chat, -100500)
    assert pinned["pinned_message_id"] == bot.pinned[0]["message_id"]


async def test_pin_failure_keeps_chat_link(conn_chat, caplog) -> None:
    """pinChatMessage вернул TelegramBadRequest → привязка осталась, WARNING."""
    import logging as _logging

    _drop_personal_users(conn_chat)
    _seed(conn_chat, [SUB_FULL])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = PinBot(fail_pin=True)

    with caplog.at_level(_logging.WARNING):
        sent = await ns._process_substitutions(conn_chat, bot, TARGET,
                                               throttle=False)

    assert sent == 1, "сообщение всё равно ушло"
    assert bot.pinned == []
    assert db.get_group_chat(conn_chat, -100500) is not None, \
        "привязка чата должна остаться"
    assert db.get_pinned_message(conn_chat, -100500) is None
    assert any("pin failed" in r.message for r in caplog.records), \
        "должно быть предупреждение в логе"


async def test_empty_day_is_not_pinned(conn_chat) -> None:
    """Пар нет → расписание не закрепляем (висящее пустое только мешает).

    Проверяем на прямом вызове: когда замена есть, она сама добавляется в
    расписание отдельной строкой (:func:`apply_substitutions`), поэтому
    «пустой день» получается только без замен на него — например, когда
    триггером рассылки была замена на другую дату.
    """
    from bot.services.notify_service import build_full_schedule_for_chat

    _drop_personal_users(conn_chat)
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = PinBot()

    # Готовим текст «пар нет» на воскресенье и шлём его как расписание.
    empty_day = date(2026, 9, 27)
    texts = build_full_schedule_for_chat(conn_chat, "26КАД", empty_day, True)
    assert "На завтра пар нет" in texts[0]

    await ns._send_to_chat(conn_chat, bot, -100500, texts,
                           date_iso=empty_day.isoformat())

    assert bot.pinned == [], "пустой день закреплять нельзя"
    assert db.get_pinned_message(conn_chat, -100500) is None


# --- разделители в old_subject (часть 1) ---

def test_card_without_planned_lesson_has_no_strike() -> None:
    """Замена без плановой пары: строки «<s>...</s>» быть не должно."""
    sub = dict(SUB_FULL, old_subject="", new_subject="",
               teacher="", room="", para=1, is_cancelled=True)
    body = ns.render_substitution_card(sub, TARGET)

    assert "<s>" not in body, "зачёркнутой строки быть не должно"
    assert "————————————————" not in body
    assert "❌ <b>1 пара</b>" in body
    assert "<i>Пара отменена</i>" in body


def test_card_with_divider_placeholder_is_cleaned() -> None:
    """Разделитель в old_subject не показывается (старые данные в БД)."""
    sub = dict(SUB_FULL, old_subject="————————————————", new_subject="",
               is_cancelled=True)
    body = ns.render_substitution_card(sub, TARGET)

    assert "<s>" not in body
    assert "—" not in body, "разделитель не должен попасть в сообщение"


def test_card_with_planned_lesson_keeps_strike() -> None:
    """Замена с плановой парой: строка «<s>старый предмет</s>» остаётся."""
    sub = dict(SUB_FULL, old_subject="ОД.03 История",
               new_subject="ОД.07 Математика")
    body = ns.render_substitution_card(sub, TARGET)

    assert "<s>ОД.03 История</s>" in body
    assert "<b>ОД.07 Математика</b>" in body


def test_clean_subject_helper() -> None:
    """Хелпер рендера: заглушка → пустая строка, предмет экранируется."""
    assert ns._clean_subject("————————————————") == ""
    assert ns._clean_subject("") == ""
    assert ns._clean_subject("ОД.03 История") == "ОД.03 История"
    assert ns._clean_subject("<b>злой</b>") == "&lt;b&gt;злой&lt;/b&gt;"
    assert ns._clean_subject("—") == ""


async def test_group_chat_message_has_no_divider(conn_chat) -> None:
    """В сообщении для чата тоже нет разделителя (общий рендер карточки)."""
    _drop_personal_users(conn_chat)
    cancelled = dict(SUB_FULL, para=1, old_subject="————————————————",
                     new_subject="", teacher="", room="", is_cancelled=True)
    _seed(conn_chat, [cancelled])
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = FakeBot()

    await ns._process_substitutions(conn_chat, bot, TARGET, throttle=False)

    body = bot.sent[0]["text"]
    assert "<s>" not in body
    assert "————————————————" not in body


async def test_unpin_previous_ignores_errors(conn_chat) -> None:
    """Ошибка unpin не мешает: отметка в БД снимается."""
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    db.set_pinned_message(conn_chat, -100500, 999, "2026-09-27")

    class FailingUnpin(FakeBot):
        async def __call__(self, method, request_timeout=None):
            if type(method).__name__ == "UnpinChatMessage":
                raise RuntimeError("message is not pinned")
            return await super().__call__(method, request_timeout)

    await ns.unpin_previous(FailingUnpin(), conn_chat, -100500)

    assert db.get_pinned_message(conn_chat, -100500) is None


async def test_no_pin_without_date(conn_chat) -> None:
    """Без даты (date_iso="") закрепления нет — у /schedule он не нужен."""
    db.add_group_chat(conn_chat, -100500, "КСТ", "supergroup",
                      "26КАД", USER_ID)
    bot = PinBot()

    await ns._send_to_chat(conn_chat, bot, -100500, ["текст расписания"])

    assert bot.pinned == []
    assert len(bot.sent) == 1
# --- полное расписание в личку вместо карточек замен ---
#
# Проблема, которую закрывают эти тесты: студент получал только карточки
# («❌ 1 пара ... Пара отменена») и не видел остального дня. Теперь и в личку,
# и в чат уходит ПОЛНОЕ расписание на завтра с учётом замен.

# Четверг: замена 2 пары (не отменена — так видно и 📚, и 🔁).
FULL_TARGET = date(2026, 10, 1)


def _full_schedule_users(tmp_path: Path, count: int = 2, name: str = "full.db"):
    """БД с расписанием, заменой и несколькими пользователями группы.

    Returns:
        ``(conn, [tg_id, ...])``.
    """
    c = get_connection(tmp_path / name)
    apply_migrations(c)
    with transaction(c):
        for para, subject in [(1, "История"), (2, "Литература"),
                              (3, "Физика")]:
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES ('26КАД', 4, ?, ?, 'Тест Т.Т.', '101', '', 'x')",
                (para, subject),
            )
        c.execute(
            "INSERT INTO substitutions_cache (group_name, date_iso, para,"
            " old_subject, new_subject, teacher, room, is_cancelled,"
            " is_self_study, fetched_at)"
            " VALUES ('26КАД', ?, 2, 'Литература', 'ОД.11 Астрономия',"
            " 'Кудрявцева П.А.', '307А', 0, 0, 'x')",
            (FULL_TARGET.isoformat(),),
        )
        ids = []
        for index in range(count):
            tg_id = 9000 + index
            c.execute(
                "INSERT INTO users (tg_id, group_name, notifications_enabled,"
                " created_at) VALUES (?, '26КАД', 1, 'x')", (tg_id,),
            )
            ids.append(tg_id)
    return c, ids


async def test_user_gets_full_not_cards(tmp_path: Path) -> None:
    """В личку уходит ПОЛНОЕ расписание, а не карточки замен.

    Проверяем оба признака: есть плановые пары (📚) и заменённые (🔁), и нет
    старого формата «Пара отменена».
    """
    conn, ids = _full_schedule_users(tmp_path, count=1)
    bot = FakeBot()

    sent = await ns._process_substitutions(conn, bot, FULL_TARGET,
                                           throttle=False)

    assert sent == 1
    body = "\n".join(m["text"] for m in bot.sent if m["text"])
    assert "🔔 <b>Расписание на завтра · 26КАД</b>" in body
    assert "🔁" in body, "заменённая пара"
    assert "📚" in body, "плановая пара"
    assert "ОД.11 Астрономия" in body
    assert "Пара отменена" not in body, "старого формата быть не должно"
    assert "Подробнее в боте: /schedule" in body, "футер для лички"
    conn.close()


async def test_user_schedule_header_replaced(tmp_path: Path) -> None:
    """Заголовок «Замены на завтра» больше не используется в личке."""
    conn, ids = _full_schedule_users(tmp_path, count=1)
    bot = FakeBot()

    await ns._process_substitutions(conn, bot, FULL_TARGET, throttle=False)

    body = "\n".join(m["text"] for m in bot.sent if m["text"])
    assert "Замены на завтра" not in body
    assert "Расписание на завтра" in body
    conn.close()


async def test_two_users_both_get_one_message(tmp_path: Path) -> None:
    """Двое студентов получают по одному сообщению каждый."""
    conn, ids = _full_schedule_users(tmp_path, count=2)
    bot = FakeBot()

    sent = await ns._process_substitutions(conn, bot, FULL_TARGET,
                                           throttle=False)

    assert sent == 2
    recipients = [m["chat_id"] for m in bot.sent if m.get("chat_id")]
    assert sorted(recipients) == sorted(ids)
    conn.close()
async def test_second_run_same_day_sends_nothing(tmp_path: Path) -> None:
    """Повторный прогон в тот же день — 0 отправок (дедуп по tg_id и дате)."""
    conn, ids = _full_schedule_users(tmp_path, count=1)
    bot = FakeBot()
    await ns._process_substitutions(conn, bot, FULL_TARGET, throttle=False)
    bot.sent.clear()

    again = await ns._process_substitutions(conn, bot, FULL_TARGET,
                                            throttle=False)

    assert again == 0
    assert bot.sent == []
    conn.close()


async def test_new_substitution_same_day_no_resend(tmp_path: Path) -> None:
    """Новая замена в тот же день не присылает расписание повторно.

    Требование ТЗ: даже если лист замен обновился несколько раз за вечер,
    студент получает ОДНО полное расписание на дату.
    """
    conn, ids = _full_schedule_users(tmp_path, count=1)
    bot = FakeBot()
    await ns._process_substitutions(conn, bot, FULL_TARGET, throttle=False)
    bot.sent.clear()

    with transaction(conn):
        conn.execute(
            "INSERT INTO substitutions_cache (group_name, date_iso, para,"
            " old_subject, new_subject, teacher, room, is_cancelled,"
            " is_self_study, fetched_at)"
            " VALUES ('26КАД', ?, 3, 'Физика', 'ОД.99 Астрономия',"
            " 'Т.Т.', '202', 0, 0, 'x')", (FULL_TARGET.isoformat(),),
        )
    again = await ns._process_substitutions(conn, bot, FULL_TARGET,
                                            throttle=False)

    assert again == 0, "расписание на эту дату уже отправлено"
    conn.close()


def test_dedup_signature_is_per_user() -> None:
    """Подпись дедупа включает tg_id — иначе первый закрыл бы группу."""
    sig_first = ns.full_schedule_signature(111, FULL_TARGET.isoformat())
    sig_second = ns.full_schedule_signature(222, FULL_TARGET.isoformat())

    assert sig_first != sig_second
    assert FULL_TARGET.isoformat() in sig_first


async def test_forbidden_user_deactivated_other_gets(tmp_path: Path) -> None:
    """Блокировка бота одним студентом не мешает второму."""
    from aiogram.exceptions import TelegramForbiddenError

    conn, ids = _full_schedule_users(tmp_path, count=2)
    blocked = ids[0]

    class BlockingBot(FakeBot):
        """Падает с TelegramForbiddenError для одного получателя."""

        async def __call__(self, method, request_timeout=None):
            name = type(method).__name__
            chat_id = getattr(method, "chat_id", None)
            if name == "SendMessage" and chat_id == blocked:
                raise TelegramForbiddenError(method=method, message="blocked")
            return await super().__call__(method, request_timeout)

    bot = BlockingBot()
    sent = await ns._process_substitutions(conn, bot, FULL_TARGET,
                                           throttle=False)

    assert sent == 1, "второй студент получил"
    row = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (blocked,)
    ).fetchone()
    assert row["is_active"] == 0
    conn.close()


async def test_empty_day_message_for_user(tmp_path: Path) -> None:
    """Пар нет на завтра → «🎉 На завтра пар нет».

    Проверяем саму функцию сборки: замена на день без пар создала бы «пару
    вне плана», и тест проверял бы не пустой день, а карточку замены.
    """
    conn, ids = _full_schedule_users(tmp_path, count=1)
    empty = date(2026, 9, 6)      # воскресенье: в расписании пары нет

    texts = ns.build_full_schedule_for_target(conn, "26КАД", empty,
                                              for_chat=False)

    assert len(texts) == 1
    assert "На завтра пар нет" in texts[0]
    conn.close()
# --- build_full_schedule_for_target: личка против чата ---

async def test_target_footers_differ(tmp_path: Path) -> None:
    """Футер различается: личка → /schedule, чат → ссылка на бота."""
    conn, ids = _full_schedule_users(tmp_path, count=1)

    personal = ns.build_full_schedule_for_target(conn, "26КАД", FULL_TARGET,
                                                 for_chat=False)
    chat = ns.build_full_schedule_for_target(conn, "26КАД", FULL_TARGET,
                                             for_chat=True)

    assert "Подробнее в боте: /schedule" in personal[0]
    assert "Подробности — в боте в личке: @kst24_bot" in chat[0]
    conn.close()


async def test_target_body_is_same_for_both(tmp_path: Path) -> None:
    """Тело расписания совпадает: различается только футер."""
    conn, ids = _full_schedule_users(tmp_path, count=1)

    personal = ns.build_full_schedule_for_target(conn, "26КАД", FULL_TARGET,
                                                 for_chat=False)[0]
    chat = ns.build_full_schedule_for_target(conn, "26КАД", FULL_TARGET,
                                             for_chat=True)[0]

    assert personal.rsplit("\n", 1)[0] == chat.rsplit("\n", 1)[0]
    conn.close()


async def test_chat_wrapper_respects_flag(tmp_path: Path) -> None:
    """Обёртка для чата не шлёт ничего, если новых замен нет."""
    conn, ids = _full_schedule_users(tmp_path, count=1)

    assert ns.build_full_schedule_for_chat(conn, "26КАД", FULL_TARGET,
                                           False) == []
    assert ns.build_full_schedule_for_chat(conn, "26КАД", FULL_TARGET, True)
    conn.close()


async def test_chat_gets_full_schedule_too(tmp_path: Path) -> None:
    """Чат группы получает то же полное расписание (не карточки)."""
    conn, ids = _full_schedule_users(tmp_path, count=1)
    chat_id = -100900
    db.add_group_chat(conn, chat_id, "Группа", "group", "26КАД", ids[0])
    bot = FakeBot()

    await ns._process_substitutions(conn, bot, FULL_TARGET, throttle=False)

    chat_msgs = [m["text"] for m in bot.sent
                 if m.get("chat_id") == chat_id and m["text"]]
    assert chat_msgs, "в чат тоже ушло расписание"
    body = "\n".join(chat_msgs)
    assert "Расписание на завтра" in body
    assert "🔁" in body and "📚" in body
    assert "Подробности — в боте в личке: @kst24_bot" in body
    conn.close()


async def test_old_render_still_available() -> None:
    """Старая функция карточек замен не удалена (используется в других местах)."""
    assert callable(ns.render_substitution_notification)
    texts = ns.render_substitution_notification("26КАД", [], FULL_TARGET)
    assert texts == []