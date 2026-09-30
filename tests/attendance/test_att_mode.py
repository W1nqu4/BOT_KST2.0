"""Тесты режима посещаемости и опроса «Да/Нет» (миграция 14).

Проверяются: миграция, выбор режима старостой, рассылка в двух режимах,
авто-``absent`` при закрытии и итоговые сообщения.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot.attendance import attendance_service as svc
from bot.attendance import attendance_texts as atexts
from bot.attendance import db as att_db
from bot.attendance import service
from bot.config import KRASNOYARSK
from bot.db import get_connection, transaction
from bot.migrations import MIGRATIONS, apply_migrations, get_schema_version
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
STAROSTA = 9201
S1, S2, S3 = 9202, 9203, 9204
CHAT_ID = -100920
PARA = 3
SUBJECT = "Физика"

# Среда 30.09.2026: 3 пара будни (13:15-14:50).
DAY = date(2026, 9, 30)
DATE_ISO = DAY.isoformat()


@pytest.fixture()
def conn(tmp_path: Path):
    """БД: группа с чатом и парой в среду."""
    c = get_connection(tmp_path / "att_mode.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
            " subject, teacher, room, week_type, updated_at)"
            " VALUES (?, 3, ?, ?, 'Т.Т.', '', '', 'x')",
            (GROUP, PARA, SUBJECT),
        )
        c.execute(
            "INSERT INTO group_chats (chat_id, chat_title, chat_type,"
            " group_name, added_by, added_at, notifications_enabled)"
            " VALUES (?, 'Группа', 'group', ?, ?, 'x', 1)",
            (CHAT_ID, GROUP, STAROSTA),
        )
        code = service.create_group(c, GROUP, STAROSTA, "Абрамчик С.Г.")
        for index, tg_id in enumerate([S1, S2, S3], start=1):
            service.join_group(c, tg_id, code, f"Студент{index} И.И.")
    yield c
    c.close()


def _poll(conn) -> dict:
    """Строка опроса по тестовой паре."""
    row = conn.execute(
        "SELECT * FROM attendance_polls WHERE group_name = ? AND date_iso = ?"
        " AND para = ?", (GROUP, DATE_ISO, PARA)
    ).fetchone()
    return dict(row) if row else {}


def _texts(bot: FakeBot) -> list[str]:
    """Тексты отправленных и отредактированных сообщений."""
    return [m["text"] for m in bot.sent if m["text"]]


# --- миграция 14 ---

def test_migration_14_in_list() -> None:
    """Миграция 14 зарегистрирована и схема доводится до неё."""
    assert 14 in MIGRATIONS


def test_schema_version_is_14(conn) -> None:
    """Версия схемы — 14."""
    assert get_schema_version(conn) == 14
    assert max(MIGRATIONS) == 14


def test_new_columns_exist(conn) -> None:
    """Новые колонки появились в обеих таблицах."""
    poll_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(attendance_polls)")}
    group_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(study_groups)")}

    assert {"mode", "poll_type"} <= poll_cols
    assert "attendance_mode" in group_cols


def test_existing_rows_get_defaults(conn) -> None:
    """Старые записи получают 'chat' и 'self' — старая логика не ломается."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance_polls (group_name, date_iso, para,"
            " chat_id, started_at, closes_at) VALUES (?, '2026-09-01', 1,"
            " ?, 'x', 'x')",
            (GROUP, CHAT_ID),
        )
    row = conn.execute(
        "SELECT mode, poll_type FROM attendance_polls"
        " WHERE date_iso = '2026-09-01'"
    ).fetchone()

    assert row["mode"] == "chat"
    assert row["poll_type"] == "self"


def test_polls_not_recreated(conn) -> None:
    """Таблица опросов не пересоздана: данные на месте."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance_polls (group_name, date_iso, para,"
            " chat_id, started_at, closes_at) VALUES (?, '2026-09-02', 2,"
            " ?, 'x', 'x')", (GROUP, CHAT_ID),
        )
    # Повторное применение миграций (идемпотентность) данные не теряет.
    apply_migrations(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM attendance_polls"
    ).fetchone()[0]
    assert count == 1


# --- режим группы ---

def test_default_mode_is_chat(conn) -> None:
    """По умолчанию режим — чат группы."""
    assert att_db.get_attendance_mode(conn, GROUP) == "chat"


def test_set_mode_direct(conn) -> None:
    """Староста меняет режим на личку, значение сохраняется."""
    assert att_db.set_attendance_mode(conn, GROUP, "direct") is True
    assert att_db.get_attendance_mode(conn, GROUP) == "direct"


def test_set_mode_back_to_chat(conn) -> None:
    """Режим можно вернуть обратно."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    att_db.set_attendance_mode(conn, GROUP, "chat")
    assert att_db.get_attendance_mode(conn, GROUP) == "chat"


