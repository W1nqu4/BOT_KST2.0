"""Дедлайны: CRUD, срочность, парсинг дат (шаг 8).

Все функции работают с соединением ``conn``, которое хендлеры получают от
диспетчера. Удаление — мягкое: строка остаётся в таблице с ``deleted_at``,
поэтому история сохраняется.

Дедлайн может быть **без даты**: тогда он попадает в группу «Без даты» и
никогда не напоминается.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from bot.config import TIMEZONE
from bot.db import transaction

logger = logging.getLogger(__name__)

# Группы срочности: (эмодзи, заголовок). Порядок — как показываем в списке.
URGENCY_OVERDUE = ("🔴", "Просрочено")
URGENCY_TODAY = ("🟠", "Сегодня")
URGENCY_TOMORROW = ("🟡", "Завтра")
URGENCY_WEEK = ("⚪", "На неделе")
URGENCY_LATER = ("⚪", "Позже")
URGENCY_NO_DATE = ("⚪", "Без даты")

URGENCY_ORDER = (
    URGENCY_OVERDUE, URGENCY_TODAY, URGENCY_TOMORROW,
    URGENCY_WEEK, URGENCY_LATER, URGENCY_NO_DATE,
)

# Форматы ручного ввода даты (в порядке проверки).
MANUAL_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y")

# Полная дата ДД.ММ.ГГГГ / ДД.ММ.ГГ либо короткая ДД.ММ.
_FULL_DATE_RE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$")
_SHORT_DATE_RE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}$")

# Разделители даты: точка, дефис, слэш.
_SEPARATORS_RE = re.compile(r"[.\-/]")


def now_iso() -> str:
    """Текущий момент в ISO-8601 с поясом техникума."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def add(conn, tg_id: int, subject: str, teacher: str, task: str,
        date_iso: str | None) -> int:
    """Добавить дедлайн.

    Args:
        conn: соединение SQLite.
        tg_id: владелец.
        subject: предмет (или произвольный заголовок).
        teacher: преподаватель (может быть пустым).
        task: что нужно сделать.
        date_iso: срок в ISO или None/'' — дедлайн без даты.

    Returns:
        ``id`` созданной записи.
    """
    deadline_date = (date_iso or "").strip() or None
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO deadlines (tg_id, subject, teacher, task, deadline_date)"
            " VALUES (?, ?, ?, ?, ?)",
            (tg_id, subject, teacher, task, deadline_date),
        )
        new_id = int(cursor.lastrowid or 0)
    logger.info("deadline added", extra={"deadline_id": new_id, "tg_id": tg_id})
    return new_id


def get(conn, deadline_id: int, tg_id: int) -> dict | None:
    """Дедлайн по id, принадлежащий пользователю; иначе None.

    Проверка владельца обязательна: id приходят из callback-данных, которые
    пользователь может подделать.
    """
    row = conn.execute(
        "SELECT * FROM deadlines WHERE id = ? AND tg_id = ? AND deleted_at IS NULL",
        (deadline_id, tg_id),
    ).fetchone()
    return dict(row) if row is not None else None


