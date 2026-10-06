"""Данные VK-бота: группа пользователя VK и подбор группы по вводу.

Модуль aiogram-free: только sqlite3 и :mod:`bot.parsers.groups`. Здесь нет
ничего, что тянуло бы ``aiogram`` (его подгружает ``bot.attendance.service``
через пакет ``bot.attendance``), поэтому поиск группы реализован на тех же
источниках своими силами:

- источник списка групп — ``schedule_cache`` (как в
  ``bot.db.list_available_groups``);
- нормализация — :func:`bot.parsers.groups.normalize_group_name`;
- нечёткое сравнение — :mod:`difflib` с теми же порогами, что в Telegram
  (``0.6`` для автопринятия, ``0.5`` для подсказок).

В финале связка двух платформ: ``storage`` даёт доступ к общим функциям
связки из :mod:`bot.db` (``use_link_code`` и т. п.), но не импортирует
``bot.handlers`` — правила по импортам сохраняются.

Хранилище — таблица ``vk_users`` (миграция 15). Группа VK-пользователя
специально НЕ пишется в ``users``: там ``tg_id``, и Telegram-рассылки
приняли бы VK-пользователя за Telegram-чат.
"""
from __future__ import annotations

import difflib
import logging
import sqlite3
from datetime import datetime

from bot.config import TIMEZONE
from bot.db import TEACHER_APPROVED
from bot.parsers.groups import normalize_group_name

logger = logging.getLogger(__name__)

# Имя Telegram-бота: подсказка, куда идти за кодом связки. Совпадает с
# bot.handlers.account_link.BOT_USERNAME и упоминанием в notify_service.
TELEGRAM_BOT_USERNAME = "kst24_bot"

# Порог автопринятия группы введённого имени. Как в Telegram
# (bot.attendance.service.find_group_by_name).
MATCH_CUTOFF = 0.6

# Порог для подсказок «может, ты имел в виду».
SUGGEST_CUTOFF = 0.5

# Сколько групп предлагать при опечатке.
MAX_SUGGESTIONS = 3


def _now_iso() -> str:
    """Текущий момент в поясе техникума (как в остальных таблицах)."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def get_user_group(conn: sqlite3.Connection, vk_id: int) -> str | None:
    """Группа VK-пользователя или None, если он ещё не регистрировался.

    Args:
        conn: соединение SQLite.
        vk_id: id пользователя VK (``message.from_id``).

    Returns:
        Имя группы или None.
    """
    row = conn.execute(
        "SELECT group_name FROM vk_users WHERE vk_id = ?", (vk_id,)
    ).fetchone()
    if row is None:
        return None
    group = str(row["group_name"]).strip()
    return group or None


def get_user(conn: sqlite3.Connection, vk_id: int) -> dict | None:
    """Данные VK-пользователя (группа, имя) или None.

    Returns:
        Словарь ``{'vk_id', 'group_name', 'full_name'}`` или None.
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


def save_user_group(conn: sqlite3.Connection, vk_id: int, group_name: str,
                    full_name: str = "") -> None:
    """Сохранить группу VK-пользователя (создать запись или обновить).

    Имя обновляется только если передано непустым: при смене группы из
    текстового шага имя из VK-профиля недоступно, и затирать сохранённое
    пустой строкой нельзя.

    Args:
        conn: соединение SQLite.
        vk_id: id пользователя VK.
        group_name: уже нормализованное имя группы.
        full_name: имя из профиля VK (может быть пустым).
    """
    now = _now_iso()
    with conn:  # commit/rollback автоматически
        cursor = conn.execute(
            "UPDATE vk_users SET group_name = ?, updated_at = ? WHERE vk_id = ?",
            (group_name, now, vk_id),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO vk_users"
                " (vk_id, group_name, full_name, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (vk_id, group_name, full_name or "", now, now),
            )
        elif full_name:
            conn.execute(
                "UPDATE vk_users SET full_name = ? WHERE vk_id = ?",
                (full_name, vk_id),
            )
    logger.info(
        "vk group saved", extra={"vk_id": vk_id, "group": group_name}
    )