def test_set_mode_unknown_group(conn) -> None:
    """Для несуществующей группы изменение возвращает False."""
    assert att_db.set_attendance_mode(conn, "НетТакой", "direct") is False


def test_set_mode_rejects_garbage(conn) -> None:
    """Недопустимый режим — ValueError, а не тихая запись мусора."""
    with pytest.raises(ValueError):
        att_db.set_attendance_mode(conn, GROUP, "telepathy")


def test_mode_unknown_group_falls_back_to_chat(conn) -> None:
    """Для группы без записи режим — 'chat' (поведение до миграции)."""
    assert att_db.get_attendance_mode(conn, "НетТакой") == "chat"
# --- start_poll: окно и тип ---

async def test_chat_mode_sends_to_group_chat(conn) -> None:
    """Режим chat: одно сообщение в чат группы с двумя кнопками."""
    bot = FakeBot()

    result = await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    assert result["ok"] is True
    chat_msgs = [m for m in bot.sent if m.get("chat_id") == CHAT_ID]
    assert len(chat_msgs) == 1
    assert "Физика" in chat_msgs[0]["text"]
    buttons = [b.text for row in chat_msgs[0]["reply_markup"].inline_keyboard
               for b in row]
    assert buttons == ["✅ Я на паре", "❌ Меня нет"]


async def test_chat_mode_poll_row(conn) -> None:
    """Запись опроса: mode=chat, poll_type=check, message_id сохранён."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    poll = _poll(conn)
    assert poll["mode"] == "chat"
    assert poll["poll_type"] == "check"
    assert poll["message_id"] is not None


async def test_direct_mode_sends_to_every_student(conn) -> None:
    """Режим direct: сообщение каждому студенту в личку."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    bot = FakeBot()

    result = await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    assert result["ok"] is True
    recipients = sorted(m["chat_id"] for m in bot.sent
                        if m.get("chat_id") is not None)
    assert recipients == sorted([STAROSTA, S1, S2, S3]), "староста тоже студент"
    assert not any(m.get("chat_id") == CHAT_ID for m in bot.sent)


async def test_direct_mode_poll_row(conn) -> None:
    """Запись direct-опроса: mode=direct, message_id не нужен."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    poll = _poll(conn)
    assert poll["mode"] == "direct"
    assert poll["poll_type"] == "check"
    assert poll["message_id"] is None
    assert int(poll["chat_id"]) == 0, "у опроса нет одного адресата"


async def test_closes_at_is_start_plus_five(conn) -> None:
    """Окно опроса — 5 минут от начала пары (3 пара будни: 13:15)."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    closes = datetime.fromisoformat(_poll(conn)["closes_at"])
    assert closes == datetime(2026, 9, 30, 13, 20, tzinfo=KRASNOYARSK)
    assert closes - datetime(2026, 9, 30, 13, 15,
                             tzinfo=KRASNOYARSK) == timedelta(minutes=5)


async def test_closes_at_saturday(conn) -> None:
    """В субботу окно считается по субботним звонкам (3 пара с 12:50)."""
    saturday = date(2026, 10, 3)
    with transaction(conn):
        conn.execute(
            "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
            " subject, teacher, room, week_type, updated_at)"
            " VALUES (?, 6, 3, 'Физика', 'Т.Т.', '', '', 'x')", (GROUP,))
    bot = FakeBot()

    await svc.start_poll(conn, bot, GROUP, saturday, PARA, force=True)

    row = conn.execute(
        "SELECT closes_at FROM attendance_polls WHERE date_iso = ?",
        (saturday.isoformat(),),
    ).fetchone()
    assert datetime.fromisoformat(row["closes_at"]) == datetime(
        2026, 10, 3, 12, 55, tzinfo=KRASNOYARSK)


async def test_start_poll_no_lesson(conn) -> None:
    """Пары нет — опрос не создаётся."""
    bot = FakeBot()
    result = await svc.start_poll(conn, bot, GROUP, DAY, 1, force=True)

    assert result["ok"] is False
    assert result["error"] == "no_lesson"


