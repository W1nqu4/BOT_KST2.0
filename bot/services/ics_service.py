"""Генерация .ics-подписки (шаг 9).

**Почему НЕ RRULE.** Чёт/нечет у нас определяется чётностью ЧИСЛА МЕСЯЦА
(не номером недели), поэтому правило повторения ``RRULE:FREQ=WEEKLY;INTERVAL=2``
даёт неверный календарь. Вместо этого генерируются КОНКРЕТНЫЕ ``VEVENT``
на каждый день горизонта (:data:`bot.config.ICS_HORIZON_DAYS`): для каждой
даты берётся её чётность и выбираются подходящие пары.

Особенности формата:

- ``DTSTART``/``DTEND`` — с ``TZID=Asia/Krasnoyarsk``, **без перевода в UTC**:
  клиент сам разбирается с поясом, а в файле время выглядит «как в жизни»;
- ``VTIMEZONE`` для Asia/Krasnoyarsk — один раз в начале файла (UTC+7,
  переходов на летнее время нет);
- ``UID`` стабилен между генерациями (md5 от группы, даты, пары, предмета,
  преподавателя): при обновлении подписки клиент обновляет события, а не
  создаёт дубли;
- строки экранируются (:func:`_ics_escape`) и фолдятся по 75 октетов с учётом
  UTF-8 (:func:`_fold_line`): кириллица занимает 2 байта, резать по байту
  нельзя — получится битый символ.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from bot.config import (
    ICS_ALARM_MINUTES_BEFORE,
    ICS_DEADLINE_ALARM_TIME,
    ICS_HORIZON_DAYS,
    ICS_TIMEZONE,
    ICS_TIMEZONE_NAME,
    TIMEZONE,
)
from bot.db import transaction
from bot.services.deadline_service import days_left
from bot.services.schedule_service import (
    apply_substitutions,
    get_lessons_for_day,
)

logger = logging.getLogger(__name__)

# Предельная длина строки .ics в октетах (RFC 5545: 75, не считая CRLF).
ICS_MAX_OCTETS = 75

# Символы, которые в .ics обязаны быть экранированы обратным слэшем.
ICS_ESCAPE_MAP = {
    "\\": "\\\\",
    ";": "\\;",
    ",": "\\,",
    "\r\n": "\\n",
    "\n": "\\n",
    "\r": "\\n",
}

# Доменные иконки в SUMMARY: клиенты календаря их показывают как есть.
ICON_LESSON = "📚"
ICON_SUBSTITUTION = "🔁"
ICON_CANCELLED = "❌"
ICON_SELF_STUDY = "📖"
ICON_DEADLINE = "📝"


def get_or_create_token(conn, tg_id: int) -> str:
    """Токен подписки пользователя: существующий или новый.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.

    Returns:
        Строка-токен (32 hex-символа).
    """
    row = conn.execute(
        "SELECT token FROM calendar_tokens WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    if row is not None:
        return str(row["token"])

    token = uuid.uuid4().hex
    with transaction(conn):
        conn.execute(
            "INSERT INTO calendar_tokens (tg_id, token, created_at)"
            " VALUES (?, ?, ?)",
            (tg_id, token, datetime.now(TIMEZONE).isoformat(timespec="seconds")),
        )
    logger.info("calendar token created", extra={"tg_id": tg_id})
    return token


def get_tg_id_by_token(conn, token: str) -> int | None:
    """Владелец токена или None, если токен неизвестен/пустой."""
    if not token:
        return None
    row = conn.execute(
        "SELECT tg_id FROM calendar_tokens WHERE token = ?", (token,)
    ).fetchone()
    return int(row["tg_id"]) if row is not None else None


def build_calendar_url(public_base_url: str, token: str) -> str:
    """HTTPS-ссылка на .ics-файл для подписки."""
    base = public_base_url.rstrip("/")
    return f"{base}/calendar/{token}.ics"


def build_webcal_url(public_base_url: str, token: str) -> str:
    """Ссылка ``webcal://`` для iOS/macOS (открывает приложение «Календарь»)."""
    https = build_calendar_url(public_base_url, token)
    if https.startswith("https://"):
        return "webcal://" + https[len("https://"):]
    if https.startswith("http://"):
        return "webcal://" + https[len("http://"):]
def _ics_escape(text: str) -> str:
    """Экранировать значение для .ics.

    RFC 5545 требует экранировать обратный слэш, точку с запятой, запятую и
    переводы строк (последние — как литерал ``\\n``).

    Args:
        text: произвольный текст (предмет, ФИО, кабинет).

    Returns:
        Экранированная строка без переводов строк.
    """
    if text is None:
        return ""
    result = str(text)
    result = result.replace("\\", "\\\\")
    result = result.replace(";", "\\;")
    result = result.replace(",", "\\,")
    result = result.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return result


def _octets(text: str) -> int:
    """Длина строки в октетах (байтах UTF-8)."""
    return len(text.encode("utf-8"))


