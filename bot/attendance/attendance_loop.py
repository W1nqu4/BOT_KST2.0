"""Фоновая задача посещаемости: опросы по звонкам (этап 2).

Раз в минуту проверяем: если сейчас идёт пара (по звонкам нужного дня), и у
группы есть привязанный чат, и пары в расписании есть — отправляем в чат
опрос «кто на паре». Закрываем опросы, у которых вышло время (конец пары).

Почему раз в минуту: пара начинается в конкретную минуту, и опрос должен
уйти в её начале. Проверка «есть ли уже опрос» (``UNIQUE`` в БД) не даёт
отправить его повторно.

Тексты и SQL живут в :mod:`bot.attendance.attendance_service` — здесь только
цикл, чтобы логику можно было тестировать без ожидания.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from bot.attendance import attendance_service as att_svc
from bot.config import KRASNOYARSK
from bot.services.schedule_service import _sleep

logger = logging.getLogger(__name__)

# Период проверки: раз в минуту (пара начинается в конкретную минуту).
ATTENDANCE_TICK_SECONDS = 60


async def attendance_loop(conn, bot, now_provider=None) -> None:
    """Бесконечный цикл: опросы к началу пары, закрытие по звонку.

    Ошибка прохода не роняет задачу: логируется, и цикл продолжается.
    ``asyncio.CancelledError`` пролетает наружу — иначе задачу нельзя
    корректно остановить при завершении приложения.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now_provider: функция «текущий момент» (для тестов).

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    moment_provider = now_provider or (lambda: datetime.now(KRASNOYARSK))
    while True:
        try:
            await att_svc.tick(conn, bot, moment_provider())
        except asyncio.CancelledError:
            logger.info("attendance_loop cancelled")
            raise
        except Exception:
            logger.exception("attendance_loop failed")
        await _sleep(ATTENDANCE_TICK_SECONDS)