def available_groups(conn: sqlite3.Connection) -> list[str]:
    """Список групп КСТ из кэша расписания (``schedule_cache``).

    Returns:
        Отсортированные имена групп; пустой список, если кэш ещё не наполнен.
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM schedule_cache"
        " WHERE group_name IS NOT NULL AND group_name != ''"
        " ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]


def find_exact_group(conn: sqlite3.Connection, query: str) -> str | None:
    """Найти группу по точному совпадению (после нормализации).

    Точного совпадения достаточно для сохранения: «26 КАД» → «26КАД»,
    «25кад» → «25КАД». Нечёткое сравнение для этого не используется намеренно —
    см. :func:`suggest_groups`.

    Args:
        conn: соединение SQLite.
        query: то, что ввёл пользователь.

    Returns:
        Каноническое имя группы или None.
    """
    normalized = normalize_group_name(query or "")
    if not normalized:
        return None
    return normalized if normalized in available_groups(conn) else None


def find_group(conn: sqlite3.Connection, query: str) -> str | None:
    """Ближайшая группа по нечёткому сравнению (fuzzy) или None.

    ВНИМАНИЕ: результат может быть не тем, что ввёл пользователь. На коротких
    номерах групп сходство обманчиво высокое: «25КД» и «25КАД» совпадают на
    0.89, то есть автопринятие сохранило бы чужую группу и показало чужое
    расписание. Поэтому функция годится для ПОДСКАЗОК (см.
    :func:`suggest_groups`), а для сохранения используйте
    :func:`find_exact_group`.

    Args:
        conn: соединение SQLite.
        query: ввод пользователя.

    Returns:
        Имя ближайшей группы или None.
    """
    normalized = normalize_group_name(query or "")
    if not normalized:
        return None

    groups = available_groups(conn)
    if not groups:
        logger.warning("список групп пуст: кэш расписания не заполнен")
        return None
    if normalized in groups:
        return normalized

    matches = difflib.get_close_matches(
        normalized, groups, n=1, cutoff=MATCH_CUTOFF
    )
    return matches[0] if matches else None


def suggest_groups(conn: sqlite3.Connection, query: str) -> list[str]:
    """До трёх похожих групп для подсказки при опечатке.

    Args:
        conn: соединение SQLite.
        query: ввод пользователя.

    Returns:
        Список ближайших названий (может быть пустым).
    """
    normalized = normalize_group_name(query or "")
    groups = available_groups(conn)
    if not normalized or not groups:
        return []
    return difflib.get_close_matches(
        normalized, groups, n=MAX_SUGGESTIONS, cutoff=SUGGEST_CUTOFF
    )


# --- связка аккаунтов ---
#
# Функции связки живут в :mod:`bot.db` (общая таблица ``account_links``), но
# вызывающий код в VK работает через этот модуль: так у VK-бота одна точка
# доступа к данным и нет прямых импортов ``bot.db`` в хендлерах.

def link_account(conn: sqlite3.Connection, code: str, vk_id: int) -> dict:
    """Связать VK-аккаунт с Telegram по одноразовому коду.

    Args:
        conn: соединение SQLite.
        code: код из Telegram-бота.
        vk_id: id пользователя VK.

    Returns:
        ``{'ok', 'tg_id', 'error'}`` (см. :func:`bot.db.use_link_code`).
    """
    from bot.db import use_link_code

    return use_link_code(conn, code, vk_id)


def get_tg_id_by_vk(conn: sqlite3.Connection, vk_id: int) -> int | None:
    """Telegram-аккаунт, связанный с этим VK-аккаунтом (или None)."""
    from bot.db import get_tg_id_by_vk as _get

    return _get(conn, vk_id)


def unlink_account(conn: sqlite3.Connection, vk_id: int) -> bool:
    """Удалить связку со стороны VK.

    Returns:
        True, если связка была и удалена.
    """
    from bot.db import unlink_account_by_vk

    return unlink_account_by_vk(conn, vk_id)


def get_tg_group(conn: sqlite3.Connection, tg_id: int) -> str | None:
    """Группа Telegram-аккаунта (из таблицы ``users``).

    Отдельная функция нужна потому, что :func:`get_user_group` в этом модуле
    читает ``vk_users``: это группа VK-пользователя. Для переноса группы при
    связке нужна именно Telegram-группа.
    """
    from bot.db import get_user_group as _get_tg_group

    return _get_tg_group(conn, tg_id)


def get_profile_group(conn: sqlite3.Connection, vk_id: int) -> str | None:
    """Группа для профиля VK с учётом связки.

    У связанной пары группа одна на обе платформы, поэтому читаем через общую
    функцию: так профиль покажет то же, что Telegram-бот.
    """
    from bot.db import get_effective_group

    return get_effective_group(conn, vk_id=vk_id)


# --- роль преподавателя ---
#
# Заявки живут в таблице ``teachers`` по ``tg_id`` (Telegram). У VK-пользователя
# своего tg_id нет, поэтому сначала берём связанный Telegram: без связки подать
# заявку нельзя — иначе препод в TG не получил бы доступ по ней. Функции ниже
# оборачивают :mod:`bot.db`, чтобы у VK-кода была одна точка доступа к данным.

def get_linked_tg_id(conn: sqlite3.Connection, vk_id: int) -> int | None:
    """Telegram-аккаунт, связанный с VK (синоним :func:`get_tg_id_by_vk`).

    Отдельное имя — потому что в контексте преподавателя читается понятнее:
    заявка подаётся «за связанный Telegram», а не «по vk_id».
    """
    return get_tg_id_by_vk(conn, vk_id)


def get_teacher(conn: sqlite3.Connection, tg_id: int) -> dict | None:
    """Заявка/роль преподавателя по Telegram id (или None)."""
    from bot.db import get_teacher as _get

    return _get(conn, tg_id)


def teacher_full_name(conn: sqlite3.Connection, vk_id: int) -> str | None:
    """ФИО преподавателя для этого VK-пользователя (или None).

    None означает «не преподаватель»: нет связки с TG, нет заявки либо заявка
    ещё не одобрена. Одобрение проверяется здесь же — по ней VK-бот решает,
    показывать ли меню преподавателя.
    """
    tg_id = get_linked_tg_id(conn, vk_id)
    if tg_id is None:
        return None

    teacher = get_teacher(conn, tg_id)
    if not teacher or str(teacher.get("status")) != TEACHER_APPROVED:
        return None
    return str(teacher.get("full_name") or "") or None


def apply_teacher(conn: sqlite3.Connection, tg_id: int,
                  full_name: str) -> dict:
    """Создать заявку на роль преподавателя (см. :func:`bot.db.apply_teacher`)."""
    from bot.db import apply_teacher as _apply

    return _apply(conn, tg_id, full_name)


def cancel_teacher_application(conn: sqlite3.Connection, tg_id: int) -> bool:
    """Отменить свою заявку, пока она не рассмотрена."""
    from bot.db import cancel_teacher_application as _cancel

    return _cancel(conn, tg_id)


# --- учебная группа (раздел «📊 Моя группа») ---
#
# Читаем таблицы ``study_groups`` и ``students`` ПРЯМЫМ SQL, а не через
# :mod:`bot.attendance.db`. Причина: пакет ``bot.attendance`` импортирует
# aiogram в своём ``__init__`` (там лежат хендлеры), и обращение к нему из
# ``bot_vk`` подняло бы весь Telegram-стек в процессе VK-бота. Те же таблицы
# и тот же порядок строк, что в ``bot/attendance/db.py``, поэтому данные
# видны одинаково на обеих платформах.
#
# У VK-пользователя нет своего tg_id, поэтому «свой» студент определяется по
# связке vk_id → tg_id (``students.tg_id`` — Telegram id). Без связки студент
# не может быть ни старостой, ни замом: показываем приглашение привязать TG.

# Роли студентов (совпадают с bot/attendance/models.py).
ROLE_STUDENT = "student"
ROLE_DEPUTY = "deputy"
ROLE_STAROSTA = "starosta"

# Роли, которым показываем код приглашения (совпадают с MANAGE_ROLES в TG).
GROUP_ADMIN_ROLES = frozenset({ROLE_STAROSTA, ROLE_DEPUTY})


def get_study_group(conn: sqlite3.Connection, group_name: str) -> dict | None:
    """Учебная группа (``study_groups``) по имени или None.

    Returns:
        Словарь с ``group_name``, ``invite_code``, ``starosta_tg_id``,
        ``deputy_tg_id``, ``created_at``, ``created_by``.
    """
    row = conn.execute(
        "SELECT * FROM study_groups WHERE group_name = ?", (group_name,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_group_students(conn: sqlite3.Connection,
                       group_name: str) -> list[dict]:
    """Студенты учебной группы по алфавиту (как в Telegram).

    Returns:
        Список словарей ``students``: ``tg_id``, ``group_name``, ``full_name``,
        ``role``, ``joined_at``.
    """
    rows = conn.execute(
        "SELECT * FROM students WHERE group_name = ?"
        " ORDER BY full_name COLLATE NOCASE, tg_id",
        (group_name,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_student(conn: sqlite3.Connection, tg_id: int) -> dict | None:
    """Студент учебной группы по Telegram id или None."""
    row = conn.execute(
        "SELECT * FROM students WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_student_by_vk(conn: sqlite3.Connection, vk_id: int) -> dict | None:
    """Студент учебной группы для этого VK-пользователя (или None).

    None означает «не в группе»: либо нет связки с TG, либо связанный
    Telegram-аккаунт ещё не ввёл код приглашения.
    """
    tg_id = get_linked_tg_id(conn, vk_id)
    if tg_id is None:
        return None
    return get_student(conn, tg_id)


def role_of(student: dict | None) -> str:
    """Роль студента (``student``/``deputy``/``starosta``)."""
    if not student:
        return ROLE_STUDENT
    return str(student.get("role") or ROLE_STUDENT)


def is_group_admin(conn: sqlite3.Connection, vk_id: int) -> bool:
    """Является ли VK-пользователь старостой или замом своей группы."""
    return role_of(get_student_by_vk(conn, vk_id)) in GROUP_ADMIN_ROLES


def student_name_by_tg(conn: sqlite3.Connection, tg_id: int | None) -> str:
    """ФИО студента по Telegram id (пустая строка, если его нет).

    Нужно для строк «Староста: …» / «Зам: …»: в ``study_groups`` лежит только
    tg_id, а показывать надо имя.
    """
    if tg_id is None:
        return ""
    student = get_student(conn, int(tg_id))
    return str(student["full_name"]) if student else ""


def group_snapshot(conn: sqlite3.Connection, group_name: str) -> dict:
    """Всё для карточки группы одним вызовом: счётчики, роли, код.

    Собирает данные в одном месте, чтобы хендлер VK не ходил в БД пять раз и
    не дублировал логику связей (староста/зам лежат и в ``study_groups``, и в
    ``students.role``).

    Returns:
        Словарь ``{'group_name', 'count', 'students', 'starosta_name',
        'deputy_name', 'invite_code', 'mode'}``; пустые значения, если группы
        нет.
    """
    group = get_study_group(conn, group_name) or {}
    students = get_group_students(conn, group_name)
    return {
        "group_name": group_name,
        "count": len(students),
        "students": students,
        "starosta_name": student_name_by_tg(conn, group.get("starosta_tg_id")),
        "deputy_name": student_name_by_tg(conn, group.get("deputy_tg_id")),
        "invite_code": str(group.get("invite_code") or ""),
        "mode": str(group.get("attendance_mode") or "chat"),
    }
# --- дедлайны ---
#
# Дедлайны общие с Telegram: строки таблицы ``deadlines`` привязаны к ``tg_id``,
# поэтому у VK-пользователя работает только со связкой vk_id → tg_id. Так
# студент заводит дедлайн в TG и видит его в VK (и наоборот) — это и есть
# «полный паритет», ради которого связка и делалась.
#
# Логика (срочность, разбор дат, soft delete) живёт в
# :mod:`bot.services.deadline_service` — он aiogram-free, поэтому VK его
# переиспользует напрямую, без копии правил.

def deadlines_tg_id(conn: sqlite3.Connection, vk_id: int) -> int | None:
    """Telegram id для работы с дедлайнами (или None без связки)."""
    return get_linked_tg_id(conn, vk_id)


def list_deadlines(conn: sqlite3.Connection, tg_id: int) -> list[dict]:
    """Активные дедлайны пользователя, отсортированные по сроку."""
    from bot.services import deadline_service

    return deadline_service.list_active(conn, tg_id)


def add_deadline(conn: sqlite3.Connection, tg_id: int, subject: str,
                 teacher: str, task: str, date_iso: str | None) -> int:
    """Создать дедлайн; возвращает его id."""
    from bot.services import deadline_service

    return deadline_service.add(conn, tg_id, subject, teacher, task, date_iso)


def get_deadline(conn: sqlite3.Connection, deadline_id: int,
                 tg_id: int) -> dict | None:
    """Дедлайн по id, принадлежащий пользователю (иначе None).

    Проверка владельца обязательна: id приходит из payload кнопки, а его
    пользователь может подделать.
    """
    from bot.services import deadline_service

    return deadline_service.get(conn, deadline_id, tg_id)


def delete_deadline(conn: sqlite3.Connection, deadline_id: int,
                    tg_id: int) -> bool:
    """Мягко удалить дедлайн (проставить ``deleted_at``)."""
    from bot.services import deadline_service

    return deadline_service.soft_delete(conn, deadline_id, tg_id)


def parse_deadline_date(text: str) -> str | None:
    """Разобрать дату, введённую вручную (ДД.ММ.ГГГГ, ДД.ММ.ГГ, ДД.ММ)."""
    from bot.services import deadline_service

    return deadline_service.parse_manual_date(text)


def deadline_days_left(date_iso: str | None) -> int | None:
    """Сколько дней осталось до срока (отрицательное — просрочено)."""
    from bot.services import deadline_service

    return deadline_service.days_left(date_iso)