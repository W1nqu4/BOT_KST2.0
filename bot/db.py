"""Слой работы с базой данных (SQLite).

Весь остальной код обращается к SQLite только через этот модуль (и через
``bot/migrations.py`` для схемы). Параметры подключения:

- ``isolation_level=None`` — явное управление транзакциями (BEGIN/COMMIT),
  благодаря чему ``transaction()`` корректно работает и с DDL, и с DML;
- ``row_factory=sqlite3.Row`` — доступ к полям строк по имени колонки;
- ``PRAGMA foreign_keys=ON`` — контроль внешних ключей;
- ``PRAGMA journal_mode=WAL`` — устойчивый к сбоям журнал (для ``:memory:``
  SQLite сам вернёт режим memory, это не ошибка).
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bot.config import DEFAULT_DB_PATH


def _resolve_db_path(db_path: str | Path | None) -> str | Path:
    """Путь к БД: явный аргумент → env DB_PATH → значение по умолчанию из config."""
    if db_path is not None and str(db_path).strip():
        return db_path
    return os.environ.get("DB_PATH", "").strip() or DEFAULT_DB_PATH


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Создать новое соединение с SQLite и включить нужные PRAGMA.

    Args:
        db_path: путь к файлу БД или ``":memory:"``. Если не задан — берётся
            env-переменная DB_PATH (или DEFAULT_DB_PATH из :mod:`bot.config`).

    Returns:
        ``sqlite3.Connection``. Закрытие — ответственность вызывающего кода
        (либо используйте :func:`transaction` в режиме пути).
    """
    path = _resolve_db_path(db_path)
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(
    target: sqlite3.Connection | str | Path | None = None,
) -> Iterator[sqlite3.Connection]:
    """Транзакция: при нормальном выходе — commit, при исключении — rollback.

    Два режима использования::

        with transaction(conn) as c:      # для уже открытого соединения:
            ...                           # commit/rollback, закрытие НЕ делаем

        with transaction("data/bot.db"):  # контекстный менеджер сам открывает
            ...                           # соединение и закрывает его на выходе
                                          # (без аргумента — env DB_PATH / дефолт)

    Вложенность поддерживается: если соединение уже находится в транзакции,
    внутренняя транзакция присоединяется к внешней, а commit/rollback делает
    внешняя.
    """
    if isinstance(target, sqlite3.Connection):
        conn, owns_connection = target, False
    else:
        conn, owns_connection = get_connection(target), True
    try:
        if conn.in_transaction:
            # Уже внутри внешней транзакции — просто работаем в ней.
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if owns_connection:
            conn.close()
def get_user(conn: sqlite3.Connection, tg_id: int) -> sqlite3.Row | None:
    """Пользователь по ``tg_id`` или None, если его ещё нет.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id пользователя.

    Returns:
        Строка таблицы ``users`` (поля по имени колонки) или None.
    """
    return conn.execute(
        "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
    ).fetchone()


def get_user_group(conn: sqlite3.Connection, tg_id: int) -> str | None:
    """Имя группы пользователя или None, если он ещё не зарегистрирован.

    Отсутствие пользователя и есть «группа не выбрана»: в схеме
    ``users.group_name`` объявлен NOT NULL, поэтому записи без группы нет.
    """
    row = get_user(conn, tg_id)
    if row is None:
        return None
    group = str(row["group_name"]).strip()
    return group or None


def upsert_user(conn: sqlite3.Connection, tg_id: int, group_name: str,
                full_name: str = "", now: str | None = None) -> None:
    """Создать пользователя или обновить его группу/имя.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.
        group_name: нормализованное имя группы.
        full_name: имя из Telegram (для админки).
        now: момент создания в ISO; по умолчанию берётся в поясе техникума.
    """
    from datetime import datetime

    from bot.config import TIMEZONE

    created = now or datetime.now(TIMEZONE).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users (tg_id, group_name, full_name, is_active, created_at)"
        " VALUES (?, ?, ?, 1, ?)"
        " ON CONFLICT(tg_id) DO UPDATE SET"
        "   group_name = excluded.group_name,"
        "   full_name = excluded.full_name,"
        "   is_active = 1",
        (tg_id, group_name, full_name, created),
    )


def list_available_groups(conn: sqlite3.Connection) -> list[str]:
    """Уникальные имена групп из кэша расписания (для проверки ввода).

    Returns:
        Отсортированный список групп; пустой, если кэш ещё не заполнен.
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM schedule_cache ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]


def count_users(conn: sqlite3.Connection) -> int:
    """Количество активных пользователей (для /stats на шаге 11)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]
    )


