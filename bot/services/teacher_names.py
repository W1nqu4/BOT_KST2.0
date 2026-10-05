"""Справочник преподавателей и статусы заявки — БЕЗ aiogram.

Модуль общий для Telegram- и VK-ботов. Из :mod:`bot.handlers.teacher_apply`
эту логику пришлось вынести: тот модуль импортирует ``aiogram`` на уровне
модуля, а VK-боту (``bot_vk/``) aiogram тянуть нельзя — иначе ``vkbottle``
процесс поднимал бы весь Telegram-стек.

Зависимости только aiogram-free: :mod:`bot.db` и
:data:`bot.parsers.teachers.TEACHERS`.
"""
from __future__ import annotations

from bot import db
from bot.parsers.teachers import PLACEHOLDER_MARK, TEACHERS

# Сколько ФИО показывать кнопками за раз. В Telegram лимит — 100 кнопок,
# но длинный список нечитаем; в VK вызов дополнительно урезает свой предел.
MAX_CHOICES = 20


def available_names() -> list[str]:
    """ФИО из справочника, доступные для заявки.

    Записи с пометкой :data:`bot.parsers.teachers.PLACEHOLDER_MARK` не
    предлагаются: там настоящего имени ещё нет («Аверина (ФИО уточняется)»),
    и одобрить такую заявку админ не смог бы — непонятно, кто это.
    Метка «вакансия» отсекается отдельно (:func:`bot.db.is_vacancy`): это не
    человек, а место, за которым стоят десятки групп.

    Returns:
        Отсортированный список полных ФИО.
    """
    return sorted(
        value for value in TEACHERS.values()
        if PLACEHOLDER_MARK not in value and not db.is_vacancy(value)
    )


def match_names(query: str, limit: int = MAX_CHOICES) -> list[str]:
    """Найти ФИО по подстроке (регистр и ё/е не важны).

    Args:
        query: то, что ввёл пользователь («Богатырева», «богатыр»).
        limit: максимум совпадений.

    Returns:
        Список полных ФИО (может быть пустым).
    """
    needle = (query or "").strip().lower().replace("ё", "е")
    if not needle:
        return []
    found = [
        name for name in available_names()
        if needle in name.lower().replace("ё", "е")
    ]
    return found[:limit]


def status_rus(status: str) -> str:
    """Человекочитаемый статус заявки."""
    return {
        db.TEACHER_PENDING: "⏳ ожидает проверки",
        db.TEACHER_APPROVED: "✅ одобрена",
        db.TEACHER_REJECTED: "❌ отклонена",
    }.get(status, status)


__all__ = ("MAX_CHOICES", "available_names", "match_names", "status_rus")