"""Миграции схемы базы данных.

Текущая версия схемы хранится в таблице ``schema_version`` (ровно одна
строка). :func:`apply_migrations` читает текущую версию и последовательно
применяет недостающие миграции из ``MIGRATIONS``; каждая миграция
выполняется в отдельной транзакции — либо применяется целиком, либо не
применяется вовсе.

Добавление новой миграции (шаг N → N+1)::

    def migrate_2_add_smth(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE ...")

    MIGRATIONS = {1: migrate_1_initial, 2: migrate_2_add_smth}
"""

import logging
import sqlite3
from collections.abc import Callable

from bot.db import transaction

logger = logging.getLogger(__name__)


class MigrationError(Exception):
    """Ошибка миграции: в MIGRATIONS пропущен номер версии."""


def migrate_1_initial(conn: sqlite3.Connection) -> None:
    """Миграция 0 → 1: исходная схема из ТЗ (таблицы и индексы).

    Соглашения по типам:
    - даты/моменты времени — TEXT в ISO-8601 (``YYYY-MM-DD`` для дат,
      полный формат с offset ``+07:00`` для моментов, пояс Asia/Krasnoyarsk);
    - булевы значения — INTEGER 0/1;
    - ``week_type`` — 'even' | 'odd' | 'all' (пара идёт каждую неделю);
    - ``day_of_week`` — 1..7, понедельник = 1 (ISO);
    - ``deadlines.deleted_at`` — NULL означает активную запись (soft delete).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id      INTEGER PRIMARY KEY,
            group_name TEXT    NOT NULL,
            full_name  TEXT    NOT NULL DEFAULT '',
            is_active  INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deadlines (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id         INTEGER NOT NULL
                          REFERENCES users (tg_id) ON DELETE CASCADE,
            subject       TEXT    NOT NULL,
            teacher       TEXT    NOT NULL DEFAULT '',
            task          TEXT    NOT NULL DEFAULT '',
            deadline_date TEXT    NOT NULL,
            deleted_at    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name  TEXT    NOT NULL,
            day_of_week INTEGER NOT NULL,
            para_number INTEGER NOT NULL,
            subject     TEXT    NOT NULL,
            teacher     TEXT    NOT NULL DEFAULT '',
            room        TEXT    NOT NULL DEFAULT '',
            week_type   TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS substitutions_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name   TEXT    NOT NULL,
            date_iso     TEXT    NOT NULL,
            para         INTEGER NOT NULL,
            old_subject  TEXT    NOT NULL DEFAULT '',
            new_subject  TEXT    NOT NULL DEFAULT '',
            teacher      TEXT    NOT NULL DEFAULT '',
            room         TEXT    NOT NULL DEFAULT '',
            is_cancelled INTEGER NOT NULL DEFAULT 0,
            fetched_at   TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            group_name  TEXT NOT NULL,
            signature   TEXT NOT NULL,
            notify_date TEXT NOT NULL,
            sent_at     TEXT NOT NULL,
            PRIMARY KEY (group_name, signature, notify_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    _create_indexes(conn)


def _create_indexes(conn: sqlite3.Connection) -> None:
    """Индексы исходной схемы (часть migrate_1_initial)."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_group_name ON users (group_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deadlines_tg_id_deadline_date"
        " ON deadlines (tg_id, deadline_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_cache_group_day_para"
        " ON schedule_cache (group_name, day_of_week, para_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_substitutions_cache_group_date"
        " ON substitutions_cache (group_name, date_iso)"
    )


def migrate_2_add_self_study(conn: sqlite3.Connection) -> None:
    """Миграция 1 → 2: колонка ``is_self_study`` в ``substitutions_cache``.

    Парсер листа замен (шаг 4) возвращает флаг «самостоятельная работа», но в
    исходной схеме колонки под него не было — флаг терялся при записи в БД.
    Существующие строки получают 0 (не самостоятельная работа).
    """
    conn.execute(
        "ALTER TABLE substitutions_cache"
        " ADD COLUMN is_self_study INTEGER NOT NULL DEFAULT 0"
    )


