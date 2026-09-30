"""Справка: /help, /settings и заглушки для разделов следующих шагов.

«Дедлайны» и «Профиль» станут настоящими на шаге 8; здесь они отвечают
«Раздел в разработке», чтобы кнопки главного меню не молчали.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.attendance import keyboards as att_kb
from bot.handlers.calendar import (
    BTN_CALENDAR,
    _base_url,
    send_calendar_links,
)
from bot.keyboards import inline as inline_kb
from bot.keyboards import reply as reply_kb

logger = logging.getLogger(__name__)

router = Router(name="help")

HELP_TEXT = (
    "🤖 <b>Что умеет бот</b>\n"
    "• Показывает расписание с учётом чёт/нечет по числу месяца\n"
    "• Листает дни ◀️ ▶️ и показывает замены\n"
    "• Ищет ближайшие пары по предмету («📚 Предметы» внутри расписания)\n"
    "• Ведёт твои дедлайны с напоминаниями\n"
    "• Даёт подписку на .ics-календарь (Google / Apple)\n"
    "• Группа и посещаемость: код от старосты, список группы\n"
    "🔔 За 5 минут до пары — напоминание с предметом и кабинетом\n\n"
    "<b>Команды</b>\n"
    "/start — начать, сменить группу\n"
    "/setup_schedule — указать группу для расписания (без кода)\n"
    "/mygroup — вступить в группу посещаемости по коду старосты\n"
    "/teacher — расписание преподавателя\n"
    "/help — эта справка\n"
    "/settings — настройки уведомлений\n"
    "/attendance — отметиться на паре\n"
    "/my_attendance — моя посещаемость и аттестация\n"
    "/vote — запустить голосование за отсутствующих (староста)\n"
    "/make_deputy — назначить зама (только староста)\n"
    "/admin — админ-панель (только для создателя)\n"
    "/make_starosta — назначить старосту (только для админа)\n"
    "/schedule — расписание на сегодня (в групповом чате)\n"
    "/setup — привязать чат к группе КСТ (для админа чата)\n"
    "/unsync — отвязать чат (для админа чата)\n\n"
    "<b>Меню</b>\n"
    "📆 Расписание · 📝 Дедлайны · 📊 Моя группа · 👤 Профиль\n\n"
    "<i>Добавь бота в чат группы — замены будут приходить туда "
    "автоматически.</i>\n\n"
    f"Что-то сломалось? Кнопка «{reply_kb.BTN_FEEDBACK}» — "
    "в профиле; сообщение уйдёт администратору вместе с контекстом.\n"
    "По вопросам работы бота — @W1nqu4."
)

SETTINGS_TEXT = (
    "⚙️ <b>Настройки</b>\n\n"
    "Тут можно включить или выключить уведомления о заменах."
)


def settings_kb(enabled: bool) -> InlineKeyboardMarkup:
    """Кнопка переключения уведомлений (подпись зависит от состояния)."""
    label = "🔕 Отключить уведомления" if enabled else "🔔 Включить уведомления"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data="settings:toggle"),
    ]])


def settings_text(enabled: bool) -> str:
    """Текст настроек с текущим состоянием уведомлений."""
    state = "включены 🔔" if enabled else "выключены 🔕"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Уведомления о заменах: <b>{state}</b>\n\n"
        "<i>Рассылка идёт в окне 15:30–23:00 — только новые замены "
        "на следующий учебный день.</i>"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка по боту."""
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("settings"))
async def cmd_settings(message: Message, conn, state: FSMContext) -> None:
    """Настройки: переключатель уведомлений о заменах."""
    from bot.state import SCREEN_PROFILE, set_last_screen

    tg_id = message.from_user.id if message.from_user else 0
    enabled = db.get_notifications_enabled(conn, tg_id)
    await set_last_screen(state, SCREEN_PROFILE)
    await message.answer(
        settings_text(enabled), parse_mode="HTML",
        reply_markup=settings_kb(enabled),
    )


@router.callback_query(F.data == "settings:toggle")
async def cb_settings_toggle(callback: CallbackQuery, conn) -> None:
    """Переключить уведомления и показать новое состояние."""
    tg_id = callback.from_user.id
    if db.get_user(conn, tg_id) is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    enabled = not db.get_notifications_enabled(conn, tg_id)
    db.set_notifications_enabled(conn, tg_id, enabled)

    if callback.message is not None:
        await callback.message.edit_text(
            settings_text(enabled), parse_mode="HTML",
            reply_markup=settings_kb(enabled),
        )
    await callback.answer("Уведомления включены" if enabled else "Уведомления выключены")


