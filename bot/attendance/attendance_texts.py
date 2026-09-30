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
from bot.config import MIN_ATTESTATION_LESSONS
from bot.utils.text import plural_ru

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

# --- опрос «Да/Нет» (миграция 14) ---
CHECK_BUTTON_YES = "✅ Я на паре"
CHECK_BUTTON_NO = "❌ Меня нет"
ALERT_CHECK_YES = "✅ Отмечен как присутствующий"
ALERT_CHECK_NO = "❌ Отмечен как отсутствующий"
ALERT_CHECK_ALREADY = "Ты уже ответил"
ALERT_CHECK_CLOSED = "Опрос уже закрыт"
ALERT_NOT_IN_GROUP = "Ты не в группе"

# Экран «Режим посещаемости» (староста).
BTN_ATT_MODE = "📊 Режим посещаемости"
MODE_DENIED = "⛔ Режим меняет только староста."
MODE_CHANGED_CHAT = (
    "✅ Режим изменён на «Чат группы».\n\n"
    "Со следующей пары бот будет присылать опрос в чат группы."
)
MODE_CHANGED_DIRECT = (
    "✅ Режим изменён на «Личка».\n\n"
    "Со следующей пары бот будет присылать опросы в личку."
)

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
                         by_subject: list[dict],
                         attestation: dict | None = None,
                         group: str = "") -> str:
    """Сводка «Моя посещаемость» за месяц с блоком аттестации.

    Args:
        month_title: заголовок месяца («Октябрь 2026»).
        counts: ``{status: количество}``.
        by_subject: ``[{'subject', 'present', 'total'}]`` (оставлено для
            совместимости с прежним вызовом; в новом макете не выводится).
        attestation: результат
            :func:`bot.attendance.attestation_service.get_attestation_summary`
            или None — тогда блок аттестации не показывается.
        group: группа для шапки («25КАД»).

    Returns:
        HTML-текст сообщения.
    """
    header = "📊 <b>Моя посещаемость</b>"
    if group:
        header += f"\n🎓 {escape(group)} · {escape(month_title)}"
    else:
        header += f"\n🎓 {escape(month_title)}"

    lines = [
        header,
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

    if attestation and attestation.get("items"):
        lines.extend(["", "━━━━━━━━━━━━━━━━", ""])
        lines.extend(render_attestation_block(attestation))

    return "\n".join(lines)


def render_attestation_block(attestation: dict) -> list[str]:
    """Строки блока «Аттестация по предметам».

    Args:
        attestation: результат ``get_attestation_summary``.

    Returns:
        Список строк (без завершающего перевода строки).

    Note:
        Порог берётся из :data:`bot.config.MIN_ATTESTATION_LESSONS`, а не из
        результата: в подписи нужен именно порог, а не «сколько не хватает»
        (последнее уже посчитано по каждому предмету отдельно).
    """
    threshold = MIN_ATTESTATION_LESSONS
    lines = [
        "⚠️ <b>Аттестация по предметам</b>",
        f"<i>Минимум {threshold} "
        f"{plural_ru(threshold, 'пара', 'пары', 'пар')} "
        f"по предмету за месяц</i>",
        "",
    ]

    for item in attestation["items"]:
        attended = int(item["attended"])
        subject = escape(str(item["subject"]))

        if item["is_attested"]:
            lines.append(f"✅ {subject} — {attended}/{threshold}")
        elif attended:
            lines.append(
                f"⚠️ {subject} — {attended}/{threshold} "
                f"(нужно ещё {item['need_more']})"
            )
        else:
            lines.append(f"❌ {subject} — 0/{threshold}")
    return lines


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
                       good: list[str],
                       attestation: dict | None = None) -> str:
    """Отчёт за неделю для старосты.

    Args:
        group: группа.
        week_title: «23-29 сентября».
        truants: ``[{'full_name': ..., 'absent': N}]`` — прогульщики.
        good: ФИО с отличной посещаемостью.
        attestation: результат
            :func:`bot.attendance.attestation_service.get_group_attestation_report`
            или None — тогда блоки аттестации не показываются.

    Returns:
        HTML-текст сообщения.
    """
    lines = [
        f"📊 <b>Отчёт за неделю ({escape(week_title)})</b>",
        f"🎓 {escape(group)}",
        "",
    ]

    at_risk = (attestation or {}).get("at_risk") or []
    excellent = (attestation or {}).get("excellent") or []

    if at_risk:
        lines.append("🔴 <b>Под угрозой неаттестации:</b>")
        for item in at_risk:
            lines.append(f"  • {escape(str(item['full_name']))} — "
                         f"{_format_risks(item['subjects'])}")
        lines.append("")

    if excellent:
        lines.append("✅ <b>Отличная посещаемость:</b>")
        for item in excellent:
            lines.append(f"  • {escape(str(item['full_name']))} — "
                         f"все предметы ✅")
        lines.append("")

    lines.append("🔴 <b>Прогулы:</b>")
    if truants:
        for item in truants:
            absent = int(item["absent"])
            word = plural_ru(absent, "пара", "пары", "пар")
            lines.append(
                f"  • {escape(str(item['full_name']))} — {absent} {word}"
            )
    else:
        lines.append("  <i>нет</i>")

    lines.extend(["", "✅ <b>Отличная посещаемость за неделю:</b>"])
    if good:
        for name in good:
            lines.append(f"  • {escape(name)}")
    else:
        lines.append("  <i>пока никого</i>")

    return "\n".join(lines)


