"""Клавиатуры VK-бота.

Клавиатуры VK отличаются от Telegram: кнопки описываются ``Text`` (нажатие
приходит как обычное сообщение с этим текстом) и сериализуются в JSON строкой
через ``.get_json()``. Хендлеры ловят нажатия по тексту кнопки, поэтому
константы ``BTN_*`` — единственный источник этих строк: и клавиатура, и
фильтры хендлеров берут их отсюда, иначе подписи разъедутся.
"""
from __future__ import annotations

from vkbottle import Keyboard, KeyboardButtonColor, Text

# Подписи кнопок. Иконка входит в текст: VK присылает нажатие именно как
# текст кнопки, поэтому «📆 Сегодня» и «Сегодня» — разные строки.
BTN_TODAY = "📆 Сегодня"
BTN_WEEK = "📅 Неделя"
BTN_DEADLINES = "📝 Дедлайны"
BTN_PROFILE = "👤 Профиль"


def main_kb() -> str:
    """Главное меню VK-бота (постоянная клавиатура под полем ввода).

    Returns:
        JSON-строка клавиатуры для параметра ``keyboard`` в ``message.answer``.
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_TODAY), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_WEEK), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_DEADLINES), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_PROFILE), color=KeyboardButtonColor.SECONDARY)
    ).get_json()


def schedule_kb() -> str:
    """Клавиатура экрана расписания (без «Дедлайнов»).

    Отдельная клавиатура нужна там, где раздел ещё не реализован: не показываем
    кнопку, которая уводит в заглушку.
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_TODAY), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_WEEK), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_PROFILE), color=KeyboardButtonColor.SECONDARY)
    ).get_json()