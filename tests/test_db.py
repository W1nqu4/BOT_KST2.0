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
}
EXPECTED_INDEXES = {
    "idx_users_group_name",
    "idx_deadlines_tg_id_deadline_date",
    "idx_schedule_cache_group_day_para",
    "idx_substitutions_cache_group_date",
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
