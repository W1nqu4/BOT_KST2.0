"""Форматирование расписания для VK (простой текст, без HTML).

Собирает сообщения по образцу Telegram-версии
(``bot.handlers.schedule.render_day``): те же иконки состояний пары
📚/🔁/❌/📖 и тот же порядок строк «карточка → предмет → преподаватель →
кабинет → время». Сам модуль Telegram не импортирует: оттуда нужен только
вид текста, а он возвращает HTML — для VK разметка не нужна.

Работа с данными — через ``bot.services.schedule_service``: он не тянет
aiogram (проверено), поэтому VK-бот может им пользоваться напрямую.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from bot.services.schedule_service import (
    get_lessons_for_day,
    time_range_for_date,
    week_type_for_date,
)
from bot_vk import texts

logger = logging.getLogger(__name__)

# Иконки карточек — ровно те же, что в Telegram (bot.handlers.schedule).
ICON_PLANNED = "📚"       # плановая пара
ICON_SUBSTITUTION = "🔁"  # замена
ICON_CANCELLED = "❌"     # отменённая пара
ICON_SELF_STUDY = "📖"    # самостоятельная работа

# Названия дней недели по ISO-номеру (1..7), как в Telegram
# (bot.keyboards.inline.day_name).
DAY_NAMES = {
    1: "Понедельник",
    2: "Вторник",
    3: "Среда",
    4: "Четверг",
    5: "Пятница",
    6: "Суббота",
    7: "Воскресенье",
}


def day_name(d: date) -> str:
    """Название дня недели для даты (пустая строка для неизвестного номера)."""
    return DAY_NAMES.get(d.isoweekday(), "")


def lesson_icon(lesson: dict) -> str:
    """Иконка пары по её состоянию.

    Приоритет как в Telegram: отмена → самостоятельная работа → замена →
    плановая пара.
    """
    if lesson.get("is_cancelled"):
        return ICON_CANCELLED
    if lesson.get("is_self_study"):
        return ICON_SELF_STUDY
    if lesson.get("is_substitution"):
        return ICON_SUBSTITUTION
    return ICON_PLANNED


def lessons_with_substitutions(conn, group: str, target: date) -> list[dict]:
    """Занятия группы на дату с наложенными заменами.

    Импорт :func:`apply_substitutions` сделан внутри функции: так модуль
    остаётся дешёвым при загрузке.

    Args:
        conn: соединение SQLite.
        group: имя группы.
        target: дата.

    Returns:
        Список занятий (см. ``get_lessons_for_day``) с полями замен.
    """
    from bot.services.schedule_service import apply_substitutions

    lessons = get_lessons_for_day(conn, group, target)
    return apply_substitutions(conn, lessons, group, target)


def render_lesson(lesson: dict) -> str:
    """Карточка одной пары в формате VK (простой текст).

    Пример::

        🔁 2 пара
        Физика (было Химия) · Тауснев В.Н. · каб. 316А
        ⏰ 10:45-12:20
    """
    lines = [texts.LESSON_TITLE.format(
        icon=lesson_icon(lesson), para=lesson["para_number"]
    )]

    subject = (lesson.get("subject") or "").strip()
    teacher = (lesson.get("teacher") or "").strip()
    room = (lesson.get("room") or "").strip()

    if lesson.get("is_cancelled"):
        # Отмену показываем текстом, а не пустой строкой.
        lines.append(texts.LESSON_CANCELLED.format(subject=subject))
        return "\n".join(lines)

    details = [part for part in (subject, teacher) if part]
    if room:
        details.append(f"каб. {room}")
    if details:
        lines.append(" · ".join(details))

    if lesson.get("is_self_study"):
        lines.append(texts.LESSON_SELF_STUDY.format(subject=subject))

    planned = (lesson.get("planned_subject") or "").strip()
    if lesson.get("is_substitution") and planned and planned != subject:
        lines.append(texts.LESSON_WAS.format(planned=planned))

    time_range = (lesson.get("time_range") or "").strip()
    if not time_range:
        # Страховка: замена на пару, которой нет в плане, приходит с пустым
        # временем — вычислим его по звонкам.
        time_range = time_range_for_date(lesson["para_number"], None)
    if time_range:
        lines.append(texts.LESSON_TIME.format(time=time_range))

    return "\n".join(lines)


def day_header(group: str, target: date) -> str:
    """Шапка дня: дата, группа, чётность числа."""
    return texts.DAY_HEADER.format(
        day_name=day_name(target),
        date=target.strftime("%d.%m.%Y"),
        group=group,
        day=target.day,
        week_type=week_type_for_date(target),
    )


def render_day(group: str, target: date, lessons: list[dict],
               show_header: bool = True) -> str:
    """Текст расписания на один день.

    Args:
        group: имя группы.
        target: дата.
        lessons: занятия после :func:`lessons_with_substitutions`.
        show_header: добавлять ли шапку (в неделе она есть у каждого дня).

    Returns:
        Текст сообщения. Пустой день — отдельная дружелюбная строка.
    """
    blocks: list[str] = []
    if show_header:
        blocks.append(day_header(group, target))

    if not lessons:
        blocks.append(texts.DAY_EMPTY)
    else:
        blocks.extend(render_lesson(lesson) for lesson in lessons)

    return "\n\n".join(blocks)


def render_week(conn, group: str, start: date, days: int = 6) -> list[str]:
    """Расписание на несколько дней подряд, разбитое на сообщения VK.

    Ограничение длины — требование API VK (лимит сообщения), поэтому неделя
    режется по границе дня, а не по символу: иначе карточка пары разорвалась
    бы посередине.

    Args:
        conn: соединение SQLite.
        group: имя группы.
        start: первый день.
        days: сколько дней показывать (по умолчанию 6 — как в Telegram).

    Returns:
        Список сообщений (минимум одно).
    """
    from bot_vk.config import VK_MESSAGE_LIMIT

    messages: list[str] = []
    current = texts.WEEK_HEADER.format(group=group)

    for offset in range(days):
        target = start + timedelta(days=offset)
        lessons = lessons_with_substitutions(conn, group, target)
        block = render_day(group, target, lessons, show_header=True)

        # +2 — разделитель между блоками.
        if current and len(current) + len(block) + 2 > VK_MESSAGE_LIMIT:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}"

    if current:
        messages.append(current)
    return messages

    lessons = get_lessons_for_day(conn, group, target)
    return apply_substitutions(conn, lessons, group, target)