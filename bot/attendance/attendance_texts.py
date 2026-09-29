"""Тексты посещаемости (этап 2).

Вынесены из обработчиков: сюда попадают и сообщения-опросы в чат, и сводки
в личке, и отчёты старосты. Все тексты — HTML (``parse_mode="HTML"``), поэтому
данные из БД экранируются вызывающим кодом.
"""
from __future__ import annotations

from html import escape

from bot.attendance.models import (
    STATUS_ABSENT,
    STATUS_EXCUSED,
    STATUS_LATE,
    STATUS_PRESENT,
)

# Иконки статусов: единый язык во всех экранах.
STATUS_ICONS = {
    STATUS_PRESENT: "✅",
    STATUS_LATE: "⏰",
    STATUS_ABSENT: "❌",
    STATUS_EXCUSED: "📝",
}

STATUS_LABELS = {
    STATUS_PRESENT: "Присутствовал",
    STATUS_LATE: "Опоздал",
    STATUS_ABSENT: "Пропустил",
    STATUS_EXCUSED: "По уважительной",
}

# Шапка опроса в чате.
POLL_HINT = "Кто на паре? Жми галочку."
POLL_EMPTY = "<i>Пока никто не отметился.</i>"
POLL_BUTTON = "✅ Я на паре"

# Ответы на нажатие (alert).
ALERT_NOT_REGISTERED = "Ты не зарегистрирован в группе"
ALERT_POLL_CLOSED = "Опрос уже закрыт"
ALERT_ALREADY_MARKED = "Ты уже отметился"
ALERT_MARKED = "✅ Отмечен"
ALERT_MARKED_LATE = "✅ Отмечен (опоздал)"

# /attendance вне пары.
NO_PARA_NOW = (
    "Сейчас пар нет. Отметки доступны только во время пары.\n\n"
    "<i>Опрос приходит в чат группы автоматически в начале пары.</i>"
)
NO_PARA_IN_SCHEDULE = "🤔 У твоей группы сегодня нет пар по расписанию."
NEED_GROUP = (
    "🤷 Ты пока не в группе. Введи код от старосты: "
    "«📊 Моя группа» → «🔢 Ввести код»."
)

# /my_attendance.
NO_ATTENDANCE_YET = (
    "📊 <b>Моя посещаемость</b>\n\n"
    "Пока нет отметок за этот месяц."
)

# /report_week.
NO_REPORT_DATA = (
    "📊 <b>Отчёт за неделю</b>\n\n"
    "За эту неделю отметок нет."
)

MANAGE_DENIED = "⛔ Доступно только старосте."
MARK_NEED_ARGS = (
    "Формат: <code>/mark дата пара</code>\n"
    "Например: <code>/mark 2026-09-30 2</code> "
    "или <code>/mark сегодня 2</code>"
)
MARK_NO_LESSON = "🤔 У группы нет пары с таким номером в этот день."
MARK_NO_STUDENTS = "🤔 В группе пока нет студентов."


def render_poll(group: str, subject: str, day, para: int, marks: list[dict],
                closed: bool = False) -> str:
    """Сообщение-опрос со списком отметившихся."""
    header = (
        f"📚 <b>{escape(subject)}</b> · {para} пара · "
        f"{day.strftime('%d.%m')}"
    )
    if closed:
        header = f"🔒 {header}"

    if not marks:
        return f"{header}\n\n{POLL_EMPTY}\n\n{POLL_HINT}"

    lines = [header, ""]
    for mark in marks:
        icon = STATUS_ICONS.get(str(mark.get("status")), "•")
        lines.append(f"{icon} {escape(str(mark['full_name']))}")
    lines.extend(["", POLL_HINT])
    return "\n".join(lines)


def render_poll_final(group: str, subject: str, day, para: int,
                      marks: list[dict], absent: list[dict]) -> str:
    """Итог опроса: кто отметился и кто нет."""
    header = (
        f"🔒 <b>Опрос закрыт</b>\n"
        f"📚 {escape(subject)} · {para} пара · {day.strftime('%d.%m')}"
    )
    lines = [header, ""]

    lines.append(f"✅ <b>Отметились ({len(marks)}):</b>")
    if marks:
        for mark in marks:
            icon = STATUS_ICONS.get(str(mark.get("status")), "•")
            lines.append(f"  {icon} {escape(str(mark['full_name']))}")
    else:
        lines.append("  <i>никто</i>")

    lines.extend(["", f"❌ <b>Не отметились ({len(absent)}):</b>"])
    if absent:
        for student in absent:
            lines.append(f"  • {escape(str(student['full_name']))}")
    else:
        lines.append("  <i>все отметились</i>")

    return "\n".join(lines)
