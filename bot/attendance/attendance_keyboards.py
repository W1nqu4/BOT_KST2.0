"""Inline-клавиатуры посещаемости (этап 2).

Callback-данные:
- ``att:mark:{date}:{para}`` — кнопка «Я на паре» в опросе;
- ``att:open:{date}:{para}`` — «Отметиться» из лички (показать кнопку);
- ``att:my`` — сводка «Моя посещаемость»;
- ``att:today`` — пары на сегодня;
- ``att:mk:{idx}`` — переключить статус студента в /mark;
- ``att:req:{date}:{para}`` — староста просит опрос досрочно.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.attendance import attendance_texts as atext

# Callback-данные.
CB_MARK_PREFIX = "att:mark:"       # att:mark:2026-09-30:2
CB_OPEN_PREFIX = "att:open:"       # att:open:2026-09-30:2
CB_MY = "att:my"
CB_TODAY = "att:today"
CB_CYCLE_PREFIX = "att:mk:"        # att:mk:0
CB_REQUEST_PREFIX = "att:req:"     # att:req:2026-09-30:2
CB_BACK = "att:back"
# Опрос «Да/Нет» (миграция 14): att:check:{date}:{para}:{yes|no}
CB_CHECK_PREFIX = "att:check:"


def poll_kb(date_iso: str, para: int) -> InlineKeyboardMarkup:
    """Клавиатура опроса: одна кнопка «Я на паре»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=atext.POLL_BUTTON,
            callback_data=f"{CB_MARK_PREFIX}{date_iso}:{para}",
        )],
    ])


def mark_kb(date_iso: str, para: int) -> InlineKeyboardMarkup:
    """Кнопка отметки для личного экрана «Отметиться на паре»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=atext.POLL_BUTTON,
            callback_data=f"{CB_MARK_PREFIX}{date_iso}:{para}",
        )],
    ])


def my_attendance_kb() -> InlineKeyboardMarkup:
    """Кнопки под сводкой «Моя посещаемость»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 Пары сегодня", callback_data=CB_TODAY)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home")],
    ])


def day_kb(paras: list[dict], date_iso: str) -> InlineKeyboardMarkup:
    """Кнопки отметки по каждой паре дня (для экрана «Отметиться»).

    Args:
        paras: ``[{'para': N, 'subject': str, 'marked': bool}]``.
        date_iso: дата.
    """
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if item.get('marked') else '➖'} "
                 f"{item['para']} пара · {str(item['subject'])[:28]}",
            callback_data=f"{CB_OPEN_PREFIX}{date_iso}:{item['para']}",
        )]
        for item in paras
    ]
    rows.append([InlineKeyboardButton(text="🏠 Меню",
                                      callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mark_list_kb(students: list[dict], date_iso: str,
                 para: int) -> InlineKeyboardMarkup:
    """Список студентов с чекбоксами для /mark.

    Дата и пара зашиты в callback (``att:mk:{date}:{para}:{index}``), поэтому
    обработчику не нужен FSM: клавиатура самодостаточна, а список не
    «устаревает» при потере состояния.

    Порядок статусов при нажатии: present → late → absent → excused → present
    (см. :func:`bot.attendance.attendance_service.cycle_status`).
    """
    rows = [
        [InlineKeyboardButton(
            text=f"{atext.mark_button_label(item.get('status'))} "
                 f"{str(item['full_name'])[:30]}",
            callback_data=f"{CB_CYCLE_PREFIX}{date_iso}:{para}:{index}",
        )]
        for index, item in enumerate(students)
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад",
                                      callback_data="grp:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def request_poll_kb(date_iso: str, para: int) -> InlineKeyboardMarkup:
    """Кнопка старосты «запустить опрос сейчас»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📣 Запустить опрос",
            callback_data=f"{CB_REQUEST_PREFIX}{date_iso}:{para}",
        )],
    ])


# --- опрос «Да/Нет» (миграция 14) ---

def check_poll_kb(date_iso: str, para: int) -> InlineKeyboardMarkup:
    """Кнопки опроса «Да/Нет»: «Я на паре» и «Меня нет».

    Две кнопки в одном ряду: студент отвечает в одно нажатие, а «нет» — такой
    же полноценный ответ, как «да» (он сразу пишет ``absent``).

    Args:
        date_iso: дата пары.
        para: номер пары.

    Returns:
        InlineKeyboardMarkup с двумя кнопками.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=atext.CHECK_BUTTON_YES,
            callback_data=f"{CB_CHECK_PREFIX}{date_iso}:{para}:yes",
        ),
        InlineKeyboardButton(
            text=atext.CHECK_BUTTON_NO,
            callback_data=f"{CB_CHECK_PREFIX}{date_iso}:{para}:no",
        ),
    ]])


def attendance_mode_kb(current: str) -> InlineKeyboardMarkup:
    """Кнопки выбора режима посещаемости (староста).

    Текущий режим помечается галочкой в подписи — как в остальных экранах
    настроек проекта, чтобы было видно, что уже выбрано.

    Args:
        current: текущий режим (``chat`` или ``direct``).

    Returns:
        InlineKeyboardMarkup: выбор режима + «🔙 Назад».
    """
    from bot.attendance import keyboards as group_kb
    from bot.attendance.models import MODE_DIRECT

    direct_mark = " ✅" if current == MODE_DIRECT else ""
    chat_mark = "" if current == MODE_DIRECT else " ✅"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"📱 Личка{direct_mark}",
                                 callback_data="grp:att_mode:direct"),
            InlineKeyboardButton(text=f"💬 Чат группы{chat_mark}",
                                 callback_data="grp:att_mode:chat"),
        ],
        [InlineKeyboardButton(text="🔙 Назад",
                              callback_data=group_kb.CB_MANAGE)],
    ])