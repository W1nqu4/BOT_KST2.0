"""Доступ VK-бота к Telegram-боту — без импорта aiogram.

Заявка преподавателя живёт в таблице ``teachers`` по ``tg_id``, а уведомить о
ней надо админов В TELEGRAM (админ живёт там). Из ``bot_vk/`` импортировать
``aiogram`` нельзя: это подняло бы весь Telegram-стек в процессе VK-бота,
поэтому ссылка на готовый объект ``aiogram.Bot`` кладётся сюда из
:mod:`bot.main` (оба бота работают в одном процессе — см.
``bot.main.run_both_bots`` → ``start_vk_bot``).

Модуль намеренно ничего не импортирует из ``bot/``: только хранит ссылку.
Если VK-бот запущен отдельно (``python -m bot_vk.main_vk``), ссылки нет —
уведомление не уйдёт, и об этом честно скажет warning в
:func:`bot.services.teacher_notify.notify_admin_about_teacher_application`.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ссылка на aiogram.Bot. Тип — Any: модуль не должен знать про aiogram даже
# на уровне аннотаций (иначе появится импорт ради типизации).
_tg_bot: Any | None = None


def set_tg_bot(bot: Any | None) -> None:
    """Запомнить Telegram-бота для уведомлений из VK.

    Вызывается из :mod:`bot.main` при сборке VK-бота. Повторный вызов
    перезаписывает ссылку (перезапуск polling, тесты).

    Args:
        bot: объект ``aiogram.Bot`` или None, чтобы очистить ссылку.
    """
    global _tg_bot
    _tg_bot = bot
    logger.debug(
        "vk: Telegram-бот для уведомлений %s",
        "подключён" if bot is not None else "не задан",
    )


def get_tg_bot() -> Any | None:
    """Telegram-бот для уведомлений админов (или None).

    Returns:
        Ссылка из :func:`set_tg_bot` либо None, если VK-бот работает сам по
        себе и Telegram-бота рядом нет.
    """
    return _tg_bot


__all__ = ("get_tg_bot", "set_tg_bot")