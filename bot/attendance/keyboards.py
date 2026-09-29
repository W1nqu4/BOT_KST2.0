"""Клавиатуры посещаемости (этап 1).

Reply-меню и inline-плитки группы. Callback-данные держим с префиксом ``grp:``
— они попадают в уже отправленные сообщения, поэтому формат стабильный.
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Тексты кнопок главного меню (единый источник для обработчиков).
BTN_SCHEDULE = "📆 Расписание"
BTN_DEADLINES = "📝 Дедлайны"
BTN_MY_GROUP = "📊 Моя группа"
BTN_PROFILE = "👤 Профиль"

# Callback-данные раздела «Моя группа».
CB_ENTER_CODE = "grp:enter_code"
CB_CREATE = "grp:create"
CB_PICK_GROUP_PREFIX = "grp:pick:"     # grp:pick:25КАД
CB_MARK = "grp:mark"
CB_MY_ATTENDANCE = "grp:my_att"
CB_LIST = "grp:list"
CB_MANAGE = "grp:manage"
CB_MARK_MANUAL = "grp:mark_manual"
CB_REPORT = "grp:report"
CB_SHOW_CODE = "grp:show_code"
CB_NEW_CODE = "grp:new_code"
CB_MAKE_DEPUTY = "grp:make_deputy"
CB_DEPUTY_PREFIX = "grp:deputy:"       # grp:deputy:{tg_id}
CB_BACK = "grp:back"
CB_MENU = "menu:home"

# Заглушки этапа 2 (кнопки есть, ответ — «скоро»).
# Кнопки «Моя группа». Заглушек больше нет: все ведут в реальные обработчики
# посещаемости (этап 2) — CB_MARK → /attendance, CB_MY_ATTENDANCE → /my_attendance,
# CB_MARK_MANUAL → /mark, CB_REPORT → /report_week.
STUB_CALLBACKS: frozenset[str] = frozenset()


def main_kb() -> ReplyKeyboardMarkup:
    """Главное меню с разделом посещаемости.

    Layout:
        [📆 Расписание] [📝 Дедлайны]
        [📊 Моя группа] [👤 Профиль]

    Returns:
        ReplyKeyboardMarkup с четырьмя кнопками.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SCHEDULE),
             KeyboardButton(text=BTN_DEADLINES)],
            [KeyboardButton(text=BTN_MY_GROUP),
             KeyboardButton(text=BTN_PROFILE)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел или напиши группу",
    )


def my_group_not_registered_kb() -> InlineKeyboardMarkup:
    """Меню регистрации: ввести код или создать группу."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔢 Ввести код",
                                 callback_data=CB_ENTER_CODE),
            InlineKeyboardButton(text="⭐ Создать группу",
                                 callback_data=CB_CREATE),
        ],
    ])


def my_group_student_kb() -> InlineKeyboardMarkup:
    """Плитки студента: отметка и своя посещаемость (этап 2), список, меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Отметиться на паре",
                              callback_data=CB_MARK)],
        [InlineKeyboardButton(text="📊 Моя посещаемость",
                              callback_data=CB_MY_ATTENDANCE)],
        [InlineKeyboardButton(text="📋 Список группы", callback_data=CB_LIST)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data=CB_MENU)],
    ])


def my_group_starosta_kb() -> InlineKeyboardMarkup:
    """Плитки старосты/зама: отметка вручную, отчёт, управление, список."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Отметить вручную",
                              callback_data=CB_MARK_MANUAL)],
        [InlineKeyboardButton(text="📊 Отчёт за неделю",
                              callback_data=CB_REPORT)],
        [InlineKeyboardButton(text="⚙️ Управление группой",
                              callback_data=CB_MANAGE)],
        [InlineKeyboardButton(text="📋 Список группы", callback_data=CB_LIST)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data=CB_MENU)],
    ])
def group_management_kb() -> InlineKeyboardMarkup:
    """Управление группой: код, перегенерация, назначение зама, назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔑 Показать код",
                                 callback_data=CB_SHOW_CODE),
            InlineKeyboardButton(text="🔄 Новый код", callback_data=CB_NEW_CODE),
        ],
        [InlineKeyboardButton(text="👤 Назначить зама",
                              callback_data=CB_MAKE_DEPUTY)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=CB_BACK)],
    ])