def migrate_3_deadline_date_nullable(conn: sqlite3.Connection) -> None:
    """Миграция 2 → 3: ``deadlines.deadline_date`` допускает NULL.

    Дедлайн может быть без конкретной даты (группа срочности «Без даты»,
    ``days_left`` = None), а исходная схема объявляла колонку NOT NULL.

    SQLite не умеет менять NOT NULL у существующей колонки, поэтому таблица
    пересоздаётся: создаём новую, переносим данные, удаляем старую,
    переименовываем. Существующие строки (пустая строка вместо даты также
    трактуется как «без даты») сохраняются.
    """
    conn.execute("""
        CREATE TABLE deadlines_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id         INTEGER NOT NULL
                          REFERENCES users (tg_id) ON DELETE CASCADE,
            subject       TEXT    NOT NULL,
            teacher       TEXT    NOT NULL DEFAULT '',
            task          TEXT    NOT NULL DEFAULT '',
            deadline_date TEXT,
            deleted_at    TEXT
        )
    """)
    conn.execute("""
        INSERT INTO deadlines_new
            (id, tg_id, subject, teacher, task, deadline_date, deleted_at)
        SELECT id, tg_id, subject, teacher, task,
               NULLIF(deadline_date, ''), deleted_at
        FROM deadlines
    """)
    conn.execute("DROP TABLE deadlines")
    conn.execute("ALTER TABLE deadlines_new RENAME TO deadlines")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deadlines_tg_id_deadline_date"
        " ON deadlines (tg_id, deadline_date)"
    )


def migrate_4_calendar_tokens(conn: sqlite3.Connection) -> None:
    """Миграция 3 → 4: таблица ``calendar_tokens`` для .ics-подписки.

    Токен в ссылке ``/calendar/{token}.ics`` — единственный идентификатор
    пользователя для внешнего HTTP-клиента (Telegram id в ссылку не попадёт).
    Один пользователь — один токен: перевыпуск не нужен, а при утечке
    достаточно удалить строку.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_tokens (
            tg_id      INTEGER PRIMARY KEY
                       REFERENCES users (tg_id) ON DELETE CASCADE,
            token      TEXT    NOT NULL UNIQUE,
            created_at TEXT    NOT NULL
        )
    """)
    # UNIQUE уже создаёт индекс, но явный не помешает (и читается однозначно).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_tokens_token"
        " ON calendar_tokens (token)"
    )


def migrate_5_notifications_enabled(conn: sqlite3.Connection) -> None:
    """Миграция 4 → 5: флаг ``users.notifications_enabled``.

    Пользователь может отключить уведомления о заменах в /settings.
    По умолчанию 1 (включены), поэтому существующие записи ничего не теряют.
    """
    conn.execute(
        "ALTER TABLE users"
        " ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: migrate_1_initial,
    2: migrate_2_add_self_study,
    3: migrate_3_deadline_date_nullable,
    4: migrate_4_calendar_tokens,
    5: migrate_5_notifications_enabled,
}


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Текущая версия схемы; 0 — если таблица версий ещё не создана."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Применить все недостающие миграции (идемпотентно).

    Args:
        conn: соединение, полученное через :func:`bot.db.get_connection`.

    Returns:
        Итоговая версия схемы.

    Raises:
        MigrationError: если в нумерации MIGRATIONS есть пропуск.
    """
    _ensure_schema_version_table(conn)
    current = get_schema_version(conn)
    target = max(MIGRATIONS, default=0)

    for version in range(current + 1, target + 1):
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise MigrationError(
                f"Миграция {version - 1} → {version} не найдена в MIGRATIONS"
            )
        logger.info(
            "applying migration",
            extra={"from_version": version - 1, "to_version": version},
        )
        with transaction(conn):
            migration(conn)
            conn.execute("UPDATE schema_version SET version = ?", (version,))

    if target > current:
        logger.info(
            "migrations applied",
            extra={"from_version": current, "to_version": target},
        )
    return get_schema_version(conn)


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """Создать таблицу версий, если её ещё нет (начальная версия — 0)."""
    with transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        if count == 0:
            conn.execute("INSERT INTO schema_version (version) VALUES (0)")

