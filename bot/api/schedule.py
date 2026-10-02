"""Сборка дней расписания для Mini App.

Общая часть ``/api/schedule/today``, ``/api/schedule/day`` и
``/api/schedule/week``: у них один и тот же формат дня, различается только
набор дат. Логика занятий берётся из :mod:`bot.services.schedule_service`,
здесь — только сборка ответа.
"""
from __future__ import annotations

from datetime import date, timedelta

from bot.keyboards.inline import day_name
from bot.services.schedule_service import (
    apply_substitutions,
    get_lessons_for_day,
    week_type_for_date,
)
from bot.api.serializers import lessons_to_json

# Сколько дней в учебной неделе: понедельник–суббота (воскресенье выходной).
WEEK_DAYS = 6


def day_payload(conn, group: str, target: date,
                today: date | None = None) -> dict:
    """Один день расписания → JSON.

    Args:
        conn: соединение SQLite.
        group: группа студента.
        target: дата дня.
        today: «сегодня» для флага ``is_today`` (в тестах задаётся явно).

    Returns:
        Словарь с ``date``, ``weekday``, ``week_type``, ``lessons``,
        ``is_today``, ``group``. Пустой день даёт ``lessons: []``.
    """
    lessons = apply_substitutions(
        conn, get_lessons_for_day(conn, group, target), group, target
    )
    return {
        "group": group,
        "date": target.isoformat(),
        "weekday": day_name(target.isoweekday()),
        "week_type": week_type_for_date(target),
        "lessons": lessons_to_json(lessons),
        "is_today": target == (today or date.today()),
    }


def week_start_for(target: date) -> date:
    """Понедельник недели, в которую попадает дата."""
    return target - timedelta(days=target.weekday())


def week_payload(conn, group: str, target: date) -> dict:
    """Понедельник–суббота недели с датой ``target`` → JSON.

    Args:
        conn: соединение SQLite.
        group: группа студента.
        target: любая дата внутри нужной недели.

    Returns:
        ``{'group', 'week_start', 'days': [...]}`` — ровно
        :data:`WEEK_DAYS` дней, начиная с понедельника.
    """
    monday = week_start_for(target)
    days = [
        day_payload(conn, group, monday + timedelta(days=offset), today=target)
        for offset in range(WEEK_DAYS)
    ]
    return {
        "group": group,
        "week_start": monday.isoformat(),
        "days": days,
    }


__all__ = ["WEEK_DAYS", "day_payload", "week_payload", "week_start_for"]