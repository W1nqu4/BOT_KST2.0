"""Тесты слоя БД: bot.db + bot.migrations (in-memory SQLite)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import MIGRATIONS, apply_migrations, get_schema_version

EXPECTED_TABLES = {
    "schema_version",
    "users",
    "deadlines",
    "schedule_cache",
    "substitutions_cache",
    "sent_notifications",
    "meta",
    "calendar_tokens",
    "group_chats",
}
EXPECTED_INDEXES = {
    "idx_users_group_name",
    "idx_deadlines_tg_id_deadline_date",
    "idx_schedule_cache_group_day_para",
    "idx_substitutions_cache_group_date",
    "idx_group_chats_group",
}


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """In-memory соединение (без миграций); закрывается после теста."""
    c = get_connection(":memory:")
    yield c
    c.close()


def _insert_user(
    conn: sqlite3.Connection, tg_id: int = 123456, group_name: str = "26КАД"
) -> None:
    """Вставить тестового пользователя."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, full_name, is_active, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (tg_id, group_name, "Иван Иванов", 1, "2026-09-26T10:00:00+07:00"),
        )


def test_apply_migrations_creates_all_tables(conn: sqlite3.Connection) -> None:
    version = apply_migrations(conn)
    assert version == max(MIGRATIONS)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    assert EXPECTED_TABLES <= {r["name"] for r in rows}