async def test_start_poll_twice_refused(conn) -> None:
    """Второй опрос по той же паре не создаётся."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    second = await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    assert second["ok"] is False
    assert second["error"] == "already_exists"


async def test_direct_mode_without_students(conn) -> None:
    """В пустой группе direct-опрос не создаётся."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    with transaction(conn):
        conn.execute("DELETE FROM students")
    bot = FakeBot()

    result = await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    assert result["ok"] is False
    assert result["error"] == "no_students"
# --- авто-absent при закрытии ---

def _mark(conn, tg_id: int, status: str, method: str = "self") -> None:
    """Поставить отметку напрямую."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
            " full_name, status, marked_by, marked_at, method, subject)"
            " VALUES (?, ?, ?, ?, 'Студент И.И.', ?, ?, 'x', ?, ?)",
            (GROUP, DATE_ISO, PARA, tg_id, status, tg_id, method, SUBJECT),
        )


def test_mark_absent_for_silent(conn) -> None:
    """Не ответившие получают absent с method='auto'."""
    marked = svc.mark_absent_for_silent(conn, GROUP, DATE_ISO, PARA)

    assert len(marked) == 4, "староста и три студента"
    rows = conn.execute(
        "SELECT status, method, marked_by FROM attendance"
    ).fetchall()
    assert all(r["status"] == "absent" for r in rows)
    assert all(r["method"] == "auto" for r in rows)
    assert all(int(r["marked_by"]) == 0 for r in rows), "ставит бот"


def test_mark_absent_keeps_existing_marks(conn) -> None:
    """Уже отвечавших авто-отметка не трогает."""
    _mark(conn, S1, "present")
    _mark(conn, S2, "absent")

    svc.mark_absent_for_silent(conn, GROUP, DATE_ISO, PARA)

    rows = {int(r["tg_id"]): (r["status"], r["method"])
            for r in conn.execute(
                "SELECT tg_id, status, method FROM attendance")}
    assert rows[S1] == ("present", "self"), "ответ «да» сохранён"
    assert rows[S2] == ("absent", "self"), "ответ «нет» сохранён"


def test_mark_absent_idempotent(conn) -> None:
    """Повторный вызов ничего не добавляет."""
    svc.mark_absent_for_silent(conn, GROUP, DATE_ISO, PARA)
    count = conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]

    svc.mark_absent_for_silent(conn, GROUP, DATE_ISO, PARA)

    assert conn.execute(
        "SELECT COUNT(*) FROM attendance"
    ).fetchone()[0] == count


def test_mark_absent_saves_subject(conn) -> None:
    """Автоотметка сохраняет предмет пары (для аттестации)."""
    svc.mark_absent_for_silent(conn, GROUP, DATE_ISO, PARA)

    subjects = {r["subject"] for r in conn.execute(
        "SELECT subject FROM attendance")}
    assert subjects == {SUBJECT}
# --- финальные сообщения ---

async def test_finalize_chat_marks_silent_and_edits(conn) -> None:
    """Закрытие в режиме chat: авто-absent + редактирование сообщения."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    _mark(conn, S1, "present")
    bot.sent.clear()

    poll = {**_poll(conn), "is_closed": 1}
    ok = await svc.finalize_poll_message(conn, bot, poll)

    assert ok is True
    edits = [m for m in bot.sent if m["method"] == "EditMessageText"]
    assert edits
    assert "Опрос закрыт" in edits[0]["text"]
    assert "Были: 1" in edits[0]["text"]
    assert "Прогуляли: 3" in edits[0]["text"]
    assert "/my_attendance" in edits[0]["text"]

    rows = {int(r["tg_id"]): r["method"] for r in conn.execute(
        "SELECT tg_id, method FROM attendance")}
    assert rows[S2] == "auto" and rows[S3] == "auto"


