"""Reply-клавиатуры: главное меню.

Тексты кнопок — константы: обработчики сравнивают именно с ними, поэтому
менять текст нужно в одном месте.
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Тексты кнопок главного меню.
BTN_TODAY = "📅 Сегодня"
BTN_SCHEDULE = "📆 Расписание"
BTN_DEADLINES = "📝 Дедлайны"
BTN_PROFILE = "👤 Профиль"
BTN_FEEDBACK = "🐛 Сообщить о проблеме"

# Текст для отмены FSM-шагов (обратная связь, рассылка админа).
BTN_CANCEL = "↩️ Отмена"

# Ответ на кнопки, которые подключатся позже.
IN_DEVELOPMENT = "🚧 Раздел в разработке"


def main_kb() -> ReplyKeyboardMarkup:
    """Главное меню: по две кнопки в ряд, клавиатура подстраивается.

    Returns:
        ReplyKeyboardMarkup с 5 кнопками.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_SCHEDULE)],
            [KeyboardButton(text=BTN_DEADLINES), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_FEEDBACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел или напиши группу",
    )