def list_active(conn, tg_id: int) -> list[dict]:
    """Активные дедлайны пользователя, отсортированные по сроку.

    Дедлайны без даты идут в конце: в SQLite NULL при ASC идёт первым, поэтому
    порядок задаём явным выражением ``deadline_date IS NULL``.

    Returns:
        Список словарей таблицы ``deadlines``.
    """
    rows = conn.execute(
        "SELECT * FROM deadlines"
        " WHERE tg_id = ? AND deleted_at IS NULL"
        " ORDER BY deadline_date IS NULL, deadline_date ASC, id ASC",
        (tg_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def update(conn, deadline_id: int, tg_id: int, subject: str, teacher: str,
           task: str, date_iso: str | None) -> bool:
    """Изменить дедлайн.

    Returns:
        True, если запись найдена и обновлена; False — если её нет или она
        принадлежит другому пользователю.
    """
    if get(conn, deadline_id, tg_id) is None:
        return False
    deadline_date = (date_iso or "").strip() or None
    with transaction(conn):
        conn.execute(
            "UPDATE deadlines SET subject = ?, teacher = ?, task = ?,"
            " deadline_date = ?"
            " WHERE id = ? AND tg_id = ?",
            (subject, teacher, task, deadline_date, deadline_id, tg_id),
        )
    logger.info("deadline updated", extra={"deadline_id": deadline_id})
    return True


def soft_delete(conn, deadline_id: int, tg_id: int) -> bool:
    """Мягко удалить дедлайн (проставить ``deleted_at``).

    Физического удаления нет: :func:`list_active` такие записи не показывает.

    Returns:
        True, если запись была активной и помечена удалённой.
    """
    if get(conn, deadline_id, tg_id) is None:
        return False
    with transaction(conn):
        conn.execute(
            "UPDATE deadlines SET deleted_at = ? WHERE id = ? AND tg_id = ?",
            (now_iso(), deadline_id, tg_id),
        )
    logger.info("deadline soft-deleted", extra={"deadline_id": deadline_id})
    return True
def days_left(date_iso: str | None, today: date | None = None) -> int | None:
    """Сколько дней осталось до срока.

    Args:
        date_iso: срок в ISO или None/пусто.
        today: база отсчёта (по умолчанию — сегодня в поясе техникума).

    Returns:
        Число дней (отрицательное — просрочено), либо None, если даты нет
        или она не разбирается.
    """
    if not date_iso:
        return None
    base = today or datetime.now(TIMEZONE).date()
    try:
        target = date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return None
    return (target - base).days


def urgency_of(days: int | None) -> tuple[str, str]:
    """Группа срочности для числа дней.

    Returns:
        Пара ``(эмодзи, заголовок)`` из :data:`URGENCY_ORDER`.
    """
    if days is None:
        return URGENCY_NO_DATE
    if days < 0:
        return URGENCY_OVERDUE
    if days == 0:
        return URGENCY_TODAY
    if days == 1:
        return URGENCY_TOMORROW
    if days <= 7:
        return URGENCY_WEEK
    return URGENCY_LATER


def group_by_urgency(items: list[dict],
                     today: date | None = None) -> list[tuple[str, str, list[dict]]]:
    """Разложить дедлайны по группам срочности.

    Args:
        items: список словарей из :func:`list_active`.
        today: база отсчёта (для тестов).

    Returns:
        Список ``(эмодзи, заголовок, [элементы])`` для НЕПУСТЫХ групп,
        в порядке :data:`URGENCY_ORDER`. Каждому элементу добавляется ключ
        ``days_left``.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        enriched = dict(item)
        remaining = days_left(item.get("deadline_date"), today)
        enriched["days_left"] = remaining
        buckets.setdefault(urgency_of(remaining), []).append(enriched)

    return [
        (emoji, title, buckets[(emoji, title)])
        for emoji, title in URGENCY_ORDER
        if buckets.get((emoji, title))
    ]


def parse_manual_date(text: str, today: date | None = None) -> str | None:
    """Разобрать дату, введённую вручную.

    Поддерживаются форматы (разделитель — точка, дефис или слэш):

    - ``ДД.ММ.ГГГГ`` — как есть;
    - ``ДД.ММ.ГГ`` — двузначный год превращается в 20xx;
    - ``ДД.ММ`` — ближайший такой день: если он уже прошёл в текущем году,
      берётся следующий год.

    Год для короткого формата подставляется вручную, а не через ``%d.%m``:
    ``strptime`` с таким форматом в Python 3.14 выдаёт DeprecationWarning
    (неоднозначность с 29 февраля), а нам нужен предсказуемый текущий год.

    Args:
        text: ввод пользователя.
        today: база (для тестов).

    Returns:
        ISO-дата или None, если разобрать не удалось.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    base = today or datetime.now(TIMEZONE).date()

    if _FULL_DATE_RE.match(raw):
        day_s, month_s, year_s = _SEPARATORS_RE.split(raw)
        try:
            year = int(year_s)
            if len(year_s) <= 2:
                year += 2000
            return date(year, int(month_s), int(day_s)).isoformat()
        except ValueError:
            return None

    if _SHORT_DATE_RE.match(raw):
        day_s, month_s = _SEPARATORS_RE.split(raw)
        try:
            day, month = int(day_s), int(month_s)
            candidate = date(base.year, month, day)
        except ValueError:
            return None
        if candidate < base:
            try:
                candidate = candidate.replace(year=base.year + 1)
            except ValueError:
                return None
        return candidate.isoformat()

    return None


def remind_window(conn, today: date | None = None) -> list[dict]:
    """Активные дедлайны со сроком сегодня или завтра (для напоминаний).

    Returns:
        Список словарей с добавленным ключом ``days_left``.
    """
    base = today or datetime.now(TIMEZONE).date()
    rows = conn.execute(
        "SELECT * FROM deadlines"
        " WHERE deleted_at IS NULL AND deadline_date IN (?, ?)",
        (base.isoformat(), (base + timedelta(days=1)).isoformat()),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        item = dict(row)
        item["days_left"] = days_left(item.get("deadline_date"), base)
        result.append(item)
    return result
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")
