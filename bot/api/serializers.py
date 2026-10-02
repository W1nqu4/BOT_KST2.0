"""Сериализация предметов и пар расписания в JSON для Mini App.

Бот показывает предмет как «ОД.07 Математика» — код специальности часть
названия, и убирать его нельзя: по коду студенты сверяются с учебным планом.
Здесь только нормализация пробелов и «Фамилия И.О.» для преподавателя: в
расписании он записан полностью («Грехова Оксана Владимировна»).
"""
from __future__ import annotations

import re

from bot.attendance.service import normalize_full_name
from bot.db import split_teacher_cell
from bot.parsers.substitutions import is_placeholder

# Несколько пробелов и неразрывных пробелов внутри строки → один обычный.
_SPACES_RE = re.compile(r"[\s\u00a0]+")

# Статусы пары для фронта: совпадают с типом LessonStatus в webapp.
STATUS_PLANNED = "planned"
STATUS_SUBSTITUTED = "substituted"
STATUS_CANCELLED = "cancelled"
STATUS_SELF_STUDY = "self_study"


def clean_text(value: object) -> str:
    """Привести текст к показываемому виду: пробелы, заглушки → ``""``.

    Args:
        value: значение из БД (может быть None, числом — приведётся к строке).

    Returns:
        Строка без лишних пробелов; ``""`` для None и заглушек из листа замен.
    """
    raw = str(value if value is not None else "").strip()
    if not raw or is_placeholder(raw):
        return ""
    return _SPACES_RE.sub(" ", raw).strip()


def teacher_display(value: object) -> str:
    """Преподаватель в формате «Фамилия И.О.» (как в списке группы бота).

    В ячейке расписания у подгрупп бывает несколько ФИО через запятую; для
    карточки пары берём первое (совпадает с тем, что выводит бот в
    ``/teacher`` для своей группы).

    Args:
        value: содержимое колонки ``teacher``.

    Returns:
        «Фамилия И.О.» или исходный текст, если разобрать ФИО не удалось;
        ``""`` — если показывать нечего.
    """
    text = clean_text(value)
    if not text:
        return ""
    # «Грехова Оксана Владимировна, Витюгова Наталья Владимировна» → первое ФИО.
    first = split_teacher_cell(text)[0] if split_teacher_cell(text) else text
    short = normalize_full_name(first)
    return short or first


def old_subject_of(lesson: dict) -> str | None:
    """Предмет по плану — только если его заменили или отменили.

    Args:
        lesson: занятие после :func:`bot.services.schedule_service.apply_substitutions`.

    Returns:
        Название предмета из плана или None, если замены/отмены не было
        (фронт показывает его зачёркнутым в карточке замены).
    """
    planned = clean_text(lesson.get("planned_subject"))
    current = clean_text(lesson.get("subject"))
    if not planned:
        # Замена на пару, которой не было в плане: планового предмета нет.
        return None
    if planned == current and not lesson.get("is_cancelled"):
        return None
    return planned


def lesson_status(lesson: dict) -> str:
    """Статус пары для фронта.

    Приоритет как у иконок бота
    (:func:`bot.handlers.schedule.lesson_icon`): отмена → самостоятельная →
    замена → плановая пара.

    Args:
        lesson: занятие после наложения замен.

    Returns:
        ``planned`` | ``substituted`` | ``cancelled`` | ``self_study``.
    """
    if lesson.get("is_cancelled"):
        return STATUS_CANCELLED
    if lesson.get("is_self_study"):
        return STATUS_SELF_STUDY
    if lesson.get("is_substitution") or old_subject_of(lesson):
        return STATUS_SUBSTITUTED
    return STATUS_PLANNED


def lesson_to_json(lesson: dict) -> dict:
    """Одно занятие → JSON для Mini App.

    Args:
        lesson: занятие после наложения замен.

    Returns:
        Словарь с полями ``para``, ``subject``, ``teacher``, ``room``,
        ``time``, ``status``, ``old_subject``, ``is_cancelled``,
        ``is_self_study``. ``time`` — звонки нужного дня («09:00–10:35»).
    """
    return {
        "para": int(lesson.get("para_number") or 0),
        "subject": clean_text(lesson.get("subject")),
        "teacher": teacher_display(lesson.get("teacher")),
        "room": clean_text(lesson.get("room")),
        "time": clean_text(lesson.get("time_range")),
        "status": lesson_status(lesson),
        "old_subject": old_subject_of(lesson),
        "is_cancelled": bool(lesson.get("is_cancelled")),
        "is_self_study": bool(lesson.get("is_self_study")),
    }


def lessons_to_json(lessons: list[dict]) -> list[dict]:
    """Список занятий → список JSON-объектов (порядок сохраняется)."""
    return [lesson_to_json(lesson) for lesson in lessons]


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_PLANNED",
    "STATUS_SELF_STUDY",
    "STATUS_SUBSTITUTED",
    "clean_text",
    "lesson_status",
    "lesson_to_json",
    "lessons_to_json",
    "old_subject_of",
    "teacher_display",
]