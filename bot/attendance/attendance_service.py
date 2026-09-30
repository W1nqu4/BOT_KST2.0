"""Логика посещаемости: пары, опросы, отметки, сводки (этап 2).

Отдельно от :mod:`bot.attendance.service` (группы/роли, этап 1): здесь только
посещаемость. SQL вынесен в :mod:`bot.attendance.attendance_db`, тексты — в
:mod:`bot.attendance.attendance_texts`, чтобы правила читались без разметки.

Ключевые правила (согласованы с владельцем):

- опрос приходит В ЧАТ ГРУППЫ автоматически в начале пары (или по кнопке
  старосты досрочно);
- отметившиеся видны всем — публичный список в сообщении опроса;
- «опоздал» = отметка позже :data:`bot.attendance.models.LATE_AFTER_MINUTES`
  минут после начала пары;
- ``absent`` автоматически НЕ ставится: прогул фиксирует староста вручную.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time

from bot import db as core_db
from bot.attendance import attendance_db as att
from bot.attendance import db as att_db
from bot.attendance.models import (
    ALL_STATUSES,
    LATE_AFTER_MINUTES,
    METHOD_SELF,
    METHOD_STAROSTA,
    STATUS_CYCLE,
    STATUS_PRESENT,
)
from bot.config import (
    BELL_TIMES_SATURDAY,
    BELL_TIMES_WEEKDAY,
    KRASNOYARSK,
)
from bot.services.schedule_service import get_lessons_for_day

logger = logging.getLogger(__name__)

# Насколько раньше начала пары считаем, что пара «идёт» (опрос уходит
# в начале пары, поэтому окно симметричное).
PARA_START_TOLERANCE_MINUTES = 5


def bells_for_day(day: date) -> dict:
    """Звонки нужного дня: в субботу они короче (см. :mod:`bot.config`)."""
    return BELL_TIMES_SATURDAY if day.weekday() == 5 else BELL_TIMES_WEEKDAY


def _to_minutes(value: time) -> int:
    """Время → минуты от начала суток."""
    return value.hour * 60 + value.minute


def _parse_hhmm(raw: str) -> time | None:
    """``'09:00'`` → :class:`datetime.time`; None при мусоре."""
    try:
        hh, mm = str(raw).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        logger.warning("could not parse bell time", extra={"value": str(raw)})
        return None


def para_start_time(day: date, para: int) -> time | None:
    """Время начала пары по звонкам или None, если пары в этот день нет."""
    pair = bells_for_day(day).get(para)
    if not pair:
        return None
    return _parse_hhmm(pair[0])


def para_end_time(day: date, para: int) -> time | None:
    """Время окончания пары по звонкам или None."""
    pair = bells_for_day(day).get(para)
    if not pair:
        return None
    return _parse_hhmm(pair[1])


def current_para(now: datetime | None = None) -> int | None:
    """Номер пары, если сейчас идёт пара (±5 минут от начала).

    Учитывает будни и субботу (в субботу звонки другие, 5 пары нет).
    Большая перемена (13:15 в будни после 3 пары) не считается парой:
    пара «идёт» от начала и до конца по звонкам.

    Args:
        now: текущий момент (для тестов); по умолчанию — сейчас в Красноярске.

    Returns:
        Номер пары 1..5 или None.
    """
    moment = now or datetime.now(KRASNOYARSK)
    today = moment.date()
    now_minutes = moment.hour * 60 + moment.minute

    # Порядок важен: сначала более поздние пары, чтобы 12:50 не поймало
    # субботнюю 3-ю при 4-й... но пары не пересекаются, поэтому достаточно
    # проверить все и вернуть подходящую.
    for para in sorted(bells_for_day(today), reverse=True):
        start = para_start_time(today, para)
        end = para_end_time(today, para)
        if start is None or end is None:
            continue
        start_minutes = _to_minutes(start)
        end_minutes = _to_minutes(end)
        if (start_minutes - PARA_START_TOLERANCE_MINUTES
                <= now_minutes <= end_minutes):
            return para
    return None


def group_has_lesson(conn, group: str, day: date, para: int) -> bool:
    """Есть ли у группы пара с таким номером в этот день (с учётом чётности)."""
    lessons = get_lessons_for_day(conn, group, day)
    return any(int(lesson["para_number"]) == para for lesson in lessons)


def get_lesson_for_para(conn, group: str, day: date,
                        para: int) -> dict | None:
    """Пара группы по номеру или None."""
    for lesson in get_lessons_for_day(conn, group, day):
        if int(lesson["para_number"]) == para:
            return lesson
    return None


def group_chat_for(conn, group: str) -> dict | None:
    """Чат группы из ``group_chats`` (первый подходящий) или None.

    Опрос уходит только в привязанный чат: без ``/setup`` чата у группы нет,
    и слать некуда. Студент всё равно может отметиться через /attendance.
    """
    chats = core_db.get_group_chats_for_group(conn, group)
    return chats[0] if chats else None
def list_day_paras(conn, group: str, day: date) -> list[dict]:
    """Пары группы на день с временем (для /attendance)."""
    rows: list[dict] = []
    for lesson in get_lessons_for_day(conn, group, day):
        para = int(lesson["para_number"])
        start = para_start_time(day, para)
        end = para_end_time(day, para)
        span = (f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
                if start and end else "")
        rows.append({
            "para": para,
            "subject": lesson["subject"],
            "time": span,
        })
    return rows


def status_for_mark(poll: dict, moment: datetime | None = None) -> str:
    """Статус отметки по времени: ``present`` или ``late``.

    «Опоздал» — отметка позже :data:`bot.attendance.models.LATE_AFTER_MINUTES`
    минут после начала опроса (опрос стартует в начале пары).

    Args:
        poll: строка ``attendance_polls``.
        moment: момент отметки (для тестов).

    Returns:
        ``'present'`` или ``'late'``.
    """
    from bot.attendance.models import STATUS_LATE

    now = moment or datetime.now(KRASNOYARSK)
    started_raw = str(poll.get("started_at") or "")
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        logger.warning("could not parse poll started_at",
                       extra={"value": started_raw})
        return STATUS_PRESENT

    if started.tzinfo is None:
        # Старые записи без пояса: считаем по поясу техникума.
        started = started.replace(tzinfo=KRASNOYARSK)
    if (now - started).total_seconds() > LATE_AFTER_MINUTES * 60:
        return STATUS_LATE
    return STATUS_PRESENT
def apply_mark(conn, group: str, date_iso: str, para: int, tg_id: int,
               full_name: str, status: str, marked_by: int,
               method: str = METHOD_SELF,
               moment: datetime | None = None) -> dict:
    """Поставить отметку с проверками.

    Проверки те же, что нужны обработчику кнопки: опрос существует и открыт,
    отметки ещё нет.

    Args:
        conn: соединение SQLite.
        group: группа студента.
        date_iso: дата пары.
        para: номер пары.
        tg_id: студент.
        full_name: ФИО (снимок).
        status: статус отметки.
        marked_by: кто отметил (для self это сам студент).
        method: ``self`` | ``starosta`` | ``vote``.
        moment: момент отметки (для тестов).

    Returns:
        ``{'ok': bool, 'error': str | None, 'status': str}``.
        ``error``: ``bad_status``, ``poll_not_found``, ``poll_closed``,
        ``already_marked``.
    """
    if status not in ALL_STATUSES:
        return {"ok": False, "error": "bad_status", "status": status}

    poll = att.get_poll(conn, group, date_iso, para)
    if poll is None:
        return {"ok": False, "error": "poll_not_found", "status": ""}
    if poll.get("is_closed"):
        return {"ok": False, "error": "poll_closed", "status": ""}
    if att.attendance_exists(conn, group, date_iso, para, tg_id):
        return {"ok": False, "error": "already_marked", "status": ""}

    moment_iso = (moment or datetime.now(KRASNOYARSK)).isoformat(
        timespec="seconds")
    lesson = get_lesson_for_para(conn, group, date.fromisoformat(date_iso),
                                 para)
    att.mark_attendance(conn, group, date_iso, para, tg_id, full_name,
                        status=status, marked_by=marked_by, method=method,
                        subject=str(lesson["subject"]) if lesson else None,
                        now=moment_iso)
    logger.info("attendance marked",
                extra={"group": group, "date": date_iso, "para": para,
                       "tg_id": tg_id, "status": status, "method": method})
    return {"ok": True, "error": None, "status": status}


def absent_students(conn, group: str, date_iso: str,
                    para: int) -> list[dict]:
    """Студенты группы без отметки на пару (для итога опроса)."""
    marks = att.get_attendance_list(conn, group, date_iso, para)
    marked_ids = {int(mark["tg_id"]) for mark in marks}
    return [
        student for student in att_db.get_group_students(conn, group)
        if int(student["tg_id"]) not in marked_ids
    ]


async def start_poll(conn, bot, group: str, day: date, para: int,
                     force: bool = False) -> dict:
    """Отправить в чат группы сообщение-опрос «кто на паре».

    Опрос уходит только в привязанный чат (``group_chats``): иначе слать
    некуда, и студенты отмечаются через /attendance в личке.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        group: группа.
        day: дата пары.
        para: номер пары.
        force: True — запуск по кнопке старосты (досрочно).

    Returns:
        ``{'ok': bool, 'error': str | None, 'poll_id': int | None}``.
        ``error``: ``no_chat``, ``no_lesson``, ``already_exists``,
        ``send_failed``.
    """
    from bot.attendance import attendance_keyboards as att_kb
    from bot.attendance import attendance_texts as atext

    chat = group_chat_for(conn, group)
    if chat is None:
        return {"ok": False, "error": "no_chat", "poll_id": None}

    lesson = get_lesson_for_para(conn, group, day, para)
    if lesson is None:
        return {"ok": False, "error": "no_lesson", "poll_id": None}

    date_iso = day.isoformat()
    if att.poll_exists(conn, group, date_iso, para):
        return {"ok": False, "error": "already_exists", "poll_id": None}

    end = para_end_time(day, para)
    closes_at = (datetime.combine(day, end).replace(tzinfo=KRASNOYARSK)
                 if end else datetime.now(KRASNOYARSK))
    chat_id = int(chat["chat_id"])

    text = atext.render_poll(group, str(lesson["subject"]), day, para, [])
    try:
        message = await bot.send_message(
            chat_id, text, parse_mode="HTML",
            reply_markup=att_kb.poll_kb(date_iso, para),
        )
    except Exception as exc:
        logger.warning("attendance poll send failed",
                       extra={"chat_id": chat_id, "group": group,
                              "error": repr(exc)})
        return {"ok": False, "error": "send_failed", "poll_id": None}

    poll_id = att.create_poll(
        conn, group, date_iso, para, chat_id,
        getattr(message, "message_id", None),
        closes_at.isoformat(timespec="seconds"),
    )
    logger.info("attendance poll started",
                extra={"group": group, "date": date_iso, "para": para,
                       "chat_id": chat_id, "forced": force})
    return {"ok": True, "error": None, "poll_id": poll_id}
async def update_poll_message(conn, bot, poll: dict) -> bool:
    """Обновить сообщение-опрос: список отметившихся.

    Редактирование может не пройти (сообщение удалили, «message is not
    modified») — это не ошибка, поэтому ``TelegramBadRequest`` глушим.

    Returns:
        True, если сообщение отредактировано.
    """
    from aiogram.exceptions import TelegramBadRequest

    from bot.attendance import attendance_texts as atext

    group = str(poll["group_name"])
    date_iso = str(poll["date_iso"])
    para = int(poll["para"])
    day = date.fromisoformat(date_iso)

    lesson = get_lesson_for_para(conn, group, day, para) or {}
    marks = att.get_attendance_list(conn, group, date_iso, para)
    text = atext.render_poll(group, str(lesson.get("subject") or ""), day,
                             para, marks,
                             closed=bool(poll.get("is_closed")))

    message_id = poll.get("message_id")
    if not message_id:
        return False

    try:
        await bot.edit_message_text(
            chat_id=int(poll["chat_id"]), message_id=int(message_id),
            text=text, parse_mode="HTML",
            reply_markup=None if poll.get("is_closed")
            else _poll_kb(date_iso, para),
        )
    except TelegramBadRequest as exc:
        logger.debug("poll message not edited",
                     extra={"error": str(exc), "poll_id": poll.get("id")})
        return False
    except Exception as exc:
        logger.warning("poll message edit failed",
                       extra={"error": repr(exc), "poll_id": poll.get("id")})
        return False
    return True


def _poll_kb(date_iso: str, para: int):
    """Клавиатура опроса (ленивый импорт: избегаем цикла модулей)."""
    from bot.attendance import attendance_keyboards as att_kb

    return att_kb.poll_kb(date_iso, para)


async def finalize_poll_message(conn, bot, poll: dict) -> bool:
    """Показать итог закрытого опроса: кто отметился, кто нет.

    Автоматический ``absent`` НЕ ставится: список «не отметились» — это
    информация для старосты, а прогул он фиксирует сам через ``/mark``.
    """
    from aiogram.exceptions import TelegramBadRequest

    from bot.attendance import attendance_texts as atext

    group = str(poll["group_name"])
    date_iso = str(poll["date_iso"])
    para = int(poll["para"])
    day = date.fromisoformat(date_iso)

    lesson = get_lesson_for_para(conn, group, day, para) or {}
    marks = att.get_attendance_list(conn, group, date_iso, para)
    absent = absent_students(conn, group, date_iso, para)

    text = atext.render_poll_final(group, str(lesson.get("subject") or ""),
                                   day, para, marks, absent)
    message_id = poll.get("message_id")
    if not message_id:
        return False

    try:
        await bot.edit_message_text(
            chat_id=int(poll["chat_id"]), message_id=int(message_id),
            text=text, parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        logger.debug("final poll message not edited",
                     extra={"error": str(exc), "poll_id": poll.get("id")})
        return False
    except Exception as exc:
        logger.warning("final poll message failed",
                       extra={"error": repr(exc), "poll_id": poll.get("id")})
        return False
    return True


async def close_due_polls(conn, bot, now: datetime | None = None) -> int:
    """Закрыть опросы, у которых вышел срок (конец пары).

    Returns:
        Сколько опросов закрыто.
    """
    moment = now or datetime.now(KRASNOYARSK)
    closed = 0

    for poll in att.get_open_polls(conn):
        raw = str(poll.get("closes_at") or "")
        try:
            closes = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("could not parse poll closes_at",
                           extra={"value": raw, "poll_id": poll.get("id")})
            continue
        if closes.tzinfo is None:
            closes = closes.replace(tzinfo=KRASNOYARSK)
        if closes >= moment:
            continue

        att.close_poll(conn, int(poll["id"]))
        closed += 1
        await finalize_poll_message(conn, bot, {**poll, "is_closed": 1})
        logger.info("attendance poll closed",
                    extra={"group": poll["group_name"],
                           "date": poll["date_iso"], "para": poll["para"]})

    return closed
async def tick(conn, bot, now: datetime | None = None) -> dict:
    """Один проход: закрыть истёкшие опросы и голосования, начать опросы.

    Голосования за посещаемость закрываются здесь же: у них своё время жизни
    (начало пары + 45 минут), но отдельная фоновая задача с почти таким же
    периодом была бы лишней — встроено в общий проход.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now: текущий момент (для тестов).

    Returns:
        ``{'closed': N, 'started': M, 'votes_closed': K}``.
    """
    moment = now or datetime.now(KRASNOYARSK)
    today = moment.date()

    closed = await close_due_polls(conn, bot, moment)
    votes_closed = await _close_due_votes(conn, bot, moment)
    started = 0

    para = current_para(moment)
    if para is None:
        return {"closed": closed, "started": 0,
                "votes_closed": votes_closed}

    for group in att_db.get_all_groups(conn):
        group_name = str(group["group_name"])

        # Без привязанного чата опрос слать некуда (group_chats).
        if group_chat_for(conn, group_name) is None:
            continue
        # Пары у группы в этот день/номер нет — опрос не нужен.
        if not group_has_lesson(conn, group_name, today, para):
            continue
        # Один опрос на пару (UNIQUE + проверка): иначе слали бы каждую минуту.
        if att.poll_exists(conn, group_name, today.isoformat(), para):
            continue

        result = await start_poll(conn, bot, group_name, today, para)
        if result["ok"]:
            started += 1

    if closed or started or votes_closed:
        logger.info("attendance tick",
                    extra={"date": today.isoformat(), "para": para,
                           "closed": closed, "started": started,
                           "votes_closed": votes_closed})
    return {"closed": closed, "started": started,
            "votes_closed": votes_closed}


async def _close_due_votes(conn, bot, moment: datetime) -> int:
    """Закрыть истёкшие голосования (ленивый импорт: избегаем цикла)."""
    from bot.attendance import vote_service

    try:
        return await vote_service.close_due_vote_polls(conn, bot, moment)
    except Exception:
        # Ошибка голосований не должна ронять закрытие опросов.
        logger.exception("closing due vote polls failed")
        return 0


def month_bounds(day: date) -> tuple[date, date]:
    """Первое и последнее число месяца для даты."""
    import calendar

    last_day = calendar.monthrange(day.year, day.month)[1]
    return date(day.year, day.month, 1), date(day.year, day.month, last_day)


def week_bounds(day: date) -> tuple[date, date]:
    """Понедельник и воскресенье недели, в которую попадает дата."""
    from datetime import timedelta

    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def cycle_status(current: str | None) -> str:
    """Следующий статус по циклу (для кнопки старосты в /mark).

    Порядок: present → late → absent → excused → present.
    """
    if current not in STATUS_CYCLE:
        return STATUS_CYCLE[0]
    index = STATUS_CYCLE.index(str(current))
    return STATUS_CYCLE[(index + 1) % len(STATUS_CYCLE)]


def list_mark_students(conn, group: str, date_iso: str,
                       para: int) -> list[dict]:
    """Студенты группы с текущим статусом отметки (для /mark)."""
    marks = {int(mark["tg_id"]): str(mark["status"])
             for mark in att.get_attendance_list(conn, group, date_iso, para)}
    return [
        {
            "tg_id": int(student["tg_id"]),
            "full_name": str(student["full_name"]),
            "status": marks.get(int(student["tg_id"])),
        }
        for student in att_db.get_group_students(conn, group)
    ]


def set_status(conn, group: str, date_iso: str, para: int, tg_id: int,
               full_name: str, status: str, marked_by: int) -> dict:
    """Поставить статус вручную (староста) — без проверок опроса.

    Староста отмечает и без активного опроса: например, после пары, когда
    опрос уже закрыт. Поэтому :func:`apply_mark` (с проверкой опроса)
    здесь не подходит.
    """
    if status not in ALL_STATUSES:
        return {"ok": False, "error": "bad_status"}

    lesson = get_lesson_for_para(conn, group, date.fromisoformat(date_iso),
                                 para)
    att.mark_attendance(conn, group, date_iso, para, tg_id, full_name,
                        status=status, marked_by=marked_by,
                        method=METHOD_STAROSTA,
                        subject=str(lesson["subject"]) if lesson else None)
    logger.info("attendance set manually",
                extra={"group": group, "date": date_iso, "para": para,
                       "tg_id": tg_id, "status": status})
    return {"ok": True, "error": None}
def month_summary_for_period(conn, tg_id: int, group: str,
                             period_start: date,
                             period_end: date | None = None) -> dict:
    """Сводка посещаемости студента за произвольный период.

    Отличие от :func:`student_month_summary`: период задаёт вызывающий код
    (нужно для кнопки «📅 Прошлый месяц»), а группа передаётся явно — не
    приходится ещё раз ходить за ней в БД.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        group: группа студента.
        period_start: начало периода.
        period_end: конец периода (включительно); None — только start.

    Returns:
        ``{'month_title': str, 'counts': {status: N},
        'by_subject': [{'subject', 'present', 'total'}]}``.
    """
    from bot.attendance import attendance_texts as atext

    end = period_end or period_start
    rows = att.get_attendance_for_student(conn, tg_id, period_start.isoformat(),
                                          end.isoformat())

    counts = {status: 0 for status in STATUS_CYCLE}
    subjects: dict[str, dict] = {}

    for row in rows:
        status = str(row["status"])
        if status in counts:
            counts[status] += 1

        row_day = date.fromisoformat(str(row["date_iso"]))
        lesson = get_lesson_for_para(conn, group, row_day, int(row["para"]))
        subject = str(lesson.get("subject") if lesson else "Без предмета")
        item = subjects.setdefault(subject, {"subject": subject,
                                            "present": 0, "total": 0})
        item["total"] += 1
        if status == STATUS_PRESENT:
            item["present"] += 1

    by_subject = sorted(subjects.values(), key=lambda item: item["subject"])
    return {
        # Заголовок месяца — по началу периода: для прошлого месяца это его
        # собственный месяц, для текущего — текущий.
        "month_title": atext.month_title(period_start),
        "counts": counts,
        "by_subject": by_subject,
    }


def student_month_summary(conn, tg_id: int,
                          day: date | None = None) -> dict:
    """Сводка посещаемости студента за месяц.

    Предмет для каждой отметки берётся из расписания на дату пары (в самой
    отметке предмета нет — так данные не дублируются).

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        day: дата, определяющая месяц (для тестов); по умолчанию — сегодня.

    Returns:
        ``{'month_title': str, 'counts': {status: N},
        'by_subject': [{'subject', 'present', 'total'}]}``.
    """
    from bot.attendance import attendance_texts as atext

    base = day or datetime.now(KRASNOYARSK).date()
    start, end = month_bounds(base)
    rows = att.get_attendance_for_student(conn, tg_id, start.isoformat(),
                                          end.isoformat())

    counts = {status: 0 for status in STATUS_CYCLE}
    subjects: dict[str, dict] = {}

    student = att_db.get_student(conn, tg_id)
    group = str(student["group_name"]) if student else ""

    for row in rows:
        status = str(row["status"])
        if status in counts:
            counts[status] += 1

        row_day = date.fromisoformat(str(row["date_iso"]))
        lesson = get_lesson_for_para(conn, group, row_day, int(row["para"]))
        subject = str(lesson.get("subject") if lesson else "Без предмета")
        item = subjects.setdefault(subject, {"subject": subject,
                                             "present": 0, "total": 0})
        item["total"] += 1
        if status == STATUS_PRESENT:
            item["present"] += 1

    by_subject = sorted(subjects.values(), key=lambda item: item["subject"])
    return {
        "month_title": atext.month_title(base),
        "counts": counts,
        "by_subject": by_subject,
    }


def group_week_report(conn, group: str, day: date | None = None,
                      truant_threshold: int = 1) -> dict:
    """Отчёт за неделю: прогульщики и отличники.

    Args:
        conn: соединение SQLite.
        group: группа.
        day: дата внутри недели (для тестов); по умолчанию — сегодня.
        truant_threshold: сколько прогулов считаем проблемой.

    Returns:
        ``{'week_title': str, 'truants': [...], 'good': [...]}``.
    """
    from bot.attendance import attendance_texts as atext
    from bot.attendance.models import STATUS_ABSENT as _ABSENT

    base = day or datetime.now(KRASNOYARSK).date()
    start, end = week_bounds(base)
    rows = att.get_attendance_for_group_period(conn, group,
                                               start.isoformat(),
                                               end.isoformat())

    absent_counts: dict[str, int] = {}
    present_counts: dict[str, int] = {}
    for row in rows:
        name = str(row["full_name"])
        if str(row["status"]) == _ABSENT:
            absent_counts[name] = absent_counts.get(name, 0) + 1
        else:
            present_counts[name] = present_counts.get(name, 0) + 1

    truants = sorted(
        ({"full_name": name, "absent": count}
         for name, count in absent_counts.items()
         if count >= truant_threshold),
        key=lambda item: (-item["absent"], item["full_name"]),
    )
    # «Отличная посещаемость»: есть отметки и ни одного прогула.
    good = sorted(name for name in present_counts if name not in absent_counts)

    return {
        "week_title": atext.week_title(start, end),
        "truants": truants,
        "good": good,
    }