def _fold_line(line: str) -> str:
    """Свернуть строку .ics по 75 октетов (RFC 5545) с учётом UTF-8.

    Продолжение строки начинается с пробела. Резать по байту нельзя:
    кириллица занимает 2 байта, и разрез посередине даст битый символ,
    поэтому набираем символы, пока влезают в лимит.

    Args:
        line: логическая строка без переводов строк.

    Returns:
        Строка с CRLF и ведущими пробелами в продолжениях.
    """
    if _octets(line) <= ICS_MAX_OCTETS:
        return line

    chunks: list[str] = []
    current = ""
    # Первая строка — 75 октетов; продолжения — 74 (плюс ведущий пробел).
    limit = ICS_MAX_OCTETS
    for char in line:
        if _octets(current + char) > limit:
            chunks.append(current)
            current = char
            limit = ICS_MAX_OCTETS - 1
        else:
            current += char
    if current:
        chunks.append(current)

    return "\r\n ".join(chunks)
def vtimezone_block() -> list[str]:
    """Блок ``VTIMEZONE`` для Asia/Krasnoyarsk (UTC+7, без переходов).

    Летнее время в Красноярске не применяется, поэтому достаточно одного
    блока ``STANDARD`` с одинаковыми смещениями до и после.
    """
    return [
        "BEGIN:VTIMEZONE",
        f"TZID:{ICS_TIMEZONE}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0700",
        "TZOFFSETTO:+0700",
        f"TZNAME:{ICS_TIMEZONE_NAME}",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]


def _lesson_uid(group: str, d: date, lesson: dict) -> str:
    """Стабильный UID занятия.

    UID строится из данных события, поэтому при повторной генерации тот же
    урок получает тот же UID — клиент обновляет событие, а не плодит дубли.
    """
    raw = "|".join([
        group,
        d.isoformat(),
        str(lesson.get("para_number", "")),
        lesson.get("subject") or "",
        lesson.get("teacher") or "",
    ])
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"{digest}@kst-schedule"


def _alarm_block(minutes_before: int) -> list[str]:
    """VALARM: напоминание за N минут до начала."""
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Напоминание о паре",
        f"TRIGGER:-PT{minutes_before}M",
        "END:VALARM",
    ]


def lesson_summary(lesson: dict, group: str) -> str:
    """SUMMARY занятия: иконка, предмет и группа.

    Для замены добавляется «ЗАМЕНА» и прежний предмет, для отменённой пары —
    «ОТМЕНА», для самостоятельной работы — «СР».
    """
    subject = lesson.get("subject") or "Без названия"
    if lesson.get("is_cancelled"):
        return f"{ICON_CANCELLED} ОТМЕНА: {subject} ({group})"
    if lesson.get("is_self_study"):
        return f"{ICON_SELF_STUDY} СР: {subject} ({group})"
    if lesson.get("is_substitution"):
        return f"{ICON_SUBSTITUTION} ЗАМЕНА: {subject} ({group})"
    return f"{ICON_LESSON} {subject} ({group})"


def lesson_description(lesson: dict) -> str:
    """DESCRIPTION занятия: преподаватель и, для замены, прежний предмет."""
    parts: list[str] = []
    teacher = lesson.get("teacher") or ""
    if teacher:
        parts.append(teacher)
    if lesson.get("is_substitution"):
        planned = lesson.get("planned_subject") or ""
        if planned and planned != lesson.get("subject"):
            parts.append(f"Было: {planned}")
    if lesson.get("is_cancelled"):
        parts.append("Пара отменена")
    return "\n".join(parts)


def lesson_times(lesson: dict) -> tuple[str, str] | None:
    """DTSTART/DTEND пары в формате ``YYYYMMDDTHHMMSS`` (местное время).

    Returns:
        Пара строк или None, если время пары неизвестно.
    """
    bells = lesson.get("time_range") or ""
    if not bells or "-" not in bells:
        return None
    start_text, end_text = bells.split("-", 1)
    try:
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
    except ValueError:
        return None
    return (
        start.strftime("%H%M%S"),
        end.strftime("%H%M%S"),
    )


def lesson_event(group: str, d: date, lesson: dict) -> list[str]:
    """Один ``VEVENT`` для занятия.

    Args:
        group: имя группы.
        d: дата занятия.
        lesson: словарь занятия из :func:`get_lessons_for_day` после замен.

    Returns:
        Строки события (без финального CRLF) или пустой список, если у пары
        нет времени.
    """
    times = lesson_times(lesson)
    if times is None:
        return []
    start_hms, end_hms = times
    day_text = d.strftime("%Y%m%d")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{_lesson_uid(group, d, lesson)}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={ICS_TIMEZONE}:{day_text}T{start_hms}",
        f"DTEND;TZID={ICS_TIMEZONE}:{day_text}T{end_hms}",
        f"SUMMARY:{_ics_escape(lesson_summary(lesson, group))}",
    ]
    room = lesson.get("room") or ""
    if room:
        lines.append(f"LOCATION:{_ics_escape(room)}")
    description = lesson_description(lesson)
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    lines.extend(_alarm_block(ICS_ALARM_MINUTES_BEFORE))
    lines.append("END:VEVENT")
    return lines


