"""Интеграция с календарём: ссылки подписки и инструкции (шаг 9).

Пользователь получает персональную ссылку ``/calendar/{token}.ics`` и
инструкцию под свою платформу. Кнопка «Проверить» присылает .ics-файл с одним
событием через пару минут — так видно, что календарь рабочий, не дожидаясь
реальной пары.
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.keyboards import reply as reply_kb
from bot.services import ics_service

logger = logging.getLogger(__name__)

router = Router(name="calendar")

BTN_CALENDAR = "📆 Интеграция с календарём"

NO_GROUP_TEXT = (
    "Сначала выбери группу: /start → пришли номер группы "
    "(например, <code>26КАД</code>)."
)

IOS_INSTRUCTION = (
    "📱 <b>Подписка на iPhone / iPad</b>\n\n"
    "1. Скопируй ссылку ниже\n"
    "2. Настройки → Календарь → Учётные записи → Другое\n"
    "3. Добавить подписной календарь\n"
    "4. Вставь ссылку и сохрани\n\n"
    "<code>{link}</code>"
)

ANDROID_INSTRUCTION = (
    "🤖 <b>Подписка на Android</b>\n\n"
    "1. Скопируй ссылку ниже\n"
    "2. Google Calendar → Другие календари → «+»\n"
    "3. Создать по URL\n"
    "4. Вставь ссылку и сохрани\n\n"
    "<code>{link}</code>"
)


def calendar_kb(https_link: str, webcal_link: str) -> InlineKeyboardMarkup:
    """Клавиатура интеграции: платформы, скачивание файла, проверка, назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 iPhone / iPad", callback_data="cal:ios"),
            InlineKeyboardButton(text="🤖 Android", callback_data="cal:android"),
        ],
        [
            InlineKeyboardButton(text="📥 Скачать .ics", url=https_link),
        ],
        [
            InlineKeyboardButton(text="✅ Проверить", callback_data="cal:test"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="cal:back"),
        ],
    ])


async def send_calendar_links(message: Message, conn, tg_id: int,
                              public_base_url: str) -> None:
    """Показать персональные ссылки подписки и способы подключения."""
    group = db.get_user_group(conn, tg_id)
    if not group:
        await message.answer(NO_GROUP_TEXT, parse_mode="HTML")
        return

    token = ics_service.get_or_create_token(conn, tg_id)
    https_link = ics_service.build_calendar_url(public_base_url, token)
    webcal_link = ics_service.build_webcal_url(public_base_url, token)

    text = (
        "📆 <b>Интеграция с календарём</b>\n\n"
        f"🎓 Группа: <b>{group}</b>\n"
        "📚 Горизонт: 60 дней вперёд\n\n"
        "<b>Ссылка для подписки (https):</b>\n"
        f"<code>{https_link}</code>\n\n"
        "<b>Ссылка для iOS (webcal):</b>\n"
        f"<code>{webcal_link}</code>\n\n"
        "Календарь обновляется автоматически. Выбери свою платформу 👇"
    )
    await message.answer(
        text, parse_mode="HTML",
        reply_markup=calendar_kb(https_link, webcal_link),
        disable_web_page_preview=True,
    )


def _base_url(settings) -> str:
    """Базовый URL приложения из настроек (пустая строка, если нет)."""
    return getattr(settings, "public_base_url", "") if settings else ""


@router.message(F.text == BTN_CALENDAR)
async def btn_calendar(message: Message, conn, settings, state: FSMContext) -> None:
    """Кнопка «📆 Интеграция с календарём»."""
    from bot.state import SCREEN_CALENDAR_MAIN, set_last_screen

    base = _base_url(settings)
    if not base:
        await message.answer(
            "⚠️ Ссылки недоступны: не задан PUBLIC_BASE_URL.", parse_mode="HTML",
        )
        return
    await set_last_screen(state, SCREEN_CALENDAR_MAIN)
    await send_calendar_links(
        message, conn, message.from_user.id if message.from_user else 0, base,
    )


@router.callback_query(F.data == "cal:ios")
async def cb_ios(callback: CallbackQuery, conn, settings) -> None:
    """Инструкция для iOS с webcal-ссылкой."""
    token = ics_service.get_or_create_token(conn, callback.from_user.id)
    link = ics_service.build_webcal_url(_base_url(settings), token)
    if callback.message is not None:
        await callback.message.answer(
            IOS_INSTRUCTION.format(link=link), parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "cal:android")
async def cb_android(callback: CallbackQuery, conn, settings) -> None:
    """Инструкция для Android с https-ссылкой."""
    token = ics_service.get_or_create_token(conn, callback.from_user.id)
    link = ics_service.build_calendar_url(_base_url(settings), token)
    if callback.message is not None:
        await callback.message.answer(
            ANDROID_INSTRUCTION.format(link=link), parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "cal:test")
async def cb_test(callback: CallbackQuery, conn) -> None:
    """Прислать .ics-файл с одним событием через 2 минуты."""
    group = db.get_user_group(conn, callback.from_user.id) or "без группы"
    ics_text = ics_service.build_test_ics(group, minutes_ahead=2)
    document = BufferedInputFile(
        ics_text.encode("utf-8"), filename="kst-check.ics",
    )
    if callback.message is not None:
        await callback.message.answer_document(
            document,
            caption=(
                "✅ <b>Проверка календаря</b>\n\n"
                "Открой файл — внутри одно событие через 2 минуты. "
                "Если оно появилось, подписка настроена верно."
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "cal:back")
async def cb_back(callback: CallbackQuery) -> None:
    """Вернуться к главному меню."""
    if callback.message is not None:
        await callback.message.answer(
            "🏠 Главное меню — используй кнопки ниже.",
            reply_markup=reply_kb.main_kb(),
        )
    await callback.answer()