def render_my_attendance(month_title: str, counts: dict,
                         by_subject: list[dict]) -> str:
    """Сводка «Моя посещаемость» за месяц.

    Args:
        month_title: заголовок месяца («Сентябрь 2026»).
        counts: ``{status: количество}``.
        by_subject: ``[{'subject': ..., 'present': N, 'total': M}]``.

    Returns:
        HTML-текст сообщения.
    """
    lines = [
        "📊 <b>Моя посещаемость</b>",
        "",
        f"📅 {escape(month_title)}",
        "",
        f"{STATUS_ICONS[STATUS_PRESENT]} Присутствовал: "
        f"<b>{counts.get(STATUS_PRESENT, 0)}</b>",
        f"{STATUS_ICONS[STATUS_LATE]} Опоздал: "
        f"<b>{counts.get(STATUS_LATE, 0)}</b>",
        f"{STATUS_ICONS[STATUS_ABSENT]} Пропустил: "
        f"<b>{counts.get(STATUS_ABSENT, 0)}</b>",
        f"{STATUS_ICONS[STATUS_EXCUSED]} По уважительной: "
        f"<b>{counts.get(STATUS_EXCUSED, 0)}</b>",
    ]

    if by_subject:
        lines.extend(["", "По предметам:"])
        for item in by_subject:
            lines.append(
                f"• {escape(str(item['subject']))}: "
                f"{item['present']}/{item['total']} "
                f"{STATUS_ICONS[STATUS_PRESENT]}"
            )
    return "\n".join(lines)


def render_day_attendance(day, rows: list[dict]) -> str:
    """Список пар дня со статусом отметки (для /attendance).

    Args:
        day: дата.
        rows: ``[{'para': N, 'subject': str, 'time': str, 'status': str|None}]``.

    Returns:
        HTML-текст сообщения.
    """
    lines = [f"🗓 <b>Пары на {day.strftime('%d.%m.%Y')}</b>", ""]
    if not rows:
        lines.append("<i>Пар нет.</i>")
        return "\n".join(lines)

    for row in rows:
        icon = STATUS_ICONS.get(str(row.get("status") or ""), "•")
        lines.append(
            f"{icon} <b>{row['para']} пара</b> · {escape(str(row['subject']))}"
            f" · {escape(str(row['time']))}"
        )
    return "\n".join(lines)


def render_week_report(group: str, week_title: str, truants: list[dict],
                       good: list[str]) -> str:
    """Отчёт за неделю для старосты.

    Args:
        group: группа.
        week_title: «23-29 сентября».
        truants: ``[{'full_name': ..., 'absent': N}]`` — прогульщики.
        good: ФИО с отличной посещаемостью.

    Returns:
        HTML-текст сообщения.
    """
    lines = [
        f"📊 <b>Отчёт за неделю ({escape(week_title)})</b>",
        f"🎓 {escape(group)}",
        "",
        "🔴 <b>Прогулы:</b>",
    ]
    if truants:
        for item in truants:
            lines.append(
                f"  • {escape(str(item['full_name']))} — {item['absent']} пар"
            )
    else:
        lines.append("  <i>нет</i>")

    lines.extend(["", "✅ <b>Отличная посещаемость:</b>"])
    if good:
        for name in good:
            lines.append(f"  • {escape(name)}")
    else:
        lines.append("  <i>пока никого</i>")

    return "\n".join(lines)


def render_mark_list(group: str, day, para: int, subject: str,
                     students: list[dict]) -> str:
    """Сообщение ручной отметки (/mark) со списком студентов."""
    lines = [
        "✏️ <b>Ручная отметка</b>",
        f"🎓 {escape(group)} · {escape(subject)} · {para} пара · "
        f"{day.strftime('%d.%m')}",
        "",
        "<i>Нажми на студента, чтобы сменить статус: ✅ → ⏰ → ❌ → 📝.</i>",
        "",
    ]
    if not students:
        lines.append(MARK_NO_STUDENTS)
        return "\n".join(lines)

    for student in students:
        icon = STATUS_ICONS.get(str(student.get("status") or ""), "➖")
        lines.append(f"{icon} {escape(str(student['full_name']))}")
    return "\n".join(lines)


def mark_button_label(status: str | None) -> str:
    """Подпись кнопки студента в /mark: иконка текущего статуса или ➖."""
    return STATUS_ICONS.get(str(status or ""), "➖")


def month_title(day) -> str:
    """Заголовок месяца по-русски: «Сентябрь 2026»."""
    months = (
        "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    )
    return f"{months[day.month]} {day.year}"


def week_title(start, end) -> str:
    """Заголовок недели: «23-29 сентября»."""
    months_genitive = (
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    if start.month == end.month:
        return f"{start.day}-{end.day} {months_genitive[end.month]}"
    return (f"{start.day} {months_genitive[start.month]} — "
            f"{end.day} {months_genitive[end.month]}")