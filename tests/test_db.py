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
