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

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import DEFAULT_DB_PATH, TIMEZONE

logger = logging.getLogger(__name__)


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


def update_user_group_only(conn: sqlite3.Connection, tg_id: int,
                           group_name: str,
                           full_name: str | None = None) -> None:
    """Записать пользователю только группу (остальные поля не трогаем).

    Нужна для связки двух систем групп: посещаемость
    (``students.group_name``) при вступлении в группу подтягивает за собой
    группу для расписания (``users.group_name``), чтобы «📆 Расписание»
    работало сразу после ввода кода от старосты.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.
        group_name: нормализованное имя группы.
        full_name: необязательное ФИО из Telegram. Передаётся только там, где
            запись идёт из личного диалога (``/setup_schedule``): тогда
            поведение совпадает с :func:`upsert_user` — имя для админки
            обновляется, а пользователь снова считается активным. В связке
            групп ФИО не передаётся, и запись остаётся минимальной.

    Note:
        ``created_at`` в схеме объявлен ``NOT NULL`` без значения по
        умолчанию, поэтому при вставке новой строки момент регистрации
        подставляется здесь (как в :func:`upsert_user`).
    """
    from datetime import datetime

    from bot.config import TIMEZONE

    with transaction(conn):
        cursor = conn.execute(
            "UPDATE users SET group_name = ? WHERE tg_id = ?",
            (group_name, tg_id),
        )
        if cursor.rowcount == 0:
            created = datetime.now(TIMEZONE).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO users (tg_id, group_name, full_name, is_active,"
                " created_at) VALUES (?, ?, ?, 1, ?)",
                (tg_id, group_name, full_name or "", created),
            )
        elif full_name is not None:
            conn.execute(
                "UPDATE users SET full_name = ?, is_active = 1"
                " WHERE tg_id = ?",
                (full_name, tg_id),
            )

    # Синхронизация со связанным VK-аккаунтом: смена группы в Telegram обязана
    # быть видна и в VK, иначе у одного студента на платформах окажется разное
    # расписание. Делается здесь, а не в вызывающем коде: точек записи группы в
    # ``bot/`` шесть (регистрация, свободный ввод, расписание, вступление в
    # группу посещаемости), и любая забытая ломала бы связку.
    #
    # Рекурсии нет: :func:`upsert_vk_user` пишет в ``vk_users`` напрямую и
    # обратную синхронизацию не инициирует.
    linked_vk = get_vk_id_by_tg(conn, tg_id)
    if linked_vk is not None:
        upsert_vk_user(conn, linked_vk, group_name=group_name)
        logger.info(
            "group synced to linked vk",
            extra={"tg_id": tg_id, "vk_id": linked_vk, "group": group_name},
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


def split_teacher_cell(value: str) -> list[str]:
    """Разобрать ячейку преподавателей на отдельные ФИО.

    В расписании у подгрупп в одной ячейке стоит несколько человек через
    запятую («Ващенко Марина Юрьевна, Лоза Алена Станиславовна»), поэтому
    ячейка — не одно ФИО, а список.

    Args:
        value: содержимое колонки ``teacher``.

    Returns:
        Список ФИО без пустых значений (порядок сохраняется).
    """
    return [
        part.strip() for part in str(value or "").split(",") if part.strip()
    ]


def _normalize_person(text: str) -> str:
    """Ключ сравнения ФИО: нижний регистр и ё→е.

    SQLite не сравнивает кириллицу регистронезависимо (``LIKE '%Лунева%'``
    находит ноль строк, хотя в расписании «Лунёва»), поэтому поиск идёт в
    Python по этому ключу, а не через ``LIKE``.

    Args:
        text: ФИО или запрос.

    Returns:
        Нормализованная строка для сравнения.
    """
    return str(text or "").strip().lower().replace("ё", "е")


# Метка парсера «место не занято» (см. bot/parsers/schedule.py: VACANCY_RE).
# Это не человек, поэтому в роли преподавателя она не участвует: иначе
# «вакансия» получила бы доступ к десяткам групп сразу.
VACANCY_MARK = "вакансия"


def is_vacancy(name: str) -> bool:
    """Является ли значение меткой «вакансия», а не ФИО человека."""
    return _normalize_person(name) == VACANCY_MARK


def _all_teacher_names(conn: sqlite3.Connection) -> list[str]:
    """Все преподаватели: справочник плюс встречающиеся в расписании.

    Справочник нужен, чтобы фамилия находилась и тогда, когда пар у человека
    нет (тогда показываем «нет пар», а не «не нашёл»); кэш — чтобы находились
    те, кого в справочнике ещё нет.

    Returns:
        Отсортированный список полных ФИО без «вакансии» (это не человек).
    """
    from bot.parsers.teachers import TEACHERS

    names = set(TEACHERS.values())
    rows = conn.execute(
        "SELECT DISTINCT teacher FROM schedule_cache WHERE teacher <> ''"
    ).fetchall()
    for row in rows:
        names.update(split_teacher_cell(row["teacher"]))

    return sorted(name for name in names if not is_vacancy(name))


def find_teachers(conn: sqlite3.Connection, query: str) -> list[str]:
    """Найти преподавателей по части ФИО.

    Поиск по подстроке, регистронезависимый, с ё→е («лунева» находит
    «Лунёва Ирина Владимировна»). Сравнение идёт по полному ФИО, поэтому
    «Иван» найдёт и фамилии, и имена с отчествами.

    Args:
        conn: соединение SQLite.
        query: то, что ввёл пользователь («Кудрявцева», «кудр», «лунева»).

    Returns:
        Отсортированный список полных ФИО; пустой, если совпадений нет.
    """
    needle = _normalize_person(query)
    if not needle:
        return []
    return [
        name for name in _all_teacher_names(conn)
        if needle in _normalize_person(name)
    ]


def get_lessons_for_teacher(conn: sqlite3.Connection,
                            teacher: str) -> list[dict]:
    """Пары преподавателя из кэша расписания (все дни недели).

    Сравнение ФИО идёт в Python: ``LIKE`` в SQLite не игнорирует регистр
    кириллицы, поэтому «лунева» не нашла бы «Лунёва». Заодно корректно
    разбираются ячейки с несколькими преподавателями у подгрупп.

    Args:
        conn: соединение SQLite.
        teacher: полное ФИО (как в :func:`find_teachers`).

    Returns:
        Список словарей ``{group_name, day_of_week, para_number, subject,
        teacher, room, week_type}``, отсортированный по дню недели, затем
        по номеру пары. Пустой список, если пар нет.
    """
    target = _normalize_person(teacher)
    rows = conn.execute(
        "SELECT group_name, day_of_week, para_number, subject, teacher,"
        " room, week_type FROM schedule_cache"
        " ORDER BY day_of_week, para_number"
    ).fetchall()

    return [
        dict(row) for row in rows
        if any(_normalize_person(part) == target
               for part in split_teacher_cell(row["teacher"]))
    ]


def get_all_students_with_group(conn: sqlite3.Connection) -> list[dict]:
    """Все активные пользователи с группой (для личных рассылок).

    Напоминания уходят в личку, поэтому источник — ``users`` (там лежит
    ``users.group_name``, группа для расписания), а не ``students``:
    расписание строится именно по группе пользователя, и студент, вступивший
    в группу посещаемости, получает ту же группу и здесь (см.
    :func:`update_user_group_only`).

    Returns:
        Список словарей ``{'tg_id': int, 'group_name': str}``, отсортированный
        по ``tg_id`` (стабильный порядок рассылки).
    """
    rows = conn.execute(
        "SELECT DISTINCT tg_id, group_name FROM users"
        " WHERE group_name IS NOT NULL AND group_name <> '' AND is_active = 1"
        " ORDER BY tg_id"
    ).fetchall()
    return [{"tg_id": int(row["tg_id"]),
             "group_name": str(row["group_name"])} for row in rows]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Прочитать значение из таблицы ``meta`` (None, если ключа нет).

    Отличие от :func:`bot.services.cache_service.get_meta`: здесь нет
    зависимости от ``cache_service`` (он импортирует ``bot.db`` — импорт в
    обратную сторону дал бы цикл). Читают обе функции одну и ту же таблицу.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return str(row["value"]) if row is not None else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Записать значение в ``meta`` (upsert).

    Нужна фоновым задачам: по метке в ``meta`` они понимают, что разовая
    работа за период уже сделана (например, недельная рассылка прогульщикам).
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


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


def latest_substitution_history_date(conn: sqlite3.Connection) -> str | None:
    """Самая поздняя дата в истории замен или None, если история пуста."""
    row = conn.execute(
        "SELECT MAX(date_iso) AS latest FROM substitution_history"
    ).fetchone()
    value = row["latest"] if row is not None else None
    return str(value) if value else None


def get_substitution_history_dates(conn: sqlite3.Connection) -> list[str]:
    """Уникальные даты, по которым есть записи в истории замен.

    Returns:
        Отсортированный по возрастанию список дат (``YYYY-MM-DD``).
    """
    rows = conn.execute(
        "SELECT DISTINCT date_iso FROM substitution_history ORDER BY date_iso"
    ).fetchall()
    return [str(row["date_iso"]) for row in rows]


def count_substitution_history_dates(conn: sqlite3.Connection) -> int:
    """Сколько РАЗНЫХ дат накопилось в истории замен."""
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT date_iso) FROM substitution_history"
        ).fetchone()[0]
    )