async def _profile_text(conn, tg_id: int) -> str:
    """Собрать текст «Профиля».

    Вынесено из обработчика: профиль рисуется и по кнопке «👤 Профиль», и при
    возврате «🔙 Назад» с экрана посещаемости — текст обязан совпадать.

    Args:
        conn: соединение SQLite.
        tg_id: пользователь.

    Returns:
        HTML-текст профиля.
    """
    from bot.services import deadline_service

    group = db.get_user_group(conn, tg_id)
    deadlines = deadline_service.list_active(conn, tg_id)
    notifications = db.get_notifications_enabled(conn, tg_id)
    return (
        "👤 <b>Профиль</b>\n\n"
        f"🎓 Группа: <b>{escape(group or 'не выбрана')}</b>\n"
        f"📝 Активных дедлайнов: <b>{len(deadlines)}</b>\n"
        f"🔔 Уведомления: <b>{'включены' if notifications else 'выключены'}</b>\n\n"
        "Подпишись на календарь — расписание появится в телефоне само."
    )


@router.message(F.text == reply_kb.BTN_PROFILE)
async def btn_profile(message: Message, conn, settings, state: FSMContext) -> None:
    """Профиль: группа, посещаемость, дедлайны и интеграция с календарём.

    Без группы показываем тот же выбор, что в приветствии и расписании:
    указать группу для расписания или ввести код старосты. Раньше здесь была
    тупиковая подсказка «сначала выбери группу — /start», из которой студент
    без кода не мог выйти.
    """
    from bot.attendance import texts as att_texts
    from bot.state import SCREEN_PROFILE, set_last_screen

    tg_id = message.from_user.id if message.from_user else 0
    group = db.get_user_group(conn, tg_id)
    if not group:
        await message.answer(
            att_texts.PROFILE_NO_GROUP_TEXT, parse_mode="HTML",
            reply_markup=inline_kb.profile_no_group_kb(),
        )
        return

    await set_last_screen(state, SCREEN_PROFILE)
    await message.answer(
        await _profile_text(conn, tg_id), parse_mode="HTML",
        reply_markup=att_kb.profile_inline_kb(),
    )


@router.callback_query(F.data == "profile:edit")
async def cb_profile_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """«✏️ Изменить данные» — сменить группу для расписания.

    Группа посещаемости меняется только через старосту (один студент — одна
    группа), поэтому здесь речь именно о группе для расписания.
    """
    from bot.handlers.start import GroupForm

    await state.set_state(GroupForm.waiting_group)
    if callback.message is not None:
        await callback.message.answer(
            "✏️ <b>Изменить данные</b>\n\n"
            "Введи номер группы для расписания (например, "
            "<code>26КАД</code>):",
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == att_kb.CB_PROFILE_BACK)
async def cb_profile_back(callback: CallbackQuery, conn, settings,
                          state: FSMContext) -> None:
    """«🔙 Назад» с экрана посещаемости — вернуться в «Профиль».

    Профиль перерисовывается тем же кодом, что и кнопка «👤 Профиль», поэтому
    тексты и клавиатура не разъезжаются.
    """
    from bot.attendance import keyboards as att_kb_local

    if callback.message is None:
        await callback.answer()
        return

    text = await _profile_text(conn, callback.from_user.id)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=att_kb_local.profile_inline_kb(),
        )
    except Exception:
        logger.debug("could not edit profile", exc_info=True)
    await callback.answer()


@router.callback_query(F.data == "profile:feedback")
async def cb_profile_feedback(callback: CallbackQuery, state: FSMContext) -> None:
    """Запустить обратную связь из профиля (в главном меню кнопки нет)."""
    from bot.handlers.feedback import PROMPT_TEXT, Feedback

    await state.set_state(Feedback.waiting_text)
    if callback.message is not None:
        await callback.message.answer(PROMPT_TEXT, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "profile:calendar")
async def cb_profile_calendar(callback: CallbackQuery, conn, settings) -> None:
    """Переход из профиля в раздел интеграции с календарём."""
    base = _base_url(settings)
    if callback.message is not None:
        if not base:
            await callback.message.answer(
                "⚠️ Ссылки недоступны: не задан PUBLIC_BASE_URL.",
                parse_mode="HTML",
            )
        else:
            await send_calendar_links(
                callback.message, conn, callback.from_user.id, base,
            )
    await callback.answer()