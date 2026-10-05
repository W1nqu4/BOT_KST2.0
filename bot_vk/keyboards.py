"""Клавиатуры VK-бота.

Клавиатуры VK отличаются от Telegram: кнопки описываются ``Text`` (нажатие
приходит как обычное сообщение с этим текстом) и сериализуются в JSON строкой
через ``.get_json()``. Хендлеры ловят нажатия по тексту кнопки, поэтому
константы ``BTN_*`` — единственный источник этих строк: и клавиатура, и
фильтры хендлеров берут их отсюда, иначе подписи разъедутся.
"""
from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, Text

# Подписи кнопок. Иконка входит в текст: VK присылает нажатие именно как
# текст кнопки, поэтому «📆 Сегодня» и «Сегодня» — разные строки.
BTN_TODAY = "📆 Сегодня"
BTN_WEEK = "📅 Неделя"
BTN_DEADLINES = "📝 Дедлайны"
BTN_PROFILE = "👤 Профиль"

# Кнопка отмены при выборе ФИО преподавателя (inline).
BTN_TEACHER_CANCEL = "🔙 Отмена"

# Payload кнопок выбора ФИО: {"c": "tapply", "i": <индекс>}.
# В VK payload уходит в событие ``message_event``, а не в текст, поэтому
# индекс не «разъезжается» с подписью кнопки.
TEACHER_CB_FIELD = "c"
TEACHER_CB_VALUE = "tapply"
TEACHER_CB_INDEX = "i"

# Payload отмены заявки на шаге выбора ФИО.
TEACHER_CB_CANCEL_VALUE = "tapply_cancel"


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


def names_kb(names: list[str]) -> str:
    """Inline-клавиатура выбора ФИО + «Отмена» для заявки преподавателя.

    Отличие от Telegram: кнопка несёт не callback-строку, а payload — VK
    присылает его в событии ``message_event``, и индекс не нужно парсить из
    текста. Эмодзи и длинные ФИО в подписи не мешают: payload отдельный.

    Ограничения VK: не более 5 кнопок в ряду и 10 рядов. ФИО выбираются из
    справочника штучно (максимум :data:`bot.services.teacher_names.MAX_CHOICES`),
    поэтому раскладываем по 2 в ряд: подписи ФИО длинные («Виссарионова Анна
    Сергеевна»), и в ряд из пяти они не помещаются.

    Args:
        names: список ФИО (индекс = позиция в списке).

    Returns:
        JSON-строка клавиатуры для параметра ``keyboard``.
    """
    keyboard = Keyboard(one_time=False, inline=True)
    for index, name in enumerate(names):
        payload = {
            TEACHER_CB_FIELD: TEACHER_CB_VALUE,
            TEACHER_CB_INDEX: index,
        }
        # В ряду — 2 ФИО: так подписи остаются читаемыми.
        if index and index % 2 == 0:
            keyboard.row()
        keyboard.add(Callback(name, payload=payload))

    keyboard.row().add(Callback(
        BTN_TEACHER_CANCEL,
        payload={TEACHER_CB_FIELD: TEACHER_CB_CANCEL_VALUE},
    ))
    return keyboard.get_json()