# ==========================================================================
# Закрепление расписания в чате (шаг 4)
# ==========================================================================


def set_pinned_message(conn: sqlite3.Connection, chat_id: int,
                       message_id: int, date_iso: str) -> bool:
    """Запомнить, какое сообщение закреплено в чате и на какую дату.

    Returns:
        True, если запись чата найдена и обновлена.
    """
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE group_chats SET pinned_message_id = ?, pinned_date_iso = ?"
            " WHERE chat_id = ?",
            (message_id, date_iso, chat_id),
        )
    return cursor.rowcount > 0


def get_pinned_message(conn: sqlite3.Connection, chat_id: int) -> dict | None:
    """Закреплённое сообщение чата или None, если ничего не закреплено."""
    row = conn.execute(
        "SELECT pinned_message_id, pinned_date_iso FROM group_chats"
        " WHERE chat_id = ? AND pinned_message_id IS NOT NULL",
        (chat_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def clear_pinned_message(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Снять отметку о закреплении (после открепления или ошибки).

    Returns:
        True, если запись чата найдена и обновлена.
    """
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE group_chats SET pinned_message_id = NULL,"
            " pinned_date_iso = NULL WHERE chat_id = ?",
            (chat_id,),
        )
    return cursor.rowcount > 0


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
# --- Связка аккаунтов Telegram ↔ VK ---
#
# Аккаунты в мессенджерах независимы: у Telegram свой ``tg_id`` в ``users``, у
# VK свой ``vk_id`` в ``vk_users``. Связка хранится отдельно (``account_links``,
# миграция 16) и позволяет показывать одну и ту же группу на обеих платформах.
#
# Связка только через одноразовый код: @username в TG и VK — разные сущности,
# совпадение имён ничего не значит.

# Сколько символов в коде связки.
LINK_CODE_LENGTH = 6

# Сколько минут живёт код.
LINK_CODE_TTL_MINUTES = 30

# Алфавит кода без неоднозначных символов: нет O/0 и I/1, чтобы код нельзя
# было переврать при переписывании вручную.
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _link_code_now() -> datetime:
    """Текущий момент в поясе техникума (все отметки времени — в нём)."""
    return datetime.now(TIMEZONE)


def create_link_code(conn: sqlite3.Connection, tg_id: int,
                     now: datetime | None = None) -> str:
    """Создать одноразовый код связки для Telegram-пользователя.

    Старые неиспользованные коды этого ``tg_id`` удаляются: у пользователя
    должен быть ровно один действующий код, иначе после нескольких ``/link``
    на руках оказывалась бы пачка валидных кодов, и «одноразовость» терялась.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id владельца кода.
        now: момент создания (для тестов); по умолчанию — текущий.

    Returns:
        Код из :data:`LINK_CODE_LENGTH` символов (верхний регистр).
    """
    import secrets

    created = now or _link_code_now()
    expires = created + timedelta(minutes=LINK_CODE_TTL_MINUTES)

    with transaction(conn):
        conn.execute(
            "DELETE FROM link_codes WHERE tg_id = ? AND used_at IS NULL",
            (tg_id,),
        )
        # Коллизии практически исключены, но PK не даст вставить дубль —
        # пробуем снова, а не падаем.
        for _ in range(10):
            code = "".join(
                secrets.choice(LINK_CODE_ALPHABET)
                for _ in range(LINK_CODE_LENGTH)
            )
            try:
                conn.execute(
                    "INSERT INTO link_codes"
                    " (code, tg_id, created_at, expires_at, used_at)"
                    " VALUES (?, ?, ?, ?, NULL)",
                    (
                        code,
                        tg_id,
                        created.isoformat(timespec="seconds"),
                        expires.isoformat(timespec="seconds"),
                    ),
                )
                return code
            except sqlite3.IntegrityError:
                continue

    raise RuntimeError("не удалось сгенерировать уникальный код связки")

def use_link_code(conn: sqlite3.Connection, code: str, vk_id: int,
                  now: datetime | None = None) -> dict:
    """Погасить код связки и связать Telegram-аккаунт с VK.

    Проверки: код существует, не использован, не истёк, и ни одна из сторон
    ещё не связана с кем-то другим.

    Args:
        conn: соединение SQLite.
        code: код, который ввёл пользователь VK.
        vk_id: id пользователя VK.
        now: текущий момент (для тестов).

    Returns:
        Словарь ``{'ok': bool, 'tg_id': int | None, 'error': str | None}``.
    """
    moment = now or _link_code_now()
    cleaned = (code or "").strip().upper()

    if not cleaned:
        return {"ok": False, "tg_id": None, "error": "Код не указан."}

    row = conn.execute(
        "SELECT code, tg_id, expires_at, used_at FROM link_codes"
        " WHERE code = ?",
        (cleaned,),
    ).fetchone()
    if row is None:
        return {"ok": False, "tg_id": None,
                "error": "Код не найден. Получи новый в Telegram-боте: /link"}

    tg_id = int(row["tg_id"])

    if row["used_at"]:
        return {"ok": False, "tg_id": None,
                "error": "Этот код уже использован. Получи новый: /link"}

    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
    except ValueError:
        return {"ok": False, "tg_id": None,
                "error": "Код повреждён. Получи новый: /link"}
    if moment >= expires:
        return {"ok": False, "tg_id": None,
                "error": "Код истёк. Получи новый в Telegram-боте: /link"}

    # Этот VK уже с кем-то связан?
    linked_tg = get_tg_id_by_vk(conn, vk_id)
    if linked_tg is not None:
        if linked_tg == tg_id:
            return {"ok": True, "tg_id": tg_id, "error": None}
        return {"ok": False, "tg_id": None,
                "error": "Этот VK уже связан с другим Telegram-аккаунтом."}

    # Этот TG уже связан с другим VK?
    linked_vk = get_vk_id_by_tg(conn, tg_id)
    if linked_vk is not None and linked_vk != vk_id:
        return {"ok": False, "tg_id": None,
                "error": "Твой Telegram уже связан с другим VK. "
                         "Сначала отвяжи: /unlink"}

    with transaction(conn):
        conn.execute(
            "UPDATE link_codes SET used_at = ? WHERE code = ?",
            (moment.isoformat(timespec="seconds"), cleaned),
        )
        conn.execute(
            "INSERT INTO account_links (tg_id, vk_id, linked_at, linked_via)"
            " VALUES (?, ?, ?, 'from_vk')"
            " ON CONFLICT(tg_id) DO UPDATE SET"
            "   vk_id = excluded.vk_id,"
            "   linked_at = excluded.linked_at,"
            "   linked_via = excluded.linked_via",
            (tg_id, vk_id, moment.isoformat(timespec="seconds")),
        )

    logger.info("accounts linked", extra={"tg_id": tg_id, "vk_id": vk_id})
    return {"ok": True, "tg_id": tg_id, "error": None}


def get_vk_id_by_tg(conn: sqlite3.Connection, tg_id: int) -> int | None:
    """VK-аккаунт, связанный с этим Telegram-аккаунтом (или None)."""
    row = conn.execute(
        "SELECT vk_id FROM account_links WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    if row is None or row["vk_id"] is None:
        return None
    return int(row["vk_id"])


def get_tg_id_by_vk(conn: sqlite3.Connection, vk_id: int) -> int | None:
    """Telegram-аккаунт, связанный с этим VK-аккаунтом (или None)."""
    row = conn.execute(
        "SELECT tg_id FROM account_links WHERE vk_id = ?", (vk_id,)
    ).fetchone()
    if row is None or row["tg_id"] is None:
        return None
    return int(row["tg_id"])


def get_link_info(conn: sqlite3.Connection, *, tg_id: int | None = None,
                  vk_id: int | None = None) -> dict | None:
    """Информация о связке аккаунта (для экрана «Профиль»).

    Args:
        conn: соединение SQLite.
        tg_id: посмотреть со стороны Telegram.
        vk_id: посмотреть со стороны VK.

    Returns:
        ``{'tg_id', 'vk_id', 'linked_at', 'linked_via'}`` или None.
    """
    if tg_id is not None:
        row = conn.execute(
            "SELECT tg_id, vk_id, linked_at, linked_via FROM account_links"
            " WHERE tg_id = ?",
            (tg_id,),
        ).fetchone()
    elif vk_id is not None:
        row = conn.execute(
            "SELECT tg_id, vk_id, linked_at, linked_via FROM account_links"
            " WHERE vk_id = ?",
            (vk_id,),
        ).fetchone()
    else:
        return None

    if row is None:
        return None
    return {
        "tg_id": int(row["tg_id"]) if row["tg_id"] is not None else None,
        "vk_id": int(row["vk_id"]) if row["vk_id"] is not None else None,
        "linked_at": str(row["linked_at"] or ""),
        "linked_via": str(row["linked_via"] or ""),
    }


def unlink_account(conn: sqlite3.Connection, tg_id: int) -> bool:
    """Удалить связку Telegram-аккаунта с VK.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.

    Returns:
        True, если связка была и удалена; False — если связки не было.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM account_links WHERE tg_id = ?", (tg_id,)
        )
    removed = cursor.rowcount > 0
    if removed:
        logger.info("accounts unlinked", extra={"tg_id": tg_id})
    return removed


def unlink_account_by_vk(conn: sqlite3.Connection, vk_id: int) -> bool:
    """Удалить связку со стороны VK (пользователь VK отвязался).

    Args:
        conn: соединение SQLite.
        vk_id: id пользователя VK.

    Returns:
        True, если связка была и удалена.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM account_links WHERE vk_id = ?", (vk_id,)
        )
    removed = cursor.rowcount > 0
    if removed:
        logger.info("accounts unlinked by vk", extra={"vk_id": vk_id})
    return removed


# --- Данные VK-аккаунта ---

def get_vk_user(conn: sqlite3.Connection, vk_id: int) -> dict | None:
    """Данные VK-пользователя или None.

    Returns:
        ``{'vk_id', 'group_name', 'full_name'}`` или None.
    """
    row = conn.execute(
        "SELECT vk_id, group_name, full_name FROM vk_users WHERE vk_id = ?",
        (vk_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "vk_id": int(row["vk_id"]),
        "group_name": str(row["group_name"] or ""),
        "full_name": str(row["full_name"] or ""),
    }


def upsert_vk_user(conn: sqlite3.Connection, vk_id: int,
                   group_name: str | None = None,
                   full_name: str | None = None) -> None:
    """Создать или обновить VK-пользователя.

    Пустые значения не затирают уже сохранённые: при связке аккаунтов группа
    может быть неизвестна, и терять прежнюю нельзя.

    Args:
        conn: соединение SQLite.
        vk_id: id пользователя VK.
        group_name: имя группы (None — не менять).
        full_name: имя из профиля VK (None — не менять).
    """
    now = _link_code_now().isoformat(timespec="seconds")

    with transaction(conn):
        row = conn.execute(
            "SELECT vk_id FROM vk_users WHERE vk_id = ?", (vk_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO vk_users"
                " (vk_id, group_name, full_name, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (vk_id, group_name or "", full_name or "", now, now),
            )
            return

        if group_name:
            conn.execute(
                "UPDATE vk_users SET group_name = ?, updated_at = ?"
                " WHERE vk_id = ?",
                (group_name, now, vk_id),
            )
        if full_name:
            conn.execute(
                "UPDATE vk_users SET full_name = ?, updated_at = ?"
                " WHERE vk_id = ?",
                (full_name, now, vk_id),
            )


def update_vk_user_group(conn: sqlite3.Connection, vk_id: int,
                         group_name: str) -> None:
    """Записать группу VK-пользователю (создать запись при необходимости)."""
    upsert_vk_user(conn, vk_id, group_name=group_name)


def get_vk_group(conn: sqlite3.Connection, vk_id: int) -> str | None:
    """Группа VK-пользователя или None."""
    row = conn.execute(
        "SELECT group_name FROM vk_users WHERE vk_id = ?", (vk_id,)
    ).fetchone()
    if row is None:
        return None
    group = str(row["group_name"] or "").strip()
    return group or None


def get_effective_group(conn: sqlite3.Connection, *, tg_id: int | None = None,
                        vk_id: int | None = None) -> str | None:
    """Группа аккаунта с учётом связки Telegram ↔ VK.

    У связанной пары группа хранится в двух местах (``users`` и ``vk_users``) и
    синхронизируется при каждой смене. Если значения всё же разошлись (ручная
    правка в БД, сбой между двумя записями), приоритет у Telegram: он ведущая
    платформа. Расхождение попадает в лог как предупреждение.

    Args:
        conn: соединение SQLite.
        tg_id: посмотреть со стороны Telegram.
        vk_id: посмотреть со стороны VK.

    Returns:
        Имя группы или None, если группа не задана нигде.
    """
    if tg_id is None and vk_id is None:
        return None

    if tg_id is None:
        tg_id = get_tg_id_by_vk(conn, vk_id)  # type: ignore[arg-type]
        tg_group = get_user_group(conn, tg_id) if tg_id is not None else None
        linked_vk = vk_id
    else:
        tg_group = get_user_group(conn, tg_id)
        linked_vk = get_vk_id_by_tg(conn, tg_id)

    vk_group = get_vk_group(conn, linked_vk) if linked_vk is not None else None

    if tg_group and vk_group and tg_group != vk_group:
        logger.warning(
            "linked accounts have different groups",
            extra={"tg_id": tg_id, "vk_id": linked_vk,
                   "tg_group": tg_group, "vk_group": vk_group},
        )
    return tg_group or vk_group
# --- Роль «преподаватель» (миграция 17) ---
#
# Регистрация с модерацией: преподаватель выбирает своё ФИО из справочника и
# создаёт заявку; доступ к группам и расписанию открывается только после
# одобрения админом. Без модерации любой мог бы назваться чужим ФИО.
#
# Роль хранится в отдельной таблице ``teachers`` и не отменяет роль студента:
# у преподавателя может быть своя группа. Существующие таблицы не меняются.

# Статусы заявки.
TEACHER_PENDING = "pending"
TEACHER_APPROVED = "approved"
TEACHER_REJECTED = "rejected"

TEACHER_STATUSES = (TEACHER_PENDING, TEACHER_APPROVED, TEACHER_REJECTED)


def _teacher_now_iso() -> str:
    """Текущий момент в поясе техникума (как в остальных таблицах)."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def apply_teacher(conn: sqlite3.Connection, tg_id: int,
                  full_name: str) -> dict:
    """Создать заявку на роль преподавателя.

    Повторная заявка не создаётся: если заявка уже есть (в любом статусе),
    возвращается ошибка с текущим состоянием. Отклонённую заявку админ может
    одобрить позже, поэтому новый запрос не нужен.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id заявителя.
        full_name: ФИО из справочника (проверяется вызывающим кодом).

    Returns:
        Словарь ``{'ok': bool, 'error': str | None, 'status': str | None}``.
    """
    name = (full_name or "").strip()
    if not name:
        return {"ok": False, "error": "ФИО не указано.", "status": None}

    existing = get_teacher(conn, tg_id)
    if existing is not None:
        status = str(existing["status"])
        if status == TEACHER_PENDING:
            error = "Заявка уже отправлена и ждёт проверки админом."
        elif status == TEACHER_APPROVED:
            error = "Ты уже преподаватель."
        else:
            error = "Предыдущая заявка отклонена — напиши админу."
        return {"ok": False, "error": error, "status": status}

    # ФИО должно быть из справочника: иначе препод «подделается» под любого.
    from bot.parsers.teachers import TEACHERS, PLACEHOLDER_MARK

    if is_vacancy(name):
        # «вакансия» — служебная метка, а не человек: за ней стоят десятки
        # групп, и доступ к ним не должен достаться никому.
        return {"ok": False,
                "error": "Это не ФИО преподавателя.",
                "status": None}

    known = any(value == name for value in TEACHERS.values())
    if not known:
        return {"ok": False, "error": "ФИО нет в справочнике.",
                "status": None}
    if PLACEHOLDER_MARK in name:
        # «(ФИО уточняется)» — в справочнике ещё нет настоящего имени.
        return {"ok": False,
                "error": "Для этой фамилии ФИО ещё не заполнено в справочнике. "
                         "Напиши админу.",
                "status": None}

    with transaction(conn):
        conn.execute(
            "INSERT INTO teachers"
            " (tg_id, full_name, status, applied_at, approved_at, approved_by)"
            " VALUES (?, ?, ?, ?, NULL, NULL)",
            (tg_id, name, TEACHER_PENDING, _teacher_now_iso()),
        )
    logger.info("teacher application created",
                extra={"tg_id": tg_id, "full_name": name})
    return {"ok": True, "error": None, "status": TEACHER_PENDING}


def get_teacher(conn: sqlite3.Connection, tg_id: int) -> dict | None:
    """Заявка/роль преподавателя по Telegram id или None.

    Returns:
        Словарь ``{'tg_id', 'full_name', 'status', 'applied_at',
        'approved_at', 'approved_by'}`` или None.
    """
    row = conn.execute(
        "SELECT tg_id, full_name, status, applied_at, approved_at, approved_by"
        " FROM teachers WHERE tg_id = ?",
        (tg_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "tg_id": int(row["tg_id"]),
        "full_name": str(row["full_name"] or ""),
        "status": str(row["status"] or ""),
        "applied_at": str(row["applied_at"] or ""),
        "approved_at": (str(row["approved_at"])
                        if row["approved_at"] is not None else None),
        "approved_by": (int(row["approved_by"])
                        if row["approved_by"] is not None else None),
    }


def is_teacher(conn: sqlite3.Connection, tg_id: int) -> bool:
    """True, если заявка одобрена (только тогда есть доступ).

    До одобрения прав нет: ``pending`` и ``rejected`` дают False.
    """
    row = conn.execute(
        "SELECT status FROM teachers WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    return row is not None and str(row["status"]) == TEACHER_APPROVED
def _set_teacher_status(conn: sqlite3.Connection, tg_id: int,
                        status: str, admin_id: int | None = None) -> bool:
    """Сменить статус заявки (внутренняя).

    Returns:
        True, если запись была и статус изменён.
    """
    now = _teacher_now_iso()
    if status == TEACHER_APPROVED:
        sql = ("UPDATE teachers SET status = ?, approved_at = ?,"
               " approved_by = ? WHERE tg_id = ?")
        params = (status, now, admin_id, tg_id)
    else:
        # Отклонение: отметку одобрения снимаем, чтобы не путала.
        sql = ("UPDATE teachers SET status = ?, approved_at = NULL,"
               " approved_by = ? WHERE tg_id = ?")
        params = (status, admin_id, tg_id)

    with transaction(conn):
        cursor = conn.execute(sql, params)
    changed = cursor.rowcount > 0
    if changed:
        logger.info(
            "teacher status changed",
            extra={"tg_id": tg_id, "status": status, "admin_id": admin_id},
        )
    return changed


def approve_teacher(conn: sqlite3.Connection, tg_id: int,
                    admin_id: int) -> bool:
    """Одобрить заявку: преподаватель получает доступ.

    Returns:
        True, если заявка была и одобрена.
    """
    return _set_teacher_status(conn, tg_id, TEACHER_APPROVED, admin_id)


def reject_teacher(conn: sqlite3.Connection, tg_id: int,
                   admin_id: int) -> bool:
    """Отклонить заявку: доступа не будет.

    Returns:
        True, если заявка была и отклонена.
    """
    return _set_teacher_status(conn, tg_id, TEACHER_REJECTED, admin_id)


def cancel_teacher_application(conn: sqlite3.Connection, tg_id: int) -> bool:
    """Отменить СВОЮ заявку — только пока она не рассмотрена.

    Одобренную роль так снять нельзя: доступ уже выдан, и снимать его должен
    админ. Поэтому отменяется лишь статус ``pending``.

    Returns:
        True, если заявка была в статусе ``pending`` и удалена.
    """
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM teachers WHERE tg_id = ? AND status = ?",
            (tg_id, TEACHER_PENDING),
        )
    removed = cursor.rowcount > 0
    if removed:
        logger.info("teacher application cancelled", extra={"tg_id": tg_id})
    return removed


def _list_teachers_by_status(conn: sqlite3.Connection,
                             status: str) -> list[dict]:
    """Заявки с указанным статусом (по времени подачи)."""
    rows = conn.execute(
        "SELECT tg_id, full_name, status, applied_at, approved_at, approved_by"
        " FROM teachers WHERE status = ? ORDER BY applied_at",
        (status,),
    ).fetchall()
    return [
        {
            "tg_id": int(row["tg_id"]),
            "full_name": str(row["full_name"] or ""),
            "status": str(row["status"] or ""),
            "applied_at": str(row["applied_at"] or ""),
            "approved_at": (str(row["approved_at"])
                            if row["approved_at"] is not None else None),
            "approved_by": (int(row["approved_by"])
                            if row["approved_by"] is not None else None),
        }
        for row in rows
    ]


def list_pending_teachers(conn: sqlite3.Connection) -> list[dict]:
    """Все заявки, ожидающие решения админа."""
    return _list_teachers_by_status(conn, TEACHER_PENDING)


def list_approved_teachers(conn: sqlite3.Connection) -> list[dict]:
    """Все одобренные преподаватели."""
    return _list_teachers_by_status(conn, TEACHER_APPROVED)


def get_teacher_groups(conn: sqlite3.Connection,
                       full_name: str) -> list[str]:
    """Уникальные группы, где преподаватель ведёт занятия.

    Сравнение ФИО — в Python (:func:`_normalize_person`), а не через ``=``:
    в ячейке ``schedule_cache.teacher`` часто стоит НЕСКОЛЬКО преподавателей
    через запятую (подгруппы одного занятия), и ``WHERE teacher = ?`` пропустил
    бы такие пары. Заодно учитывается «ё/е» и регистр.

    Args:
        conn: соединение SQLite.
        full_name: полное ФИО из справочника.

    Returns:
        Отсортированный список групп; пустой, если пар нет.
    """
    target = _normalize_person(full_name)
    if not target or is_vacancy(full_name):
        # «вакансия» — не человек: пар у неё нет (иначе она «вела» бы
        # полсотни групп сразу).
        return []

    rows = conn.execute(
        "SELECT DISTINCT group_name, teacher FROM schedule_cache"
        " ORDER BY group_name"
    ).fetchall()
    found: set[str] = set()
    for row in rows:
        if any(_normalize_person(part) == target
               for part in split_teacher_cell(row["teacher"])):
            found.add(str(row["group_name"]))
    return sorted(found)


def get_teacher_lessons_for_day(conn: sqlite3.Connection, full_name: str,
                                d) -> list[dict]:
    """Пары преподавателя на конкретный день (с учётом чётности недели).

    Чётность берётся из :func:`bot.services.schedule_service.week_type_for_date`
    и сравнивается с ``week_type`` пары: пары «по чётным» не показываются в
    нечётную неделю. Импорт внутри функции — чтобы ``bot.db`` не зависел от
    сервиса расписания на этапе загрузки.

    Args:
        conn: соединение SQLite.
        full_name: полное ФИО из справочника.
        d: дата (``datetime.date``).

    Returns:
        Список пар ``{group_name, day_of_week, para_number, subject, teacher,
        room, week_type}``, отсортированный по номеру пары. В каждой записи
        добавлено ``time_range`` (звонки с учётом субботы).
    """
    from bot.services.schedule_service import (
        time_range_for_date,
        week_type_for_date,
    )

    target = _normalize_person(full_name)
    if not target or is_vacancy(full_name):
        return []

    day_of_week = d.isoweekday()
    target_week = week_type_for_date(d)
    rows = conn.execute(
        "SELECT group_name, day_of_week, para_number, subject, teacher,"
        " room, week_type FROM schedule_cache WHERE day_of_week = ?"
        " ORDER BY para_number",
        (day_of_week,),
    ).fetchall()

    lessons: list[dict] = []
    for row in rows:
        if not any(_normalize_person(part) == target
                   for part in split_teacher_cell(row["teacher"])):
            continue
        week_type = str(row["week_type"] or "")
        # Пустая чётность = пара каждую неделю.
        if week_type and week_type != target_week:
            continue
        lesson = dict(row)
        lesson["time_range"] = time_range_for_date(
            int(row["para_number"]), d
        )
        lessons.append(lesson)

    lessons.sort(key=lambda item: int(item["para_number"]))
    return lessons
    return sorted(found)