def get_notify_groups(conn: sqlite3.Connection) -> list[str]:
    """Группы, у которых есть хотя бы один активный пользователь.

    Нужны рассылке замен: группы без подписчиков обрабатывать незачем.

    Returns:
        Отсортированный список имён групп (пустые и NULL отброшены).
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM users"
        " WHERE is_active = 1 AND group_name IS NOT NULL AND group_name <> ''"
        " ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]


def get_users_by_group(conn: sqlite3.Connection, group_name: str) -> list[int]:
    """Активные пользователи группы (для рассылки).

    Returns:
        Список ``tg_id``.
    """
    rows = conn.execute(
        "SELECT tg_id FROM users WHERE group_name = ? AND is_active = 1",
        (group_name,),
    ).fetchall()
    return [int(row["tg_id"]) for row in rows]


def deactivate_user(conn: sqlite3.Connection, tg_id: int) -> None:
    """Пометить пользователя неактивным (заблокировал бота).

    Неактивные не попадают в рассылку и не считаются в статистике.
    """
    with transaction(conn):
        conn.execute("UPDATE users SET is_active = 0 WHERE tg_id = ?", (tg_id,))


def is_substitution_sent(conn: sqlite3.Connection, group_name: str,
                         signature: str, notify_date: str) -> bool:
    """Отправляли ли уже уведомление об этой замене в эту дату.

    Дедупликация спасает от повторов: рассылка ходит каждые 15 минут, а
    замену студент должен увидеть один раз.
    """
    row = conn.execute(
        "SELECT 1 FROM sent_notifications"
        " WHERE group_name = ? AND signature = ? AND notify_date = ?",
        (group_name, signature, notify_date),
    ).fetchone()
    return row is not None


def mark_substitution_sent(conn: sqlite3.Connection, group_name: str,
                           signature: str, notify_date: str,
                           sent_at: str | None = None) -> None:
    """Отметить уведомление о замене отправленным (идемпотентно)."""
    from datetime import datetime

    from bot.config import TIMEZONE

    moment = sent_at or datetime.now(TIMEZONE).isoformat(timespec="seconds")
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO sent_notifications"
            " (group_name, signature, notify_date, sent_at) VALUES (?, ?, ?, ?)",
            (group_name, signature, notify_date, moment),
        )


# ==========================================================================
# Статистика и модерация (шаг 11, для админки)
# ==========================================================================


def count_active_users_since(conn: sqlite3.Connection, days: int,
                             now: str | None = None) -> int:
    """Сколько активных пользователей появилось за последние ``days`` дней.

    Считаем по ``created_at``: «активность» здесь означает недавнюю
    регистрацию, а не последнее сообщение (истории действий мы не ведём).

    Args:
        conn: соединение SQLite.
        days: глубина в днях.
        now: текущий момент в ISO (для тестов).

    Returns:
        Количество пользователей.
    """
    from datetime import datetime, timedelta

    from bot.config import TIMEZONE

    moment = datetime.now(TIMEZONE) if now is None else datetime.fromisoformat(now)
    since = (moment - timedelta(days=days)).isoformat(timespec="seconds")
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1 AND created_at >= ?",
            (since,),
        ).fetchone()[0]
    )


def count_users_by_group(conn: sqlite3.Connection,
                         limit: int = 10) -> list[tuple[str, int]]:
    """Топ групп по числу активных пользователей.

    Returns:
        Список ``(группа, количество)``, отсортированный по убыванию.
    """
    rows = conn.execute(
        "SELECT group_name, COUNT(*) AS total FROM users"
        " WHERE is_active = 1 AND group_name <> ''"
        " GROUP BY group_name ORDER BY total DESC, group_name ASC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(row["group_name"]), int(row["total"])) for row in rows]


def list_users_in_group(conn: sqlite3.Connection, group_name: str,
                        limit: int = 50) -> list[dict]:
    """Активные пользователи группы (для ``/users``).

    Returns:
        Список словарей ``{'tg_id': ..., 'full_name': ...}`` и признак
        ``truncated`` отдельно — его считает вызывающий код через
        :func:`count_users_in_group`.
    """
    rows = conn.execute(
        "SELECT tg_id, full_name FROM users"
        " WHERE group_name = ? AND is_active = 1"
        " ORDER BY full_name, tg_id LIMIT ?",
        (group_name, limit),
    ).fetchall()
    return [{"tg_id": int(r["tg_id"]), "full_name": str(r["full_name"] or "")}
            for r in rows]


def count_users_in_group(conn: sqlite3.Connection, group_name: str) -> int:
    """Сколько активных пользователей в группе (для «и ещё N»)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM users WHERE group_name = ? AND is_active = 1",
            (group_name,),
        ).fetchone()[0]
    )