def _format_risks(subjects: list[dict]) -> str:
    """Сжать проблемные предметы в строку: «История 2/3, Литература 1/3».

    Нужно отчёту старосты: по каждому студенту видно, где именно он
    проседает по аттестации, но всё в одну строку — иначе отчёт группы
    распухает на пол-экрана.
    """
    threshold = MIN_ATTESTATION_LESSONS
    parts = []
    for item in subjects:
        subject = escape(str(item["subject"]))
        if int(item["attended"]) == 0:
            parts.append(f"{subject} 0/{threshold}")
        else:
            parts.append(f"{subject} {item['attended']}/{threshold}")
    return ", ".join(parts)


def render_truant_attestation(subjects: list[dict]) -> str:
    """Блок «Под угрозой неаттестации» для личного предупреждения.

    Args:
        subjects: ``at_risk`` из ``get_attestation_summary``.

    Returns:
        HTML-блок строк или пустая строка, если проблем нет.
    """
    if not subjects:
        return ""

    threshold = MIN_ATTESTATION_LESSONS
    # Пустая строка перед блоком: предупреждение о прогулах заканчивается
    # ссылкой на /my_attendance, и блок аттестации должен идти отдельно.
    lines = ["", "", "⚠️ <b>Под угрозой неаттестации:</b>"]
    for item in subjects:
        attended = int(item["attended"])
        subject = escape(str(item["subject"]))
        if attended:
            lines.append(
                f"• {subject} — {attended}/{threshold} "
                f"(нужно ещё {item['need_more']})"
            )
        else:
            lines.append(f"• {subject} — 0/{threshold}")
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


def render_check_poll(subject: str, day, para: int, time_range: str,
                      direct: bool = False) -> str:
    """Опрос «Да/Нет» в начале пары (миграция 14).

    Args:
        subject: название предмета.
        day: дата пары.
        para: номер пары.
        time_range: интервал пары («09:00-10:35»).
        direct: True — сообщение уходит студенту в личку (обращение на «ты»
            с вопросом), False — в чат группы.

    Returns:
        HTML-текст сообщения.
    """
    header = f"📚 <b>{escape(subject)}</b> · {para} пара"
    if time_range:
        header += f"\n⏰ {escape(time_range)}"
    question = "Ты на паре?" if direct else "Ты на паре? Жми свою кнопку:"
    return f"{header}\n\n{question}"


def render_check_final(subject: str, day, para: int, present: int,
                       absent: int) -> str:
    """Итог опроса «Да/Нет» в чате группы.

    Args:
        subject: предмет.
        day: дата пары.
        para: номер пары.
        present: сколько были.
        absent: сколько прогуляли (ответили «нет» и не ответили).

    Returns:
        HTML-текст сообщения.
    """
    header = (f"🔒 <b>Опрос закрыт</b>\n"
              f"📚 {escape(subject)} · {para} пара · "
              f"{day.strftime('%d.%m')}")
    return (f"{header}\n\n"
            f"✅ Были: {present}\n"
            f"❌ Прогуляли: {absent}\n\n"
            f"Подробнее — /my_attendance")


def render_check_personal(subject: str, day, para: int,
                          status: str | None) -> str:
    """Итог опроса «Да/Нет» студенту в личку.

    Args:
        subject: предмет.
        day: дата пары.
        para: номер пары.
        status: ``present`` | ``absent`` | None (не ответил).

    Returns:
        HTML-текст сообщения.
    """
    if status == STATUS_PRESENT:
        mark = "✅ был"
    elif status == STATUS_ABSENT:
        mark = "❌ прогулял"
    elif status == STATUS_EXCUSED:
        mark = "📝 по уважительной"
    elif status == STATUS_LATE:
        mark = "⏰ опоздал"
    else:
        mark = "⚠️ не ответил — записан прогул"

    return (
        f"🔒 Опрос по <b>{escape(subject)}</b> закрыт\n"
        f"📅 {day.strftime('%d.%m')} · {para} пара\n\n"
        f"Ты: {mark}\n\n"
        f"Если что-то не так — попроси старосту отметить вручную."
    )


def attendance_mode_screen(current: str) -> str:
    """Экран «Режим посещаемости» для старосты.

    Args:
        current: текущий режим (``chat`` или ``direct``).

    Returns:
        HTML-текст экрана.
    """
    from bot.attendance.models import MODE_DIRECT

    label = "Личка" if current == MODE_DIRECT else "Чат группы"
    return (
        "📊 <b>Режим посещаемости</b>\n\n"
        "Как отмечать пары?\n\n"
        "📱 <b>Личка</b> — каждому студенту в личку\n"
        "💬 <b>Чат группы</b> — опрос в чат группы\n\n"
        f"Текущий: <b>{label}</b>"
    )


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