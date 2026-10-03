"""Обработчики команд VK-бота.

Все хендлеры регистрируются внутри :func:`register_handlers`: функция получает
готовый ``Bot`` и вешает на него правила. Так :mod:`bot_vk.main_vk` остаётся
тонким, а тесты могут передать mock-бота и проверить, что правила созданы.

Тексты пока живут прямо здесь (см. TODO в :mod:`bot_vk.texts`).
"""
from __future__ import annotations

from vkbottle.bot import Bot, Message


def register_handlers(bot: Bot) -> None:
    """Зарегистрировать команды VK-бота на переданном ``Bot``.

    Args:
        bot: экземпляр ``vkbottle.bot.Bot`` (в тестах — mock с атрибутом
            ``on.message``).
    """

    @bot.on.message(text=["/start", "start", "Начать", "начать"])
    async def start_handler(message: Message) -> None:
        await message.answer(
            "👋 Привет! Я бот расписания КСТ (VK).\n\n"
            "Пока я в разработке — скоро буду показывать "
            "расписание, замены и посещаемость.\n\n"
            "А пока проверь связь командой «Привет»."
        )

    @bot.on.message(text=["Привет", "привет", "Hi", "hi", "хай"])
    async def hi_handler(message: Message) -> None:
        await message.answer(
            "Привет! Я работаю. Это Long Poll API VK."
        )

    @bot.on.message()
    async def fallback(message: Message) -> None:
        await message.answer(
            "🤔 Не понимаю эту команду. Попробуй:\n"
            "• /start — знакомство\n"
            "• Привет — проверка"
        )