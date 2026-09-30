"""Аттестация по посещаемости: сколько пар студент отходил по каждому предмету.

Логика: предмет аттестован, если студент «заработал» по нему не меньше
:data:`bot.config.MIN_ATTESTATION_LESSONS` пар за период. Оценок здесь нет и
быть не должно — считаются только посещения.

Какие статусы считаются посещением:

- ``present`` — был;
- ``late`` — был, пусть и с опозданием;
- ``excused`` — уважительная причина: пропуск законный, но в зачёт НЕ идёт
  (на паре студента не было, поэтому и в счётчик аттестации он не попадает);
- ``absent`` — не был.

Предмет берётся из самой отметки (``attendance.subject``, миграция 12), а
если там пусто (записи до миграции) — из расписания через
:func:`subject_for_mark` (fallback по ``schedule_cache``).
"""
from __future__ import annotations

import calendar
import logging
from datetime import date

from bot.attendance import attendance_db as att
from bot.attendance.attendance_service import get_lesson_for_para
from bot.attendance.models import STATUS_LATE, STATUS_PRESENT
from bot.config import MIN_ATTESTATION_LESSONS

logger = logging.getLogger(__name__)

# Статусы, которые идут в зачёт аттестации.
ATTENDED_STATUSES = (STATUS_PRESENT, STATUS_LATE)

# Подпись для отметок без распознанного предмета.
UNKNOWN_SUBJECT = "Без предмета"


def period_for_month(now: date | None = None,
                     offset_months: int = 0) -> tuple[date, date]:
    """Границы месяца со сдвигом на ``offset_months``.

    Для текущего месяца (``offset_months=0``) конец периода — сегодня, а не
    последнее число: показывать «будущие» пропуски нельзя. Для прошлых
    месяцев период закрыт целиком (1 число → последнее число).

    Args:
        now: базовая дата (для тестов); по умолчанию — сегодня.
        offset_months: 0 — текущий месяц, -1 — прошлый, -2 — позапрошлый.

    Returns:
        Пару ``(первое_число, конец_периода)``.
    """
    from datetime import datetime

    from bot.config import KRASNOYARSK

    base = now or datetime.now(KRASNOYARSK).date()

    month_index = base.year * 12 + (base.month - 1) + offset_months
    year, month = divmod(month_index, 12)
    month += 1

    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    if offset_months == 0:
        # Текущий месяц: считаем по сегодняшний день включительно.
        return start, base
    return start, date(year, month, last_day)


def subject_for_mark(conn, mark: dict) -> str:
    """Предмет отметки: из самой записи или из расписания (fallback).

    Args:
        conn: соединение SQLite.
        mark: строка ``attendance`` (нужны ``group_name``, ``date_iso``,
            ``para``, ``subject``).

    Returns:
        Название предмета или :data:`UNKNOWN_SUBJECT`, если ничего не нашли.
    """
    stored = str(mark.get("subject") or "").strip()
    if stored:
        return stored

    try:
        row_day = date.fromisoformat(str(mark["date_iso"]))
        group = str(mark["group_name"])
        para = int(mark["para"])
    except (ValueError, KeyError, TypeError):
        return UNKNOWN_SUBJECT

    lesson = get_lesson_for_para(conn, group, row_day, para)
    if lesson and lesson.get("subject"):
        return str(lesson["subject"])
    return UNKNOWN_SUBJECT


def get_group_subjects(conn, group_name: str) -> list[str]:
    """Уникальные предметы группы из расписания (отсортированы).

    Args:
        conn: соединение SQLite.
        group_name: группа.

    Returns:
        Список названий предметов; пустой, если расписание ещё не загружено.
    """
    rows = conn.execute(
        "SELECT DISTINCT subject FROM schedule_cache"
        " WHERE group_name = ? AND subject <> ''"
        " ORDER BY subject",
        (group_name,),
    ).fetchall()
    return [str(row["subject"]) for row in rows]


