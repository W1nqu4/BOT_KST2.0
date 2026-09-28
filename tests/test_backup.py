"""Тесты резервного копирования БД (шаг 12).

Главное, что проверяется: копия делается через SQLite backup API и
**открывается как рабочая БД** — при копировании файла в режиме WAL
часть данных осталась бы в ``-wal`` и копия была бы неполной.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import MIGRATIONS, apply_migrations
from bot.services import backup_service as bs


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """БД с миграциями и данными в режиме WAL."""
    path = tmp_path / "bot.db"
    conn = get_connection(path)
    apply_migrations(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (7, '26КАД', 'x')"
        )
    # Соединение НЕ закрываем: WAL-файл остаётся, как в работающем боте.
    yield path
    conn.close()


# --- make_backup ---

def test_backup_creates_file(db_path: Path) -> None:
    """Копия создаётся с именем bot_YYYYMMDD.db."""
    moment = datetime(2026, 9, 27, 12, 0)
    path = bs.make_backup(str(db_path), moment)

    assert path is not None
    assert path.name == "bot_20260927.db"
    assert path.exists()
    assert path.stat().st_size > 0


def test_backup_is_valid_sqlite_copy(db_path: Path) -> None:
    """Копия открывается и содержит те же данные (проверка WAL)."""
    path = bs.make_backup(str(db_path))
    assert path is not None

    copy_conn = get_connection(path)
    try:
        users = copy_conn.execute("SELECT tg_id, group_name FROM users").fetchall()
        assert len(users) == 1
        assert users[0]["group_name"] == "26КАД"
        # Схема тоже скопирована.
        version = copy_conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert version == max(MIGRATIONS)
    finally:
        copy_conn.close()


def test_backup_missing_db_returns_none(tmp_path: Path) -> None:
    assert bs.make_backup(str(tmp_path / "нет.db")) is None


def test_backup_dir_next_to_db(db_path: Path) -> None:
    """Каталог бэкапов лежит рядом с БД, в подпапке backups."""
    directory = bs.backup_dir_for(str(db_path))
    assert directory.name == "backups"
    assert directory.parent == db_path.parent


# --- ротация ---

def test_cleanup_removes_old_backups(tmp_path: Path) -> None:
    """Файлы старше 7 дней удаляются, свежие остаются."""
    directory = tmp_path / "backups"
    directory.mkdir()
    moment = datetime(2026, 9, 27)

    fresh = directory / "bot_20260926.db"
    fresh.write_text("fresh")
    boundary = directory / "bot_20260921.db"     # ровно 6 дней назад
    boundary.write_text("boundary")
    old = directory / "bot_20260901.db"
    old.write_text("old")

    removed = bs.cleanup_old_backups(directory, keep_days=7, moment=moment)

    assert removed == ["bot_20260901.db"]
    assert fresh.exists()
    assert boundary.exists()
    assert not old.exists()


def test_cleanup_ignores_foreign_files(tmp_path: Path) -> None:
    """Файлы с другими именами не трогаем."""
    directory = tmp_path / "backups"
    directory.mkdir()
    other = directory / "важное.txt"
    other.write_text("не бэкап")

    removed = bs.cleanup_old_backups(directory, moment=datetime(2026, 9, 27))

    assert removed == []
    assert other.exists()


def test_cleanup_missing_dir_is_safe(tmp_path: Path) -> None:
    assert bs.cleanup_old_backups(tmp_path / "нет") == []


# --- один проход ---

def test_backup_once_with_rotation(tmp_path: Path, db_path: Path) -> None:
    """backup_once делает копию и подчищает старое."""
    directory = bs.backup_dir_for(str(db_path))
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / "bot_20200101.db"
    stale.write_text("stale")

    result = bs.backup_once(str(db_path), keep_days=7,
                            moment=datetime(2026, 9, 27))

    assert result["created"] == "bot_20260927.db"
    assert result["removed"] == ["bot_20200101.db"]
    assert (directory / "bot_20260927.db").exists()


def test_backup_filename_format() -> None:
    assert bs.backup_name(datetime(2026, 9, 27)) == "bot_20260927.db"


# --- цикл ---

async def test_backup_loop_makes_backup_and_cancels(
    db_path: Path, monkeypatch
) -> None:
    """Цикл делает копию сразу при старте и корректно отменяется."""
    import asyncio

    from bot.main import Settings
    from bot.services import schedule_service as ss

    settings = Settings(
        bot_token="1:x", public_base_url="http://x", port=8080,
        db_path=str(db_path), admin_ids=(), admin_chat_id=None,
        cache_dir="data/cache", log_level="INFO",
    )

    async def stop_after_first(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(ss, "_sleep", stop_after_first)

    with pytest.raises(asyncio.CancelledError):
        await bs.backup_loop(object(), settings)

    created = list(bs.backup_dir_for(str(db_path)).glob("bot_*.db"))
    assert created, "первый бэкап должен быть сделан при старте"


async def test_backup_loop_logs_without_reserved_keys(
    db_path: Path, monkeypatch, caplog
) -> None:
    """Цикл логирует и не падает: extra без зарезервированных полей LogRecord.

    Регресс: в ``extra`` попадал ключ ``created`` (поле LogRecord) —
    логирование падало с KeyError, и цикл бэкапов ломался после первого
    успешного прохода.
    """
    import asyncio
    import logging as _logging

    from bot.main import Settings
    from bot.services import schedule_service as ss

    settings = Settings(
        bot_token="1:x", public_base_url="http://x", port=8080,
        db_path=str(db_path), admin_ids=(), admin_chat_id=None,
        cache_dir="data/cache", log_level="INFO",
    )

    calls: list[int] = []

    async def stop_after_first(seconds):
        calls.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(ss, "_sleep", stop_after_first)

    with caplog.at_level(_logging.INFO, logger="bot.services.backup_service"):
        with pytest.raises(asyncio.CancelledError):
            await bs.backup_loop(object(), settings)

    messages = [r.getMessage() for r in caplog.records]
    assert any("backup cycle finished" in m for m in messages), (
        f"запись о бэкапе должна попасть в лог, получено: {messages}"
    )


async def test_backup_loop_without_db_path_is_safe(monkeypatch) -> None:
    """Без db_path цикл не падает, а пишет предупреждение."""
    import asyncio

    from bot.main import Settings
    from bot.services import schedule_service as ss

    settings = Settings(
        bot_token="1:x", public_base_url="http://x", port=8080,
        db_path="", admin_ids=(), admin_chat_id=None,
        cache_dir="data/cache", log_level="INFO",
    )

    async def stop_after_first(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(ss, "_sleep", stop_after_first)

    with pytest.raises(asyncio.CancelledError):
        await bs.backup_loop(object(), settings)