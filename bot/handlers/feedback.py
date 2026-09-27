"""Обратная связь: кнопка «🐛 Сообщить о проблеме» (шаг 11).

Пользователь пишет одним сообщением, бот добавляет контекст (кто, из какой
группы, где был в боте) и пересылает в ``settings.admin_chat_id``.

Текст пользователя экранируется: он может содержать ``<``, ``>``, ``&``,
а сообщение отправляется с ``parse_mode='HTML'`` — иначе разметка сломается
или пользователь сможет вставить чужую ссылку.
"""

import logging
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from bot import db
from bot.config import TIMEZONE
from bot.keyboards import reply as reply_kb
from bot.state import get_last_screen, screen_label

logger = logging.getLogger(__name__)

router = Router(name="feedback")

# Версия бота: попадает в сообщение админу, чтобы понимать, о какой сборке речь.
BOT_VERSION = "0.11"

PROMPT_TEXT = (
    "🐛 <b>Сообщить о проблеме</b>\n\n"
    "Опиши проблему одним сообщением. Можно с примером: "
    "что делал, что ожидал, что получилось.\n\n"
    f"Отменить — кнопкой «{reply_kb.BTN_CANCEL}»."
)

THANKS_TEXT = (
    "🙏 <b>Спасибо, передали администратору.</b>\n"
    "<i>Если понадобится уточнить — ответим здесь.</i>"
)

CANCELLED_TEXT = "↩️ Отмена. Если что-то сломано — напиши, разберёмся."

# Если админ-чат не настроен, сообщение всё равно логируем.
NO_ADMIN_CHAT_WARNING = (
    "⚠️ Сообщение записано в лог: админ-чат не настроен (ADMIN_CHAT_ID)."
)


class Feedback(StatesGroup):
    """Состояния обратной связи."""

    waiting_text = State()


def build_feedback_report(user, text: str, group: str | None,
                          last_screen: str, version: str = BOT_VERSION,
                          moment: datetime | None = None) -> str:
    """Собрать сообщение для админа.

    Args:
        user: объект пользователя Telegram (``from_user``).
        text: текст пользователя (экранируется).
        group: группа пользователя или None.
        last_screen: идентификатор последнего экрана.
        version: версия бота.
        moment: момент времени (для тестов).

    Returns:
        HTML-текст сообщения.
    """
    now = moment or datetime.now(TIMEZONE)
    name = escape(user.full_name or "без имени")
    username = f"@{escape(user.username)}" if user.username else "без username"
    user_id = user.id

    return (
        "🐛 <b>Обратная связь</b>\n"
        f"От: {name} ({username}, <a href=\"tg://user?id={user_id}\">профиль</a>)\n"
        f"ID: <code>{user_id}</code>\n"
        f"Группа: <b>{escape(group or 'не выбрана')}</b>\n"
        f"Экран: {escape(screen_label(last_screen))}\n"
        f"Версия: {escape(version)}\n"
        f"Время: {now.strftime('%d.%m.%Y %H:%M')}\n\n"
        "———\n"
        f"{escape(text)}\n"
        "———"
    )


@router.message(F.text == reply_kb.BTN_FEEDBACK)
async def btn_feedback(message: Message, state: FSMContext) -> None:
    """Кнопка «🐛 Сообщить о проблеме»: просим описание."""
    await state.set_state(Feedback.waiting_text)
    await message.answer(PROMPT_TEXT, parse_mode="HTML")


@router.message(Feedback.waiting_text, F.text == reply_kb.BTN_CANCEL)
async def feedback_cancel(message: Message, state: FSMContext) -> None:
    """Отмена: выходим из FSM, ничего не отправляем."""
    await state.clear()
    await message.answer(CANCELLED_TEXT, reply_markup=reply_kb.main_kb())


@router.message(Feedback.waiting_text)
async def feedback_take_text(message: Message, state: FSMContext, conn,
                             settings, bot) -> None:
    """Принять описание, добавить контекст и переслать админу."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Опиши проблему текстом (или отмени).")
        return

    tg_id = message.from_user.id if message.from_user else 0
    group = db.get_user_group(conn, tg_id)
    last_screen = await get_last_screen(state)
    await state.clear()

    report = build_feedback_report(
        message.from_user, text, group, last_screen,
    )

    chat_id = getattr(settings, "admin_chat_id", None) if settings else None
    if chat_id:
        try:
            await bot.send_message(chat_id, report, parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("feedback: could not deliver to admin chat",
                           extra={"error": repr(exc)})
            await message.answer(
                "⚠️ Не удалось передать сообщение. Попробуй позже.",
            )
            return
    else:
        logger.warning("feedback: admin chat not configured",
                       extra={"tg_id": tg_id})
        await message.answer(NO_ADMIN_CHAT_WARNING, parse_mode="HTML")

    logger.info("feedback received", extra={"tg_id": tg_id, "screen": last_screen})
    await message.answer(THANKS_TEXT, parse_mode="HTML",
                         reply_markup=reply_kb.main_kb())