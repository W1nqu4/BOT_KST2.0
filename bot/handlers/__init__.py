"""Хендлеры Telegram.

Каждый модуль экспортирует ``router``; :func:`bot.main.build_dispatcher`
подключает их в порядке приоритета. Порядок важен: регистрация (start)
обрабатывает ввод группы раньше, чем общий перехват текста.
"""
from __future__ import annotations

from aiogram import Router

from bot.handlers import admin as admin_handlers
from bot.handlers import calendar as calendar_handlers
from bot.handlers import deadlines as deadlines_handlers
from bot.handlers import feedback as feedback_handlers
from bot.handlers import help as help_handlers
from bot.handlers import schedule as schedule_handlers
from bot.handlers import start as start_handlers

# Порядок подключения роутеров: сначала регистрация (FSM), затем рабочие
# разделы, затем обратная связь, админка (свои FSM-состояния) и справка.
# Админка идёт после пользовательских роутеров: её фильтр IsAdmin отсекает
# посторонних, а нейтральный ответ срабатывает только если команда не
# перехвачена раньше.
ROUTERS: tuple[Router, ...] = (
    start_handlers.router,
    schedule_handlers.router,
    deadlines_handlers.router,
    calendar_handlers.router,
    feedback_handlers.router,
    admin_handlers.router,
    help_handlers.router,
)


def all_routers() -> tuple[Router, ...]:
    """Роутеры в порядке подключения к диспетчеру."""
    return ROUTERS