def iter_active_users(conn: sqlite3.Connection) -> list[dict]:
    """Все активные пользователи (для рассылки админа).

    Returns:
        Список словарей ``{'tg_id': ..., 'full_name': ..., 'group_name': ...}``.
    """
    rows = conn.execute(
        "SELECT tg_id, full_name, group_name FROM users"
        " WHERE is_active = 1 ORDER BY tg_id"
    ).fetchall()
    return [
        {
            "tg_id": int(r["tg_id"]),
            "full_name": str(r["full_name"] or ""),
            "group_name": str(r["group_name"] or ""),
        }
        for r in rows
    ]


def get_notifications_enabled(conn: sqlite3.Connection, tg_id: int) -> bool:
    """Включены ли у пользователя уведомления о заменах (по умолчанию — да)."""
    row = conn.execute(
        "SELECT notifications_enabled FROM users WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    if row is None:
        return False
    return bool(row["notifications_enabled"])


def set_notifications_enabled(conn: sqlite3.Connection, tg_id: int,
                              enabled: bool) -> bool:
    """Включить/выключить уведомления пользователя.

    Returns:
        True, если пользователь найден и обновлён.
    """
    if get_user(conn, tg_id) is None:
        return False
    with transaction(conn):
        conn.execute(
            "UPDATE users SET notifications_enabled = ? WHERE tg_id = ?",
            (1 if enabled else 0, tg_id),
        )
    return True


# ==========================================================================
# Чаты групп и каналов (шаг 2: /setup привязывает чат к группе КСТ)
# ==========================================================================


def add_group_chat(conn: sqlite3.Connection, chat_id: int,
                   chat_title: str, chat_type: str, group_name: str,
                   added_by: int, now: str | None = None) -> None:
    """Привязать чат к группе КСТ (повторный вызов перезаписывает).

    Один ``chat_id`` — одна группа: ``/setup 25КАД`` после ``/setup 26КАД``
    меняет привязку, а не создаёт вторую. ``notifications_enabled``
    сбрасывается в 1: админ только что настроил чат, уведомления нужны.

    Args:
        conn: соединение SQLite.
        chat_id: id чата/канала Telegram.
        chat_title: название чата (может быть пустым).
        chat_type: 'group' | 'supergroup' | 'channel'.
        group_name: нормализованное имя группы КСТ.
        added_by: tg_id того, кто привязал чат.
        now: момент в ISO (для тестов); иначе берётся пояс техникума.
    """
    from datetime import datetime

    from bot.config import TIMEZONE

    added_at = now or datetime.now(TIMEZONE).isoformat(timespec="seconds")
    with transaction(conn):
        conn.execute(
            "INSERT INTO group_chats"
            " (chat_id, chat_title, chat_type, group_name, added_by,"
            "  added_at, notifications_enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            "   chat_title = excluded.chat_title,"
            "   chat_type = excluded.chat_type,"
            "   group_name = excluded.group_name,"
            "   added_by = excluded.added_by,"
            "   added_at = excluded.added_at",
            (chat_id, chat_title or "", chat_type, group_name, added_by,
             added_at),
        )


def remove_group_chat(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Отвязать чат (``/unsync`` или бота выгнали из чата).

    Returns:
        True, если запись была и её удалили.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM group_chats WHERE chat_id = ?", (chat_id,)
        )
    return cursor.rowcount > 0


def get_group_chat(conn: sqlite3.Connection, chat_id: int) -> dict | None:
    """Привязка чата или None, если чат не настроен."""
    row = conn.execute(
        "SELECT * FROM group_chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_all_group_chats(conn: sqlite3.Connection) -> list[dict]:
    """Все привязанные чаты (для админки и диагностики)."""
    rows = conn.execute(
        "SELECT * FROM group_chats ORDER BY added_at, chat_id"
    ).fetchall()
    return [dict(row) for row in rows]


def mark_group_chat_full_sent(conn: sqlite3.Connection, chat_id: int,
                              date_iso: str) -> bool:
    """Отметить, что в чат отправлено полное расписание на дату.

    Нужно для дедупликации: рассылка ходит каждые 15 минут, а полное
    расписание на завтра в чат должно уйти один раз за дату.

    Args:
        conn: соединение SQLite.
        chat_id: id чата.
        date_iso: дата (``YYYY-MM-DD``), на которую отправлено расписание.

    Returns:
        True, если запись найдена и обновлена.
    """
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE group_chats SET last_full_schedule_sent_date = ?"
            " WHERE chat_id = ?",
            (date_iso, chat_id),
        )
    return cursor.rowcount > 0


def get_group_chats_for_group(conn: sqlite3.Connection,
                              group_name: str) -> list[dict]:
    """Чаты группы, которым нужно отправлять уведомления о заменах.

    Returns:
        Список словарей строк ``group_chats`` только с включёнными
        уведомлениями (``notifications_enabled = 1``).
    """
    rows = conn.execute(
        "SELECT * FROM group_chats"
        " WHERE group_name = ? AND notifications_enabled = 1"
        " ORDER BY chat_id",
        (group_name,),
    ).fetchall()
    return [dict(row) for row in rows]


# ==========================================================================
# История замен за учебный год (шаг 3: заготовка под будущую фичу)
# ==========================================================================


def save_substitution_history(conn: sqlite3.Connection, group_name: str,
                              date_iso: str, subs: list[dict],
                              now: str | None = None) -> int:
    """Записать замены группы в историю (в ДОПОЛНЕНИЕ к текущему листу).

    Ключ записи — ``(group_name, date_iso, para)``: лист замен может
    обновляться в течение дня (замены добавляют и правят), поэтому
    повторная встреча той же пары **не создаёт дубль**, а обновляет
    ``last_seen_at``. Поля при этом тоже обновляются: замена могла
    измениться (другой предмет, кабинет, преподаватель).

    ``first_seen_at`` пишется один раз — когда пара впервые появилась в листе.

    Args:
        conn: соединение SQLite.
        group_name: нормализованное имя группы.
        date_iso: дата замен (``YYYY-MM-DD``).
        subs: словари замен парсера (``para``, ``old_subject``,
            ``new_subject``, ``teacher``, ``room``, ``is_cancelled``,
            ``is_self_study``).
        now: момент в ISO (для тестов); иначе — пояс техникума.

    Returns:
        Количество обработанных записей.
    """
    if not subs:
        return 0

    from datetime import datetime

    from bot.config import TIMEZONE

    moment = now or datetime.now(TIMEZONE).isoformat(timespec="seconds")
    with transaction(conn):
        for sub in subs:
            conn.execute(
                "INSERT INTO substitution_history"
                " (group_name, date_iso, para, old_subject, new_subject,"
                "  teacher, room, is_cancelled, is_self_study,"
                "  first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(group_name, date_iso, para) DO UPDATE SET"
                "   old_subject = excluded.old_subject,"
                "   new_subject = excluded.new_subject,"
                "   teacher = excluded.teacher,"
                "   room = excluded.room,"
                "   is_cancelled = excluded.is_cancelled,"
                "   is_self_study = excluded.is_self_study,"
                "   last_seen_at = excluded.last_seen_at",
                (
                    group_name, date_iso, int(sub.get("para") or 0),
                    str(sub.get("old_subject") or ""),
                    str(sub.get("new_subject") or ""),
                    str(sub.get("teacher") or ""),
                    str(sub.get("room") or ""),
                    int(bool(sub.get("is_cancelled"))),
                    int(bool(sub.get("is_self_study"))),
                    moment, moment,
                ),
            )
    return len(subs)


def get_known_groups(conn: sqlite3.Connection) -> list[str]:
    """Группы, у которых есть хотя бы один зарегистрированный пользователь.

    История замен пишется только для них: иначе таблица наполнялась бы
    заменами всех 76 групп техникума, а нужны они лишь там, где есть
    кому их показывать.

    Returns:
        Отсортированный список уникальных непустых имён групп.
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM users"
        " WHERE group_name IS NOT NULL AND TRIM(group_name) != ''"
        " ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]


def cleanup_substitution_history(conn: sqlite3.Connection,
                                 until_date_iso: str) -> int:
    """Удалить историю замен раньше указанной даты (граница учебного года).

    Args:
        conn: соединение SQLite.
        until_date_iso: удаляются записи со ``date_iso < until_date_iso``.

    Returns:
        Количество удалённых записей.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM substitution_history WHERE date_iso < ?",
            (until_date_iso,),
        )
    return cursor.rowcount


def count_substitution_history(conn: sqlite3.Connection) -> int:
    """Сколько записей накопилось в истории замен."""
    return int(
        conn.execute("SELECT COUNT(*) FROM substitution_history").fetchone()[0]
    )


def earliest_substitution_history_date(conn: sqlite3.Connection) -> str | None:
    """Самая ранняя дата в истории замен или None, если история пуста."""
    row = conn.execute(
        "SELECT MIN(date_iso) AS earliest FROM substitution_history"
    ).fetchone()
    value = row["earliest"] if row is not None else None
    return str(value) if value else None


def get_all_notify_groups(conn: sqlite3.Connection) -> list[str]:
    """Группы, у которых есть получатели: личные подписчики ИЛИ чаты.

    Нужна рассылке замен: чат группы может быть привязан, даже если в боте
    нет ни одного личного подписчика этой группы, и наоборот.

    Returns:
        Отсортированный список уникальных имён групп.
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM ("
        "  SELECT group_name FROM users"
        "   WHERE is_active = 1 AND group_name IS NOT NULL AND group_name <> ''"
        "  UNION"
        "  SELECT group_name FROM group_chats"
        "   WHERE group_name IS NOT NULL AND group_name <> ''"
        "     AND notifications_enabled = 1"
        ") ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]
