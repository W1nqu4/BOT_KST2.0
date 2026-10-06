"""Тесты унификации reply-клавиатуры VK с Telegram (подшаг 3).

Главное требование владельца: итоговое меню VK должно быть 1-в-1 как в TG —

    [📆 Расписание]  [📝 Дедлайны]
    [📊 Моя группа]  [👤 Профиль]

Поэтому тесты сравнивают подписи с константами Telegram напрямую
(``bot.keyboards.reply``): если кто-то поправит текст кнопки в одном боте,
тест упадёт. Раньше в VK были «📅 Сегодня» и «📅 Неделя» — их быть не должно.
"""

from __future__ import annotations

import json

import pytest

from bot.keyboards import reply as tg_reply
from bot_vk import keyboards


def vk_labels(keyboard: str) -> list[str]:
    """Подписи кнопок VK-клавиатуры в порядке следования."""
    data = json.loads(keyboard)
    return [
        button["action"]["label"]
        for row in data.get("buttons", [])
        for button in row
    ]


def vk_rows(keyboard: str) -> list[list[str]]:
    """Подписи кнопок VK-клавиатуры, разбитые по рядам."""
    data = json.loads(keyboard)
    return [
        [button["action"]["label"] for button in row]
        for row in data.get("buttons", [])
    ]


def tg_rows(markup) -> list[list[str]]:
    """Подписи кнопок Telegram-клавиатуры, разбитые по рядам."""
    return [[button.text for button in row] for row in markup.keyboard]


# --- точное совпадение с Telegram ---

def test_main_kb_matches_telegram_exactly() -> None:
    """Меню VK совпадает с меню TG: 4 кнопки, 2×2, тот же порядок.

    Сравниваем и по строкам, и по раскладке: разъехавшийся порядок ломает
    «зеркальность» так же, как другой текст кнопки.
    """
    assert vk_rows(keyboards.main_kb()) == tg_rows(tg_reply.main_kb())


def test_main_kb_has_exactly_four_buttons() -> None:
    """Ровно четыре кнопки в двух рядах по две — как в TG."""
    rows = vk_rows(keyboards.main_kb())

    assert len(rows) == 2, rows
    assert all(len(row) == 2 for row in rows), rows
    assert sum(len(row) for row in rows) == 4


def test_main_kb_order_is_schedule_deadlines_group_profile() -> None:
    """Порядок кнопок: Расписание → Дедлайны → Моя группа → Профиль."""
    labels = vk_labels(keyboards.main_kb())

    assert labels == [
        "📆 Расписание",
        "📝 Дедлайны",
        "📊 Моя группа",
        "👤 Профиль",
    ], labels


# --- константы совпадают с TG байт-в-байт ---

@pytest.mark.parametrize(("vk_name", "tg_name"), [
    ("BTN_SCHEDULE", "BTN_SCHEDULE"),
    ("BTN_DEADLINES", "BTN_DEADLINES"),
    ("BTN_MY_GROUP", "BTN_MY_GROUP"),
    ("BTN_PROFILE", "BTN_PROFILE"),
])
def test_button_constants_match_telegram(vk_name: str, tg_name: str) -> None:
    """Каждая константа кнопки совпадает с TG посимвольно."""
    vk_value = getattr(keyboards, vk_name)
    tg_value = getattr(tg_reply, tg_name)

    assert vk_value == tg_value, (
        f"{vk_name}={vk_value!r} != {tg_name}={tg_value!r}: "
        "меню ботов должно совпадать"
    )


def test_removed_buttons_are_gone() -> None:
    """Кнопок «📅 Сегодня» и «📅 Неделя» больше нет — в TG их нет.

    Проверяем и константы, и саму клавиатуру: если старые подписи вернутся
    (например, при откате правки), меню перестанет совпадать с Telegram.
    """
    assert not hasattr(keyboards, "BTN_TODAY"), "BTN_TODAY удалена"
    assert not hasattr(keyboards, "BTN_WEEK"), "BTN_WEEK удалена"

    labels = vk_labels(keyboards.main_kb())
    assert not any("Сегодня" in label for label in labels), labels
    assert not any("Неделя" in label for label in labels), labels


def test_schedule_kb_matches_main_kb() -> None:
    """Экран расписания показывается с тем же меню, что и главное.

    В Telegram день выбирается навигацией внутри расписания, отдельного меню
    для него нет — поэтому VK-версия возвращает главную клавиатуру.
    """
    assert keyboards.schedule_kb() == keyboards.main_kb()
# --- технические свойства клавиатуры ---

def test_main_kb_is_reply_not_inline() -> None:
    """Меню — reply-клавиатура (inline=False), как в TG."""
    data = json.loads(keyboards.main_kb())

    assert data["inline"] is False
    assert data["one_time"] is False, "меню должно оставаться на экране"


def test_main_kb_is_valid_json() -> None:
    """Клавиатура сериализуется в валидный JSON с кнопками."""
    data = json.loads(keyboards.main_kb())

    assert isinstance(data, dict)
    assert data.get("buttons"), "кнопки должны быть"


def test_main_kb_buttons_are_text_type() -> None:
    """Кнопки меню — текстовые: нажатие приходит обычным сообщением.

    Callback-кнопки (payload) в меню не используются: их принимает только
    inline-клавиатура через ``message_event``.
    """
    data = json.loads(keyboards.main_kb())
    actions = [b["action"] for row in data["buttons"] for b in row]

    assert all(a["type"] == "text" for a in actions), actions
    assert all("payload" not in a for a in actions), actions


def test_handlers_use_button_constants() -> None:
    """Фильтры хендлеров берут подписи из констант, а не из строк.

    Иначе при смене текста кнопки нажатие перестало бы доходить: VK присылает
    ровно тот текст, что нарисован на кнопке.
    """
    import inspect

    from bot_vk import handlers

    source = inspect.getsource(handlers.register_handlers)
    for needle in ("keyboards.BTN_SCHEDULE", "keyboards.BTN_MY_GROUP",
                   "keyboards.BTN_PROFILE"):
        assert needle in source, f"ожидалось использование {needle}"

    # Удалённые кнопки не должны упоминаться в правилах.
    for stale in ('"📅 Неделя"', '"📆 Сегодня"'):
        assert stale not in source, stale


def test_menu_texts_cover_all_buttons() -> None:
    """Все кнопки меню входят в MENU_TEXTS — иначе застрявшее FSM их съест.

    ``MENU_TEXTS`` — список подписей, которые шаг ввода группы не перехватывает.
    Забыть там кнопку значит: студент не ввёл группу, и нажатие «📊 Моя группа»
    уходит в разбор группы вместо раздела.
    """
    from bot_vk.handlers import MENU_TEXTS

    for label in vk_labels(keyboards.main_kb()):
        assert label in MENU_TEXTS, f"{label!r} отсутствует в MENU_TEXTS"