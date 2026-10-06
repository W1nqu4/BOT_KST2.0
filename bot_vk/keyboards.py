"""Клавиатуры VK-бота.

Клавиатуры VK отличаются от Telegram: кнопки описываются ``Text`` (нажатие
приходит как обычное сообщение с этим текстом) и сериализуются в JSON строкой
через ``.get_json()``. Хендлеры ловят нажатия по тексту кнопки, поэтому
константы ``BTN_*`` — единственный источник этих строк: и клавиатура, и
фильтры хендлеров берут их отсюда, иначе подписи разъедутся.
"""
from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, Text

from bot_vk import texts

# Подписи кнопок. Иконка входит в текст: VK присылает нажатие именно как
# текст кнопки, поэтому «📆 Расписание» и «Расписание» — разные строки.
#
# Набор и порядок совпадают с Telegram (bot/keyboards/reply.py: BTN_SCHEDULE,
# BTN_DEADLINES, BTN_MY_GROUP, BTN_PROFILE) — раскладка должна быть 1-в-1.
BTN_SCHEDULE = "📆 Расписание"
BTN_DEADLINES = "📝 Дедлайны"
BTN_MY_GROUP = "📊 Моя группа"
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
    """Главное меню VK-бота — 1-в-1 как в Telegram.

    Layout:
        [📆 Расписание] [📝 Дедлайны]
        [📊 Моя группа] [👤 Профиль]

    Порядок и подписи кнопок совпадают с ``bot/keyboards/reply.py:main_kb``,
    чтобы пользователь, переходящий между платформами, видел одно меню.
    «📆 Расписание» открывает сегодняшний день — отдельной кнопки «Сегодня»
    нет (в Telegram её тоже нет).

    Returns:
        JSON-строка клавиатуры для параметра ``keyboard`` в ``message.answer``.
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text(BTN_SCHEDULE), color=KeyboardButtonColor.PRIMARY)
        .add(Text(BTN_DEADLINES), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text(BTN_MY_GROUP), color=KeyboardButtonColor.SECONDARY)
        .add(Text(BTN_PROFILE), color=KeyboardButtonColor.SECONDARY)
    ).get_json()


def schedule_kb() -> str:
    """Клавиатура экрана расписания: то же меню, что и ``main_kb``.

    Раньше здесь не было «Дедлайнов» (раздел был заглушкой). Теперь раздел
    работает, поэтому набор кнопок совпадает с главным меню — как в Telegram,
    где экран расписания показывается с тем же reply-меню.
    """
    return main_kb()


# Payload кнопок дедлайнов: {"c": "dl", "a": "add"|"del"|"list"|"cancel",
# "i": <id дедлайна для удаления>}. Как и у заявки преподавателя, данные идут
# payload-ом (событие message_event), а не текстом кнопки.
DEADLINE_CB_FIELD = "c"
DEADLINE_CB_VALUE = "dl"
DEADLINE_CB_ACTION = "a"
DEADLINE_CB_ID = "i"

# Действия раздела дедлайнов.
DEADLINE_ACTION_ADD = "add"
DEADLINE_ACTION_DELETE = "del"
DEADLINE_ACTION_LIST = "list"
DEADLINE_ACTION_CANCEL = "cancel"


def deadlines_kb() -> str:
    """Inline-клавиатура списка дедлайнов: «Добавить» и «Удалить».

    Как в Telegram (``deadline_list_kb``): два действия одним рядом. В VK ряд
    ограничен пятью кнопками, здесь их две — с запасом.
    """
    return (
        Keyboard(one_time=False, inline=True)
        .add(Callback(texts.BTN_DL_ADD,
                      payload={DEADLINE_CB_FIELD: DEADLINE_CB_VALUE,
                               DEADLINE_CB_ACTION: DEADLINE_ACTION_ADD}))
        .add(Callback(texts.BTN_DL_DELETE,
                      payload={DEADLINE_CB_FIELD: DEADLINE_CB_VALUE,
                               DEADLINE_CB_ACTION: DEADLINE_ACTION_DELETE}))
    ).get_json()


# Сколько дедлайнов показывать кнопками удаления. Предел VK — 10 рядов на
# клавиатуру, а каждый дедлайн занимает свой ряд (плюс ряд «К списку»).
# 9 задач + «К списку» = 10 рядов, ровно на пределе; больше — и VK отвергнет
# всё сообщение целиком, поэтому список честно урезаем.
DEADLINE_DELETE_LIMIT = 9


def deadline_delete_kb(items: list[dict]) -> str:
    """Inline-клавиатура удаления: по кнопке на дедлайн + «К списку».

    Подпись кнопки — название задачи (обрезаем до 35 символов: VK режет
    длинные подписи сам, но обрезка на нашей стороне предсказуемее).
    ``id`` уходит в payload, поэтому обрезка подписи ничего не ломает.

    Args:
        items: список дедлайнов (``id`` и ``task``).

    Returns:
        JSON-строка клавиатуры. Не больше
        :data:`DEADLINE_DELETE_LIMIT` дедлайнов — иначе клавиатура не влезет
        в лимит VK и сообщение не дойдёт.
    """
    keyboard = Keyboard(one_time=False, inline=True)
    for item in items[:DEADLINE_DELETE_LIMIT]:
        task = str(item.get("task") or texts.DEADLINE_NO_TASK).strip()
        label = task if len(task) <= 35 else task[:34] + "…"
        keyboard.add(Callback(
            f"🗑 {label}",
            payload={DEADLINE_CB_FIELD: DEADLINE_CB_VALUE,
                     DEADLINE_CB_ACTION: DEADLINE_ACTION_DELETE,
                     DEADLINE_CB_ID: int(item["id"])},
        )).row()

    keyboard.add(Callback(
        texts.BTN_DL_LIST,
        payload={DEADLINE_CB_FIELD: DEADLINE_CB_VALUE,
                 DEADLINE_CB_ACTION: DEADLINE_ACTION_LIST},
    ))
    return keyboard.get_json()


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