def test_apply_migrations_creates_all_indexes(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    assert EXPECTED_INDEXES <= {r["name"] for r in rows}


def test_apply_migrations_idempotent(conn: sqlite3.Connection) -> None:
    first = apply_migrations(conn)
    second = apply_migrations(conn)
    third = apply_migrations(conn)
    assert first == second == third == max(MIGRATIONS)
    assert get_schema_version(conn) == max(MIGRATIONS)
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1


def test_mark_group_chat_full_sent(conn: sqlite3.Connection) -> None:
    """Отметка о полном расписании сохраняется и обновляется."""
    from bot import db

    apply_migrations(conn)
    db.add_group_chat(conn, -100500, "КСТ", "supergroup", "26КАД", 1)
    assert db.get_group_chat(conn, -100500)["last_full_schedule_sent_date"] is None

    assert db.mark_group_chat_full_sent(conn, -100500, "2026-09-29") is True
    assert db.get_group_chat(
        conn, -100500
    )["last_full_schedule_sent_date"] == "2026-09-29"

    # Повторная отметка на другую дату перезаписывает.
    db.mark_group_chat_full_sent(conn, -100500, "2026-09-30")
    assert db.get_group_chat(
        conn, -100500
    )["last_full_schedule_sent_date"] == "2026-09-30"

    # Неизвестный чат — False, без исключения.
    assert db.mark_group_chat_full_sent(conn, 424242, "2026-09-30") is False


def test_group_chat_helpers(conn: sqlite3.Connection) -> None:
    """CRUD привязок: добавление, перезапись, чтение, удаление."""
    from bot import db

    apply_migrations(conn)

    db.add_group_chat(conn, -100500, "КСТ 26КАД", "supergroup", "26КАД", 1)
    db.add_group_chat(conn, -100600, "КСТ 26КАД", "group", "26КАД", 2)
    db.add_group_chat(conn, -100700, "КСТ 26МЭГ", "channel", "26МЭГ", 3)

    assert len(db.get_all_group_chats(conn)) == 3
    assert len(db.get_group_chats_for_group(conn, "26КАД")) == 2
    assert len(db.get_group_chats_for_group(conn, "26МЭГ")) == 1
    assert sorted(db.get_all_notify_groups(conn)) == ["26КАД", "26МЭГ"]

    # Один chat_id — одна группа: повторный вызов перезаписывает.
    db.add_group_chat(conn, -100500, "КСТ 26МЭГ", "supergroup", "26МЭГ", 4)
    assert db.get_group_chat(conn, -100500)["group_name"] == "26МЭГ"
    assert len(db.get_all_group_chats(conn)) == 3
    # 26КАД остался только в чате -100600.
    assert len(db.get_group_chats_for_group(conn, "26КАД")) == 1

    # Выключенные уведомления исключают чат из рассылки.
    with transaction(conn):
        conn.execute(
            "UPDATE group_chats SET notifications_enabled = 0 WHERE chat_id = ?",
            (-100600,),
        )
    assert db.get_group_chats_for_group(conn, "26КАД") == []

    assert db.remove_group_chat(conn, -100500) is True
    assert db.get_group_chat(conn, -100500) is None
    assert db.remove_group_chat(conn, -100500) is False


# --- история замен (шаг 3) ---

SUB_HIST_ROW = {
    "para": 2, "old_subject": "ОД.03 История",
    "new_subject": "ОД.07 Математика",
    "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
    "is_cancelled": False, "is_self_study": False,
}


def _add_user_row(conn: sqlite3.Connection, tg_id: int,
                  group_name: str = "26КАД") -> None:
    """Вставить пользователя (для get_known_groups)."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, ?, 'x')",
            (tg_id, group_name),
        )


def test_save_substitution_history_inserts(conn: sqlite3.Connection) -> None:
    """Первая запись создаётся, first_seen_at == last_seen_at."""
    from bot import db

    apply_migrations(conn)
    saved = db.save_substitution_history(
        conn, "26КАД", "2026-09-28", [SUB_HIST_ROW],
        now="2026-09-28T15:00:00+07:00",
    )

    assert saved == 1
    assert db.count_substitution_history(conn) == 1

    row = conn.execute("SELECT * FROM substitution_history").fetchone()
    assert row["group_name"] == "26КАД"
    assert row["date_iso"] == "2026-09-28"
    assert row["para"] == 2
    assert row["new_subject"] == "ОД.07 Математика"
    assert row["teacher"] == "Кудрявцева Полина Алексеевна"
    assert row["is_cancelled"] == 0
    assert row["is_self_study"] == 0
    assert row["first_seen_at"] == "2026-09-28T15:00:00+07:00"
    assert row["last_seen_at"] == "2026-09-28T15:00:00+07:00"


def test_save_substitution_history_updates_not_duplicates(
        conn: sqlite3.Connection) -> None:
    """Повторная встреча той же пары обновляет last_seen_at, не дублируя."""
    from bot import db

    apply_migrations(conn)
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW],
                                 now="2026-09-28T15:00:00+07:00")
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW],
                                 now="2026-09-28T16:30:00+07:00")

    assert db.count_substitution_history(conn) == 1, "дубль появился"
    row = conn.execute("SELECT * FROM substitution_history").fetchone()
    assert row["first_seen_at"] == "2026-09-28T15:00:00+07:00", \
        "первое появление не должно перезаписываться"
    assert row["last_seen_at"] == "2026-09-28T16:30:00+07:00"


def test_save_substitution_history_updates_changed_fields(
        conn: sqlite3.Connection) -> None:
    """Изменённая замена (другой кабинет) обновляет поля записи."""
    from bot import db

    apply_migrations(conn)
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW],
                                 now="2026-09-28T15:00:00+07:00")
    changed = dict(SUB_HIST_ROW, room="999", is_cancelled=True)
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [changed],
                                 now="2026-09-28T17:00:00+07:00")

    assert db.count_substitution_history(conn) == 1
    row = conn.execute("SELECT * FROM substitution_history").fetchone()
    assert row["room"] == "999"
    assert row["is_cancelled"] == 1


def test_save_substitution_history_different_para_and_date(
        conn: sqlite3.Connection) -> None:
    """Разные пары и даты — разные записи."""
    from bot import db

    apply_migrations(conn)
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW])
    db.save_substitution_history(conn, "26КАД", "2026-09-28",
                                 [dict(SUB_HIST_ROW, para=3)])
    db.save_substitution_history(conn, "26КАД", "2026-09-29", [SUB_HIST_ROW])

    assert db.count_substitution_history(conn) == 3


def test_save_substitution_history_empty_list(conn: sqlite3.Connection) -> None:
    """Пустой список — ничего не пишем."""
    from bot import db

    apply_migrations(conn)
    assert db.save_substitution_history(conn, "26КАД", "2026-09-28", []) == 0
    assert db.count_substitution_history(conn) == 0


def test_get_known_groups_only_users(conn: sqlite3.Connection) -> None:
    """get_known_groups возвращает только группы из users, без пустых."""
    from bot import db

    apply_migrations(conn)
    _add_user_row(conn, 111, "26КАД")
    _add_user_row(conn, 222, "26КАД")
    _add_user_row(conn, 333, "25КАД")
    _add_user_row(conn, 444, "")
    _add_user_row(conn, 555, "   ")

    assert db.get_known_groups(conn) == ["25КАД", "26КАД"]


def test_get_known_groups_empty(conn: sqlite3.Connection) -> None:
    """Без пользователей список пуст."""
    from bot import db

    apply_migrations(conn)
    assert db.get_known_groups(conn) == []


def test_cleanup_substitution_history_removes_old(
        conn: sqlite3.Connection) -> None:
    """Очистка удаляет записи раньше границы и возвращает их количество."""
    from bot import db

    apply_migrations(conn)
    db.save_substitution_history(conn, "26КАД", "2025-09-01", [SUB_HIST_ROW])
    db.save_substitution_history(conn, "26КАД", "2025-12-31", [SUB_HIST_ROW])
    db.save_substitution_history(conn, "26КАД", "2026-06-29",
                                 [dict(SUB_HIST_ROW, para=3)])
    db.save_substitution_history(conn, "26КАД", "2026-09-28",
                                 [dict(SUB_HIST_ROW, para=4)])

    removed = db.cleanup_substitution_history(conn, "2026-06-30")

    assert removed == 3
    assert db.count_substitution_history(conn) == 1
    assert db.earliest_substitution_history_date(conn) == "2026-09-28"


def test_cleanup_substitution_history_nothing_to_remove(
        conn: sqlite3.Connection) -> None:
    """Если старых записей нет — 0."""
    from bot import db

    apply_migrations(conn)
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW])
    assert db.cleanup_substitution_history(conn, "2026-06-30") == 0
    assert db.count_substitution_history(conn) == 1


def test_history_count_and_earliest(conn: sqlite3.Connection) -> None:
    """count и earliest возвращают корректные значения; пусто → None."""
    from bot import db

    apply_migrations(conn)
    assert db.count_substitution_history(conn) == 0
    assert db.earliest_substitution_history_date(conn) is None

    db.save_substitution_history(conn, "26КАД", "2026-09-29",
                                 [dict(SUB_HIST_ROW, para=3)])
    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW])

    assert db.count_substitution_history(conn) == 2
    assert db.earliest_substitution_history_date(conn) == "2026-09-28"


def test_pinned_message_helpers(conn: sqlite3.Connection) -> None:
    """set/get/clear отметки о закреплении расписания (шаг 4)."""
    from bot import db

    apply_migrations(conn)
    _add_user_row(conn, 111, "26КАД")
    db.add_group_chat(conn, -100500, "КСТ", "supergroup", "26КАД", 1)

    assert db.get_pinned_message(conn, -100500) is None

    assert db.set_pinned_message(conn, -100500, 555, "2026-09-28") is True
    pinned = db.get_pinned_message(conn, -100500)
    assert pinned["pinned_message_id"] == 555
    assert pinned["pinned_date_iso"] == "2026-09-28"

    assert db.clear_pinned_message(conn, -100500) is True
    assert db.get_pinned_message(conn, -100500) is None

    # Неизвестный чат — False, без исключения.
    assert db.set_pinned_message(conn, 424242, 1, "2026-09-28") is False
    assert db.clear_pinned_message(conn, 424242) is False


def test_save_substitution_history_keeps_cancelled(
        conn: sqlite3.Connection) -> None:
    """Отмена (is_cancelled=1, пустой new_subject) сохраняется в историю."""
    from bot import db

    apply_migrations(conn)
    cancelled = dict(SUB_HIST_ROW, para=1, old_subject="", new_subject="",
                     teacher="", room="", is_cancelled=True)

    saved = db.save_substitution_history(conn, "26КАД", "2026-09-29",
                                         [cancelled])

    assert saved == 1
    assert db.count_substitution_history(conn) == 1
    row = conn.execute("SELECT * FROM substitution_history").fetchone()
    assert row["is_cancelled"] == 1
    assert row["new_subject"] == ""
    assert row["old_subject"] == ""


def test_save_substitution_history_keeps_self_study(
        conn: sqlite3.Connection) -> None:
    """Самостоятельная работа тоже сохраняется."""
    from bot import db

    apply_migrations(conn)
    self_study = dict(SUB_HIST_ROW, is_self_study=True)

    db.save_substitution_history(conn, "26КАД", "2026-09-29", [self_study])

    row = conn.execute("SELECT is_self_study FROM substitution_history").fetchone()
    assert row["is_self_study"] == 1


def test_get_substitution_history_dates(conn: sqlite3.Connection) -> None:
    """get_substitution_history_dates возвращает уникальные даты по порядку."""
    from bot import db

    apply_migrations(conn)
    assert db.get_substitution_history_dates(conn) == []

    db.save_substitution_history(conn, "26КАД", "2026-09-29", [SUB_HIST_ROW])
    db.save_substitution_history(conn, "26КАД", "2026-09-28",
                                 [dict(SUB_HIST_ROW, para=3)])
    db.save_substitution_history(conn, "25КАД", "2026-09-29",
                                 [dict(SUB_HIST_ROW, para=4)])

    assert db.get_substitution_history_dates(conn) == [
        "2026-09-28", "2026-09-29"
    ]
    assert db.count_substitution_history_dates(conn) == 2
    assert db.count_substitution_history(conn) == 3


def test_latest_substitution_history_date(conn: sqlite3.Connection) -> None:
    """latest вернёт самую позднюю дату, earliest — самую раннюю."""
    from bot import db

    apply_migrations(conn)
    assert db.latest_substitution_history_date(conn) is None

    db.save_substitution_history(conn, "26КАД", "2026-09-28", [SUB_HIST_ROW])
    db.save_substitution_history(conn, "26КАД", "2026-10-05",
                                 [dict(SUB_HIST_ROW, para=3)])

    assert db.earliest_substitution_history_date(conn) == "2026-09-28"
    assert db.latest_substitution_history_date(conn) == "2026-10-05"


def test_insert_and_read_user(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    _insert_user(conn)
    row = conn.execute(
        "SELECT * FROM users WHERE tg_id = ?", (123456,)
    ).fetchone()
    assert row is not None
    assert row["group_name"] == "26КАД"
    assert row["full_name"] == "Иван Иванов"
    assert row["is_active"] == 1
    assert row["created_at"] == "2026-09-26T10:00:00+07:00"


def test_insert_and_read_deadline(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    _insert_user(conn)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO deadlines"
            " (tg_id, subject, teacher, task, deadline_date)"
            " VALUES (?, ?, ?, ?, ?)",
            (123456, "МДК.01.01", "Петрова А.И.", "Чертеж к 10.10", "2026-10-10"),
        )
        deadline_id = cur.lastrowid
    row = conn.execute(
        "SELECT * FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row is not None
    assert row["subject"] == "МДК.01.01"
    assert row["teacher"] == "Петрова А.И."
    assert row["deadline_date"] == "2026-10-10"
    assert row["deleted_at"] is None


def test_deadline_requires_existing_user(conn: sqlite3.Connection) -> None:
    """foreign_keys=ON: дедлайн без пользователя не вставляется."""
    apply_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO deadlines (tg_id, subject, deadline_date)"
                " VALUES (?, ?, ?)",
                (999, "Предмет", "2026-10-10"),
            )


def test_transaction_rollback_on_error(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO users (tg_id, group_name, created_at)"
                " VALUES (?, ?, ?)",
                (1, "26КАД", "2026-09-26T10:00:00+07:00"),
            )
            raise RuntimeError("boom")
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


def test_transaction_path_mode_persists_and_closes(tmp_path: Path) -> None:
    """transaction(path) сам открывает/закрывает соединение, данные на диске."""
    db_file = tmp_path / "test.db"
    with transaction(db_file) as c:
        apply_migrations(c)
        _insert_user(c)

    # Соединение закрыто контекстным менеджером.
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")

    fresh = get_connection(db_file)
    try:
        assert get_schema_version(fresh) == max(MIGRATIONS)
        count = fresh.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert count == 1
    finally:
        fresh.close()
