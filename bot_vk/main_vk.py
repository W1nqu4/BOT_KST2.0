"""Точка входа VK-бота: ``python -m bot_vk.main_vk``.

Бот работает от имени сообщества по Long Poll API — публичный HTTPS и
вебхуки не нужны, поэтому процесс можно держать рядом с Telegram-ботом.

Логирование настраивается здесь же (процесс самостоятельный, свой
``logging.basicConfig``): формат совпадает с форматом Telegram-бота.
"""
from __future__ import annotations

import asyncio
import logging

from vkbottle.bot import Bot
from vkbottle.polling import BotPolling

from bot.db import get_connection
from bot.migrations import apply_migrations
from bot_vk import config, storage
from bot_vk.handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot_vk")


async def main() -> None:
    """Проверить конфигурацию, подготовить БД и уйти в Long Poll."""
    config.validate()
    logger.info("VK-бот запускается, group_id=%s", config.VK_GROUP_ID)

    # Та же БД, что у Telegram-бота: расписание и замены общие, а группы
    # VK-пользователей лежат в отдельной таблице vk_users (миграция 15).
    conn = get_connection(config.DB_PATH)
    version = apply_migrations(conn)
    logger.info(
        "БД готова, схема версии %s, групп в расписании: %s",
        version,
        len(storage.available_groups(conn)),
    )

    bot = Bot(
        token=config.VK_TOKEN,
        # group_id задаём явно: иначе vkbottle выясняет его отдельным запросом
        # groups.getById при старте, а значение VK_GROUP_ID остаётся
        # неиспользованным. С конфигом лишнего запроса нет.
        polling=BotPolling(group_id=config.VK_GROUP_ID),
    )
    register_handlers(bot, conn)
    logger.info(
        "handlers зарегистрированы: %d",
        len(bot.labeler.message_view.handlers),
    )

    try:
        await bot.run_polling()
    finally:
        # Соединение закрываем сами: процедура живёт до остановки процесса.
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())