def profile_inline_kb() -> InlineKeyboardMarkup:
    """Профиль: БЕЗ кнопки «Отметиться» (отметка живёт в «Моя группа»).

    Returns:
        InlineKeyboardMarkup: изменить данные, календарь, обратная связь, меню.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить данные",
                              callback_data="profile:edit")],
        [InlineKeyboardButton(text="📆 Интеграция с календарём",
                              callback_data="profile:calendar")],
        [InlineKeyboardButton(text="🐛 Сообщить о проблеме",
                              callback_data="profile:feedback")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data=CB_MENU)],
    ])


def group_suggestions_kb(groups: list[str]) -> InlineKeyboardMarkup:
    """Кнопки с похожими группами (когда введённой нет в расписании).

    Args:
        groups: до трёх ближайших названий.

    Returns:
        InlineKeyboardMarkup с кнопками выбора группы.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name,
                              callback_data=f"{CB_PICK_GROUP_PREFIX}{name}")]
        for name in groups
    ])


def deputy_candidates_kb(students: list[dict]) -> InlineKeyboardMarkup:
    """Список студентов для назначения зама."""
    rows = [
        [InlineKeyboardButton(
            text=str(student.get("full_name") or "без имени")[:40],
            callback_data=f"{CB_DEPUTY_PREFIX}{student['tg_id']}",
        )]
        for student in students
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад",
                                      callback_data=CB_MANAGE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def new_code_kb() -> InlineKeyboardMarkup:
    """Кнопка перегенерации кода под карточкой старосты."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сгенерировать новый",
                              callback_data=CB_NEW_CODE)],
    ])


def back_to_group_kb() -> InlineKeyboardMarkup:
    """Кнопки возврата из вложенных экранов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=CB_BACK)],
    ])


# --- админ-панель (этап 1: роль администратора) ---

CB_ADM_GROUPS = "adm:groups"
CB_ADM_STUDENTS = "adm:students"
CB_ADM_FIND = "adm:find"
CB_ADM_DELETE = "adm:delete"
CB_ADM_DELETE_OK_PREFIX = "adm:del_ok:"     # adm:del_ok:25КАД
CB_ADM_BROADCAST = "adm:broadcast"
CB_ADM_BC_SEND = "adm:bc_send"
CB_ADM_BACK = "adm:back"


def admin_panel_kb() -> InlineKeyboardMarkup:
    """Клавиатура админ-панели.

    Layout:
        [📋 Все группы]   [👥 Все студенты]
        [🔍 Найти группу] [🗑 Удалить группу]
        [📢 Рассылка]
        [🏠 Меню]
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Все группы",
                                 callback_data=CB_ADM_GROUPS),
            InlineKeyboardButton(text="👥 Все студенты",
                                 callback_data=CB_ADM_STUDENTS),
        ],
        [
            InlineKeyboardButton(text="🔍 Найти группу",
                                 callback_data=CB_ADM_FIND),
            InlineKeyboardButton(text="🗑 Удалить группу",
                                 callback_data=CB_ADM_DELETE),
        ],
        [InlineKeyboardButton(text="📢 Рассылка",
                              callback_data=CB_ADM_BROADCAST)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data=CB_MENU)],
    ])


def admin_back_kb() -> InlineKeyboardMarkup:
    """Возврат в админ-панель."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=CB_ADM_BACK)],
    ])


def delete_confirm_kb(group_name: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления группы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Удалить",
                callback_data=f"{CB_ADM_DELETE_OK_PREFIX}{group_name}",
            ),
            InlineKeyboardButton(text="↩️ Отмена",
                                 callback_data=CB_ADM_BACK),
        ],
    ])


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Подтверждение рассылки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить",
                                 callback_data=CB_ADM_BC_SEND),
            InlineKeyboardButton(text="↩️ Отмена",
                                 callback_data=CB_ADM_BACK),
        ],
    ])