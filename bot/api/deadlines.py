"""Дедлайны Mini App: список, создание, удаление.

Схема ответа совпадает с тем, что показывает бот в «📋 Дедлайны»
(:mod:`bot.handlers.deadlines`): предмет, задача, преподаватель и срок.
``days_left`` считается :func:`bot.services.deadline_service.days_left` —
отрицательное значение означает «просрочено».
"""
from __future__ import annotations

from datetime import date, datetime

from bot.services import deadline_service
from bot.api.serializers import clean_text, teacher_display

# Ограничение длины задачи: совпадает с проверкой ввода в боте.
TASK_MAX_LENGTH = 120

# Формат даты в теле запроса: ISO-8601 (``YYYY-MM-DD``).
DATE_FORMAT = "%Y-%m-%d"


def parse_deadline_date(raw: object) -> tuple[str | None, bool]:
    """Разобрать дату срока из тела запроса.

    Args:
        raw: значение поля ``date`` (строка ISO, пусто или None).

    Returns:
        ``(дата_или_None, ok)``. Пустая дата — это дедлайн без срока
        (``ok=True``, ``None``); мусор — ``ok=False``.
    """
    text = str(raw or "").strip()
    if not text:
        return None, True
    try:
        datetime.strptime(text, DATE_FORMAT)
    except ValueError:
        return None, False
    # ``date.fromisoformat`` строже ``strptime``: отсекает «2026-02-30».
    try:
        return date.fromisoformat(text).isoformat(), True
    except ValueError:
        return None, False


def deadline_item(deadline: dict) -> dict:
    """Один дедлайн → JSON для Mini App.

    Args:
        deadline: строка таблицы ``deadlines``.

    Returns:
        Словарь с полями ``id``, ``subject``, ``task``, ``teacher``, ``date``,
        ``days_left``.
    """
    date_iso = str(deadline.get("deadline_date") or "").strip()
    return {
        "id": int(deadline.get("id") or 0),
        "subject": clean_text(deadline.get("subject")),
        "task": clean_text(deadline.get("task")),
        "teacher": teacher_display(deadline.get("teacher")),
        "date": date_iso,
        "days_left": deadline_service.days_left(date_iso or None),
    }


def deadlines_payload(conn, tg_id: int) -> dict:
    """Активные дедлайны пользователя → JSON.

    Args:
        conn: соединение SQLite.
        tg_id: владелец.

    Returns:
        ``{'items': [...]}`` — отсортировано по сроку (бездатные в конце),
        как в :func:`bot.services.deadline_service.list_active`.
    """
    items = deadline_service.list_active(conn, tg_id)
    return {"items": [deadline_item(item) for item in items]}


def validate_new_deadline(body: dict) -> tuple[dict | None, str | None]:
    """Проверить тело POST ``/api/deadlines``.

    Args:
        body: разобранный JSON запроса.

    Returns:
        ``(нормализованные_поля, None)`` либо ``(None, код_ошибки)``.
        Коды: ``bad_body``, ``empty_task``, ``task_too_long``, ``bad_date``.
    """
    if not isinstance(body, dict):
        return None, "bad_body"

    subject = clean_text(body.get("subject"))
    task = clean_text(body.get("task"))
    teacher = clean_text(body.get("teacher"))

    if not subject and not task:
        return None, "empty_task"
    if len(task) > TASK_MAX_LENGTH:
        return None, "task_too_long"

    date_iso, ok = parse_deadline_date(body.get("date"))
    if not ok:
        return None, "bad_date"

    return {
        "subject": subject,
        "task": task,
        "teacher": teacher,
        "date_iso": date_iso,
    }, None


__all__ = [
    "DATE_FORMAT",
    "TASK_MAX_LENGTH",
    "deadline_item",
    "deadlines_payload",
    "parse_deadline_date",
    "validate_new_deadline",
]