async def test_finalize_direct_sends_to_each(conn) -> None:
    """Закрытие в режиме direct: итог каждому студенту в личку."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    _mark(conn, S1, "present")
    bot.sent.clear()

    poll = {**_poll(conn), "is_closed": 1}
    ok = await svc.finalize_poll_message(conn, bot, poll)

    assert ok is True
    recipients = sorted(m["chat_id"] for m in bot.sent
                        if m.get("chat_id") is not None)
    assert recipients == sorted([STAROSTA, S1, S2, S3])

    personal = next(m["text"] for m in bot.sent if m.get("chat_id") == S1)
    assert "✅ был" in personal
    silent = next(m["text"] for m in bot.sent if m.get("chat_id") == S2)
    assert "❌ прогулял" in silent


async def test_finalize_without_closing_marks_nobody(conn) -> None:
    """Перерисовка незакрытого опроса никого не наказывает."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)

    await svc.finalize_poll_message(conn, bot, _poll(conn))

    assert conn.execute(
        "SELECT COUNT(*) FROM attendance"
    ).fetchone()[0] == 0


async def test_finalize_idempotent_direct(conn) -> None:
    """Повторное закрытие не ставит дублей."""
    att_db.set_attendance_mode(conn, GROUP, "direct")
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    poll = {**_poll(conn), "is_closed": 1}

    await svc.finalize_poll_message(conn, bot, poll)
    count = conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
    await svc.finalize_poll_message(conn, bot, poll)

    assert conn.execute(
        "SELECT COUNT(*) FROM attendance"
    ).fetchone()[0] == count


async def test_finalize_legacy_self_poll_has_no_auto(conn) -> None:
    """Старый опрос «Я на паре» закрывается без автоотметок."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance_polls (group_name, date_iso, para,"
            " chat_id, message_id, started_at, closes_at, is_closed, mode,"
            " poll_type) VALUES (?, ?, ?, ?, 5, 'x', 'x', 1, 'chat', 'self')",
            (GROUP, DATE_ISO, PARA, CHAT_ID),
        )
    bot = FakeBot()

    await svc.finalize_poll_message(conn, bot, _poll(conn))

    assert conn.execute(
        "SELECT COUNT(*) FROM attendance"
    ).fetchone()[0] == 0, "старая механика прогулов не ставит"


async def test_tick_closes_check_poll(conn) -> None:
    """Фоновый проход закрывает опрос после срока."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    moment = datetime(2026, 9, 30, 13, 25, tzinfo=KRASNOYARSK)

    result = await svc.tick(conn, bot, moment)

    assert result["closed"] == 1
    assert _poll(conn)["is_closed"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance WHERE method = 'auto'"
    ).fetchone()[0] == 4, "не ответившие стали прогульщиками"


async def test_tick_keeps_poll_before_deadline(conn) -> None:
    """До истечения окна опрос остаётся открытым."""
    bot = FakeBot()
    await svc.start_poll(conn, bot, GROUP, DAY, PARA, force=True)
    moment = datetime(2026, 9, 30, 13, 17, tzinfo=KRASNOYARSK)

    result = await svc.tick(conn, bot, moment)

    assert result["closed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0


# --- экран режима ---

def test_mode_screen_labels() -> None:
    """Экран показывает текущий режим по-русски."""
    assert "Текущий: <b>Чат группы</b>" in atexts.attendance_mode_screen("chat")
    assert "Текущий: <b>Личка</b>" in atexts.attendance_mode_screen("direct")


def test_mode_change_notices() -> None:
    """Подтверждения смены режима различаются."""
    assert "Личка" in atexts.MODE_CHANGED_DIRECT
    assert "Чат группы" in atexts.MODE_CHANGED_CHAT
    assert "Со следующей пары" in atexts.MODE_CHANGED_DIRECT


def test_check_poll_kb_callbacks() -> None:
    """Callback-данные кнопок «Да/Нет»."""
    from bot.attendance import attendance_keyboards as att_kb

    kb = att_kb.check_poll_kb(DATE_ISO, PARA)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == [f"att:check:{DATE_ISO}:{PARA}:yes",
                    f"att:check:{DATE_ISO}:{PARA}:no"]


def test_mode_kb_marks_current() -> None:
    """Кнопка текущего режима помечена галочкой."""
    from bot.attendance import attendance_keyboards as att_kb

    labels_direct = [b.text for row in att_kb.attendance_mode_kb("direct").inline_keyboard
                     for b in row]
    assert any(label.startswith("📱 Личка ✅") for label in labels_direct)
    assert not any(label.startswith("💬 Чат группы ✅") for label in labels_direct)

    labels_chat = [b.text for row in att_kb.attendance_mode_kb("chat").inline_keyboard
                   for b in row]
    assert any(label.startswith("💬 Чат группы ✅") for label in labels_chat)