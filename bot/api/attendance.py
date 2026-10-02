"""Посещаемость Mini App: сводка за месяц, активная пара, ответ «Да/Нет».

Правила берутся у бота, ничего не дублируется:

- сводка — :func:`bot.attendance.attendance_service.student_month_summary`;
- аттестация — :func:`bot.attendance.attestation_service.get_attestation_summary`;
- активная пара — :func:`bot.attendance.attendance_service.current_para`
  (окно ±5 минут от начала) плюс наличие пары у группы;
- ответ — :func:`bot.attendance.attendance_service.apply_mark`: он проверяет,
  что опрос существует и открыт. Если опрос ещё не создан (цикл опросов идёт
  раз в минуту), ответ всё равно засчитывается — но только внутри окна
  :data:`bot.attendance.models.CHECK_POLL_MINUTES` минут от начала пары и
  только один раз.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from bot.attendance import attendance_db as att
from bot.attendance import attendance_service as att_svc
from bot.attendance import attestation_service as att_est
from bot.attendance import db as att_db
from bot.attendance.models import (
    CHECK_POLL_MINUTES,
    STATUS_ABSENT,
    STATUS_PRESENT,
)
from bot.config import KRASNOYARSK, MIN_ATTESTATION_LESSONS
from bot.services.schedule_service import time_range_for_date

# Ответы, которые понимает фронт.
ANSWER_YES = "yes"
ANSWER_NO = "no"

# Коды ошибок: HTTP-код выбирает роут.
ERR_NOT_IN_GROUP = "not_in_group"
ERR_NO_ACTIVE_LESSON = "no_active_lesson"
ERR_BAD_ANSWER = "bad_answer"
ERR_ALREADY_ANSWERED = "already_answered"
ERR_POLL_CLOSED = "poll_closed"


def month_summary_payload(conn, tg_id: int, group: str,
                          today: date | None = None) -> dict:
    """Сводка посещаемости за месяц + аттестация по предметам → JSON.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        group: группа студента.
        today: дата, определяющая месяц (для тестов).

    Returns:
        ``present``, ``late``, ``absent``, ``excused``, ``total``, ``percent``
        и ``subjects``. По предмету отдаются ДВА разных счётчика, их нельзя
        путать:

        - ``required`` — норматив аттестации
          (:data:`bot.config.MIN_ATTESTATION_LESSONS`), например 3;
        - ``total_lessons`` — сколько всего пар по предмету за месяц было.

        Фронт рисует «5/3 ✅» и рядом «из 8 возможных». ``need_more`` — сколько
        пар не хватает до норматива (0, если уже аттестован).
        ``percent`` — доля зачтённых занятий (был + опоздал) среди всех
        отметок: то же правило, что в боте (``render_my_attendance``).
    """
    summary = att_svc.student_month_summary(conn, tg_id, day=today)
    counts = {str(k): int(v) for k, v in (summary.get("counts") or {}).items()}

    present = counts.get(STATUS_PRESENT, 0)
    late = counts.get("late", 0)
    absent = counts.get(STATUS_ABSENT, 0)
    excused = counts.get("excused", 0)
    total = present + late + absent + excused
    counted = present + late
    percent = round(counted * 100 / total) if total else 0

    period_start, period_end = att_est.period_for_month(today)
    attestation = att_est.get_attestation_summary(
        conn, tg_id, group, period_start, period_end
    )

    return {
        "present": present,
        "late": late,
        "absent": absent,
        "excused": excused,
        "total": total,
        "percent": percent,
        "period_from": period_start.isoformat(),
        "period_to": period_end.isoformat(),
        "subjects": [
            {
                "name": str(item.get("subject") or ""),
                "attended": int(item.get("attended") or 0),
                # Норматив аттестации (MIN_ATTESTATION_LESSONS), НЕ количество
                # прошедших пар: иначе «5 из 5» вместо «5 из 3».
                "required": MIN_ATTESTATION_LESSONS,
                "total_lessons": int(item.get("total_lessons") or 0),
                "is_attested": bool(item.get("is_attested")),
                "need_more": int(item.get("need_more") or 0),
            }
            for item in (attestation.get("items") or [])
        ],
    }
def closes_at_for(day: date, para: int) -> datetime:
    """Момент закрытия окна ответа: начало пары + 5 минут.

    Правило совпадает с :func:`bot.attendance.attendance_service.start_poll`.
    Если начала пары нет (на этот номер нет звонков), возвращается текущий
    момент — окно считается закрытым, отвечать уже поздно.
    """
    start = att_svc.para_start_time(day, para)
    if start is None:
        return datetime.now(KRASNOYARSK)
    return datetime.combine(day, start).replace(
        tzinfo=KRASNOYARSK
    ) + timedelta(minutes=CHECK_POLL_MINUTES)


def active_lesson(conn, group: str, moment: datetime) -> dict | None:
    """Описание идущей пары или None, если пары нет.

    Args:
        conn: соединение SQLite.
        group: группа студента.
        moment: текущий момент в поясе техникума.

    Returns:
        ``{'para', 'subject', 'room', 'time_range', 'closes_at'}`` либо None,
        если по звонкам пары нет или её нет в расписании группы.
    """
    para = att_svc.current_para(moment)
    if para is None:
        return None

    day = moment.date()
    lesson = att_svc.get_lesson_for_para(conn, group, day, para)
    if lesson is None:
        return None

    return {
        "para": para,
        "subject": str(lesson.get("subject") or ""),
        "room": str(lesson.get("room") or ""),
        "time_range": time_range_for_date(para, day),
        "closes_at": closes_at_for(day, para).isoformat(timespec="seconds"),
    }


def answer_of(conn, group: str, day: date, para: int, tg_id: int) -> str | None:
    """Статус отметки студента на пару или None, если он ещё не отвечал.

    Отметки старосты тоже возвращаются: для фронта это «ответ уже есть»,
    изменить его через Mini App нельзя.
    """
    row = att.get_attendance(conn, group, day.isoformat(), para, tg_id)
    if row is None:
        return None
    return str(row["status"])


def is_window_open(moment: datetime, closes_at_iso: str) -> bool:
    """Открыто ли окно ответа на момент ``moment``."""
    try:
        closes_at = datetime.fromisoformat(closes_at_iso)
    except ValueError:
        return False
    if closes_at.tzinfo is None:
        closes_at = closes_at.replace(tzinfo=KRASNOYARSK)
    return moment <= closes_at


def active_payload(conn, tg_id: int, moment: datetime) -> dict:
    """Ответ ``/api/attendance/active``: активная пара и уже данный ответ.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        moment: текущий момент в поясе техникума.

    Returns:
        ``{'active': {...} | None}``. В ``active`` есть ``answered``
        (None | ``present`` | ``absent``) и ``is_open`` — открыто ли окно.
    """
    student = att_db.get_student(conn, tg_id)
    if student is None:
        return {"active": None}

    group = str(student["group_name"])
    active = active_lesson(conn, group, moment)
    if active is None:
        return {"active": None}

    active["answered"] = answer_of(
        conn, group, moment.date(), active["para"], tg_id
    )
    active["is_open"] = is_window_open(moment, active["closes_at"])
    return {"active": active}


def answer_status(answer: str) -> str | None:
    """``yes``/``no`` → статус отметки; None для мусора."""
    if answer == ANSWER_YES:
        return STATUS_PRESENT
    if answer == ANSWER_NO:
        return STATUS_ABSENT
    return None


def submit_answer(conn, tg_id: int, answer: str,
                  moment: datetime | None = None) -> tuple[dict, str | None]:
    """Отметить студента на текущей паре.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        answer: ``yes`` | ``no``.
        moment: текущий момент (для тестов); по умолчанию — сейчас
            в Красноярске.

    Returns:
        ``(payload, error_code)``. При успехе ``error_code is None``,
        а ``payload`` — ``{'ok': True, 'status': 'present'|'absent'}``.
    """
    status = answer_status(answer)
    if status is None:
        return {}, ERR_BAD_ANSWER

    student = att_db.get_student(conn, tg_id)
    if student is None:
        return {}, ERR_NOT_IN_GROUP

    now = moment or datetime.now(KRASNOYARSK)
    group = str(student["group_name"])
    active = active_lesson(conn, group, now)
    if active is None:
        return {}, ERR_NO_ACTIVE_LESSON

    if not is_window_open(now, active["closes_at"]):
        return {}, ERR_POLL_CLOSED
    date_iso = now.date().isoformat()
    if att.attendance_exists(conn, group, date_iso, active["para"], tg_id):
        return {}, ERR_ALREADY_ANSWERED

    # Опрос мог ещё не создаться (цикл опросов идёт раз в минуту): тогда
    # apply_mark откажется, и отметку ставим сами — окно уже проверено.
    result = att_svc.apply_mark(
        conn, group, date_iso, active["para"], tg_id,
        str(student["full_name"]), status=status, marked_by=tg_id,
        moment=now,
    )
    if not result["ok"]:
        if result["error"] == "already_marked":
            return {}, ERR_ALREADY_ANSWERED
        att.mark_attendance(
            conn, group, date_iso, active["para"], tg_id,
            str(student["full_name"]), status=status, marked_by=tg_id,
            method="self", subject=active["subject"],
            now=now.isoformat(timespec="seconds"),
        )
    return {"ok": True, "status": status}, None


__all__ = [
    "ANSWER_NO",
    "ANSWER_YES",
    "ERR_ALREADY_ANSWERED",
    "ERR_BAD_ANSWER",
    "ERR_NOT_IN_GROUP",
    "ERR_NO_ACTIVE_LESSON",
    "ERR_POLL_CLOSED",
    "active_lesson",
    "active_payload",
    "answer_of",
    "answer_status",
    "closes_at_for",
    "is_window_open",
    "month_summary_payload",
    "submit_answer",
]
