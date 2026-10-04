"""Хендлеры Telegram.

Каждый модуль экспортирует ``router``; :func:`bot.main.build_dispatcher`
подключает их в порядке приоритета. Порядок важен: регистрация (start)
обрабатывает ввод группы раньше, чем общий перехват текста.
"""
from __future__ import annotations

from aiogram import Router

from bot.attendance import admin_handlers as attendance_admin_handlers
from bot.attendance import attendance_handlers as attendance_marks_handlers
from bot.attendance import handlers as attendance_handlers
from bot.attendance import vote_handlers as attendance_vote_handlers
from bot.handlers import account_link as account_link_handlers
from bot.handlers import admin as admin_handlers
from bot.handlers import calendar as calendar_handlers
from bot.handlers import deadlines as deadlines_handlers
from bot.handlers import feedback as feedback_handlers
from bot.handlers import group_chats as group_chats_handlers
from bot.handlers import group_input as group_input_handlers
from bot.handlers import help as help_handlers
from bot.handlers import schedule as schedule_handlers
from bot.handlers import start as start_handlers
from bot.handlers import teacher as teacher_handlers
from bot.handlers import teacher_apply as teacher_apply_handlers

# Порядок подключения роутеров: сначала регистрация (FSM), затем рабочие
# разделы, затем обратная связь, админка (свои FSM-состояния) и справка.
# Админка идёт после пользовательских роутеров: её фильтр IsAdmin отсекает
# посторонних, а нейтральный ответ срабатывает только если команда не
# перехвачена раньше.
# Чаты групп идут сразу после регистрации: их команды (/setup, /unsync,
# /schedule, /chat_status) адресованы чату, а не личной переписке.
ROUTERS: tuple[Router, ...] = (
    start_handlers.router,
    account_link_handlers.router,
    attendance_handlers.router,
    attendance_marks_handlers.router,
    attendance_vote_handlers.router,
    attendance_admin_handlers.router,
    group_chats_handlers.router,
    schedule_handlers.router,
    teacher_handlers.router,
    teacher_apply_handlers.router,
    deadlines_handlers.router,
    calendar_handlers.router,
    feedback_handlers.router,
    admin_handlers.router,
    help_handlers.router,
    # Свободный ввод группы — ПОСЛЕДНИМ. Фильтр F.text совпадает с любым
    # текстом, и aiogram останавливается на первом сработавшем хендлере даже
    # при `return` внутри: раньше в списке этот перехватчик отобрал бы
    # сообщения у дедлайнов, обратной связи и остальных FSM-шагов.
    group_input_handlers.router,
)


def all_routers() -> tuple[Router, ...]:
    """Роутеры в порядке подключения к диспетчеру."""
    return ROUTERS
