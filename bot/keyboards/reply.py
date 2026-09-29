"""Reply-клавиатуры: главное меню.

Тексты кнопок — константы: обработчики сравнивают именно с ними, поэтому
менять текст нужно в одном месте.
"""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Тексты кнопок главного меню.
BTN_TODAY = "📅 Сегодня"
BTN_SCHEDULE = "📆 Расписание"
BTN_DEADLINES = "📝 Дедлайны"
BTN_MY_GROUP = "📊 Моя группа"
BTN_PROFILE = "👤 Профиль"
BTN_FEEDBACK = "🐛 Сообщить о проблеме"

# Текст для отмены FSM-шагов (обратная связь, рассылка админа).
BTN_CANCEL = "↩️ Отмена"

# Кнопка настройки группы для расписания (показывается inline при первом входе).
BTN_SETUP_SCHEDULE = "📆 Настроить расписание"

# Ответ на кнопки, которые подключатся позже.
IN_DEVELOPMENT = "🚧 Раздел в разработке"


def main_kb() -> ReplyKeyboardMarkup:
    """Главное меню: четыре кнопки, включая раздел посещаемости.

    Layout:
        [📆 Расписание] [📝 Дедлайны]
        [📊 Моя группа] [👤 Профиль]

    «📅 Сегодня» нет (её роль выполняет «📆 Расписание» — открывает
    сегодняшний день), «📚 Предметы» живут внутри экрана расписания.
    Кнопки «📊 Моя группа» ведёт в раздел посещаемости
    (:mod:`bot.attendance`).

    Returns:
        ReplyKeyboardMarkup с четырьмя кнопками.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SCHEDULE), KeyboardButton(text=BTN_DEADLINES)],
            [KeyboardButton(text=BTN_MY_GROUP), KeyboardButton(text=BTN_PROFILE)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел или напиши группу",
    )