def deadline_event(item: dict, today: date) -> list[str]:
    """``VEVENT`` на весь день для дедлайна.

    Дедлайн без даты пропускается. Напоминание — накануне в 20:00 местного
    времени (``ICS_DEADLINE_ALARM_TIME``).

    Args:
        item: словарь дедлайна (``id``, ``task``, ``subject``,
            ``deadline_date``).
        today: база отсчёта (для тестов).

    Returns:
        Строки события или пустой список, если даты нет.
    """
    date_iso = item.get("deadline_date")
    if not date_iso:
        return []
    try:
        target = date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return []

    task = item.get("task") or "Дедлайн"
    subject = item.get("subject") or ""
    summary = f"{ICON_DEADLINE} {task}"
    if subject:
        summary += f" — {subject}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:deadline-{item.get('id', 0)}@kst-schedule",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{target.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(target + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_ics_escape(summary)}",
    ]
    teacher = item.get("teacher") or ""
    if teacher:
        lines.append(f"DESCRIPTION:{_ics_escape(teacher)}")

    # Напоминание накануне в указанное время.
    remind_day = target - timedelta(days=1)
    try:
        hour, minute = (int(part) for part in ICS_DEADLINE_ALARM_TIME.split(":"))
    except ValueError:
        hour, minute = 20, 0
    trigger = f"{remind_day.strftime('%Y%m%d')}T{hour:02d}{minute:02d}00"
    lines.extend([
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_ics_escape('Завтра дедлайн: ' + task)}",
        f"TRIGGER;VALUE=DATE-TIME:{trigger}",
        "END:VALARM",
    ])
    lines.append("END:VEVENT")
    return lines


def build_ics(conn, group: str, deadlines: list[dict] | None = None,
              horizon_days: int = ICS_HORIZON_DAYS,
              today: date | None = None) -> str:
    """Собрать .ics-файл для группы.

    Для каждой даты горизонта берётся её чётность
    (:func:`bot.services.schedule_service.week_type_for_date`) и выбираются
    занятия этой даты с наложенными заменами. Конкретные ``VEVENT`` вместо
    ``RRULE`` — потому что чёт/нечет идёт по числу месяца (см. docstring
    модуля).

    Args:
        conn: соединение SQLite.
        group: имя группы.
        deadlines: активные дедлайны пользователя (могут быть пустыми).
        horizon_days: сколько дней вперёд генерировать.
        today: база отсчёта (для тестов).

    Returns:
        Текст .ics (CRLF, UTF-8).
    """
    base = today or datetime.now(TIMEZONE).date()
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//KST Bot//Schedule//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Расписание {_ics_escape(group)}",
        f"X-WR-TIMEZONE:{ICS_TIMEZONE}",
    ]
    lines.extend(vtimezone_block())

    events = 0
    for offset in range(horizon_days + 1):
        day = base + timedelta(days=offset)
        lessons = get_lessons_for_day(conn, group, day)
        lessons = apply_substitutions(conn, lessons, group, day)
        for lesson in lessons:
            event = lesson_event(group, day, lesson)
            if event:
                lines.extend(event)
                events += 1

    for item in deadlines or []:
        event = deadline_event(item, base)
        if event:
            lines.extend(event)
            events += 1

    lines.append("END:VCALENDAR")
    body = "\r\n".join(_fold_line(line) for line in lines) + "\r\n"
    logger.info("ics built", extra={"group": group, "events": events,
                                    "horizon_days": horizon_days})
    return body


def build_test_ics(group: str, minutes_ahead: int = 2,
                   now: datetime | None = None) -> str:
    """Мини-.ics с одним событием через N минут (кнопка «Проверить»).

    Нужен, чтобы пользователь убедился, что подписка/файл открывается, не
    дожидаясь реальной пары. Событие датировано «сейчас + minutes_ahead».

    Args:
        group: имя группы (для заголовка события).
        minutes_ahead: через сколько минут поставить событие.
        now: момент отсчёта (для тестов).

    Returns:
        Текст .ics с одним VEVENT.
    """
    moment = now or datetime.now(TIMEZONE)
    start = moment + timedelta(minutes=minutes_ahead)
    end = start + timedelta(minutes=15)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//KST Bot//Test//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Проверка подписки КСТ",
        f"X-WR-TIMEZONE:{ICS_TIMEZONE}",
    ]
    lines.extend(vtimezone_block())
    lines.extend([
        "BEGIN:VEVENT",
        f"UID:test-{int(moment.timestamp())}@kst-schedule",
        f"DTSTAMP:{stamp}",
        f"DTSTART;TZID={ICS_TIMEZONE}:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={ICS_TIMEZONE}:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_ics_escape('✅ Проверка календаря КСТ (' + group + ')')}",
        f"DESCRIPTION:{_ics_escape('Если видишь это событие — подписка работает.')}",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Проверка календаря",
        "TRIGGER:-PT1M",
        "END:VALARM",
        "END:VEVENT",
    ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"
    return lines