def _empty_item(subject: str) -> dict:
    """Пустая строка аттестации по предмету."""
    return {
        "subject": subject,
        "attended": 0,
        "total_lessons": 0,
        "is_attested": False,
        "need_more": MIN_ATTESTATION_LESSONS,
    }
def get_attestation_summary(conn, tg_id: int, group_name: str,
                            period_start: date,
                            period_end: date) -> dict:
    """Сводка аттестации студента по предметам за период.

    В набор предметов попадают все предметы группы из расписания: предмет,
    по которому студент ни разу не отметился, обязан быть виден как «0/3», а
    не исчезнуть из отчёта.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        group_name: группа студента.
        period_start: начало периода.
        period_end: конец периода (включительно).

    Returns:
        ``{'items': [...], 'attested_count': N, 'total_count': M,
        'at_risk': [...]}``. ``items`` отсортированы по названию предмета,
        ``at_risk`` — только неаттестованные, сначала те, кому нужно больше
        всего пар.
    """
    marks = att.get_attendance_for_student(
        conn, tg_id, period_start.isoformat(), period_end.isoformat()
    )

    items = {subject: _empty_item(subject)
             for subject in get_group_subjects(conn, group_name)}

    for mark in marks:
        subject = subject_for_mark(conn, mark)
        item = items.setdefault(subject, _empty_item(subject))
        # total_lessons — все отметки по предмету (включая пропуски и
        # уважительные): это «3 из 5» из макета. attended — только зачтённые.
        item["total_lessons"] += 1
        if str(mark["status"]) in ATTENDED_STATUSES:
            item["attended"] += 1

    for item in items.values():
        item["is_attested"] = item["attended"] >= MIN_ATTESTATION_LESSONS
        item["need_more"] = max(0, MIN_ATTESTATION_LESSONS - item["attended"])

    ordered = sorted(items.values(), key=lambda item: item["subject"])
    at_risk = sorted(
        (item for item in ordered if not item["is_attested"]),
        key=lambda item: (-item["need_more"], item["subject"]),
    )
    return {
        "items": ordered,
        "attested_count": sum(1 for item in ordered if item["is_attested"]),
        "total_count": len(ordered),
        "at_risk": at_risk,
    }


def get_group_attestation_report(conn, group_name: str, period_start: date,
                                 period_end: date) -> dict:
    """Аттестация всей группы за период (для старосты).

    Args:
        conn: соединение SQLite.
        group_name: группа.
        period_start: начало периода.
        period_end: конец периода.

    Returns:
        ``{'at_risk': [{'full_name', 'tg_id', 'subjects', 'at_risk_count'}],
        'excellent': [{'full_name', 'tg_id'}]}``. ``at_risk`` отсортирован по
        числу проблемных предметов (сначала самые тяжёлые), ``excellent`` —
        по ФИО.
    """
    # Список студентов группы живёт в bot.attendance.db (не в attendance_db:
    # там только отметки и опросы).
    from bot.attendance import db as att_db

    at_risk: list[dict] = []
    excellent: list[dict] = []

    for student in att_db.get_group_students(conn, group_name):
        tg_id = int(student["tg_id"])
        summary = get_attestation_summary(conn, tg_id, group_name,
                                          period_start, period_end)
        full_name = str(student["full_name"])

        if summary["at_risk"]:
            at_risk.append({
                "full_name": full_name,
                "tg_id": tg_id,
                "subjects": summary["at_risk"],
                "at_risk_count": len(summary["at_risk"]),
            })
        elif summary["total_count"]:
            # Отличник — все предметы аттестованы (и предметы вообще есть).
            excellent.append({"full_name": full_name, "tg_id": tg_id})

    at_risk.sort(key=lambda item: (-item["at_risk_count"], item["full_name"]))
    excellent.sort(key=lambda item: item["full_name"])
    return {"at_risk": at_risk, "excellent": excellent}