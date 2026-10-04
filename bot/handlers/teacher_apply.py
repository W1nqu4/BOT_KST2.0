"""Заявка на роль преподавателя: /teacher_apply и /teacher_status.

Поток: преподаватель выбирает своё ФИО из справочника →
заявка ``pending`` → админ одобряет (отдельный шаг) → доступ.

Почему с модерацией: без неё любой мог бы назваться чужим ФИО и увидеть чужие
группы и расписание. ФИО принимается ТОЛЬКО из справочника
(:data:`bot.parsers.teachers.TEACHERS`) — введённое вручную не сохраняется,
иначе «подделка под коллегу» проходила бы автоматически.

Роль преподавателя живёт отдельно от роли студента (таблица ``teachers``):
у преподавателя может быть своя группа, и студентом он от этого не перестаёт.

Этот модуль — только регистрация и статус. Поиск расписания преподавателя
уже реализован в :mod:`bot.handlers.teacher` (команда ``/teacher``).
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.parsers.teachers import PLACEHOLDER_MARK, TEACHERS

logger = logging.getLogger(__name__)

router = Router(name="teacher_apply")

# Сколько ФИО показывать кнопками за раз (лимит Telegram — 100 кнопок,
# но длинный список нечитаем).
MAX_CHOICES = 20

# Сколько первых ФИО показывать по кнопке «Показать список».
FIRST_PAGE = 20

# Callback-данные выбора ФИО.
CB_PICK_PREFIX = "tapply:pick:"
CB_CANCEL = "tapply:cancel"
CB_LIST = "tapply:list"

CANCEL_WORDS = {"отмена", "/cancel", "cancel", "стоп"}


class TeacherApply(StatesGroup):
    """Состояния заявки на роль преподавателя."""

    waiting_name = State()


def available_names() -> list[str]:
    """ФИО из справочника, доступные для заявки.

    Записи с пометкой :data:`bot.parsers.teachers.PLACEHOLDER_MARK` не
    предлагаются: там настоящего имени ещё нет («Аверина (ФИО уточняется)»),
    и одобрить такую заявку админ не смог бы — непонятно, кто это.

    Returns:
        Отсортированный список полных ФИО.
    """
    return sorted(
        value for value in TEACHERS.values()
        if PLACEHOLDER_MARK not in value and not db.is_vacancy(value)
    )


def match_names(query: str, limit: int = MAX_CHOICES) -> list[str]:
    """Найти ФИО по подстроке (регистр и ё/е не важны).

    Args:
        query: то, что ввёл пользователь («Богатырева», «богатыр»).
        limit: максимум совпадений.

    Returns:
        Список полных ФИО (может быть пустым).
    """
    needle = (query or "").strip().lower().replace("ё", "е")
    if not needle:
        return []
    found = [
        name for name in available_names()
        if needle in name.lower().replace("ё", "е")
    ]
    return found[:limit]
def names_kb(names: list[str]) -> InlineKeyboardMarkup:
    """Кнопки выбора ФИО + отмена."""
    rows = [
        [InlineKeyboardButton(text=name,
                              callback_data=f"{CB_PICK_PREFIX}{index}")]
        for index, name in enumerate(names)
    ]
    rows.append(
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=CB_CANCEL)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_kb() -> InlineKeyboardMarkup:
    """Клавиатура первого шага: показать список или отменить."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Показать список",
                              callback_data=CB_LIST)],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=CB_CANCEL)],
    ])


ASK_NAME = (
    "👨‍🏫 <b>Заявка на роль преподавателя</b>\n\n"
    "Напиши свою фамилию — я покажу совпадения из справочника.\n"
    "Например: <code>Богатырева</code>\n\n"
    "<i>ФИО выбирается из справочника КСТ; заявку рассматривает админ.</i>\n\n"
    "Отмена: /teacher_cancel"
)

NOT_FOUND = (
    "🤔 Не нашёл ФИО с «{query}».\n"
    "Попробуй только фамилию (например, <code>Богатырева</code>)."
)

TOO_MANY = (
    "Слишком много совпадений ({count}). Уточни запрос — "
    "напиши фамилию целиком."
)

ASK_QUERY_TEXT = (
    "Напиши фамилию текстом — например, <code>Богатырева</code>."
)


def application_kb() -> InlineKeyboardMarkup:
    """Кнопка «Обработать заявки» для уведомления админу.

    Ведёт на общий список заявок (callback ``teacher_refresh``): из уведомления
    админ попадает прямо к модерации, не вспоминая команду.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚙️ Обработать заявки",
            callback_data="teacher_refresh",
        )],
    ])


def status_rus(status: str) -> str:
    """Человекочитаемый статус заявки."""
    return {
        db.TEACHER_PENDING: "⏳ ожидает проверки",
        db.TEACHER_APPROVED: "✅ одобрена",
        db.TEACHER_REJECTED: "❌ отклонена",
    }.get(status, status)
# --- заявка ---

@router.message(Command("teacher_apply"))
async def cmd_teacher_apply(message: Message, state: FSMContext, conn) -> None:
    """Начать заявку на роль преподавателя (или показать текущий статус)."""
    if message.from_user is None:
        return
    tg_id = message.from_user.id

    existing = db.get_teacher(conn, tg_id)
    if existing:
        status = str(existing["status"])
        name = escape(str(existing["full_name"]))
        if status == db.TEACHER_PENDING:
            await message.answer(
                "⏳ Твоя заявка уже отправлена.\n\n"
                f"ФИО: {name}\n"
                "Статус: ожидает проверки админом.\n\n"
                "Отменить: /teacher_cancel",
                parse_mode="HTML",
            )
        elif status == db.TEACHER_APPROVED:
            await message.answer(
                "✅ Ты уже преподаватель.\n\n"
                f"ФИО: {name}\n\n"
                "Команды: /teacher (расписание), /profile",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "❌ Твоя предыдущая заявка была отклонена.\n"
                "Написать админу: @W1nqu4",
                parse_mode="HTML",
            )
        return

    await state.set_state(TeacherApply.waiting_name)
    await message.answer(ASK_NAME, parse_mode="HTML", reply_markup=start_kb())


@router.callback_query(F.data == CB_LIST)
async def cb_list_names(callback: CallbackQuery, state: FSMContext) -> None:
    """Показать первые ФИО справочника кнопками."""
    names = available_names()[:FIRST_PAGE]
    await state.update_data(teacher_matches=names)
    if callback.message is not None:
        await callback.message.answer(
            "Выбери своё ФИО (или напиши фамилию для поиска):",
            reply_markup=names_kb(names),
        )
    await callback.answer()


@router.callback_query(F.data == CB_CANCEL)
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить заявку на этапе выбора ФИО."""
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.message(TeacherApply.waiting_name)
async def process_name(message: Message, state: FSMContext, conn) -> None:
    """Найти ФИО по подстроке и показать совпадения кнопками."""
    query = (message.text or "").strip()

    if query.lower() in CANCEL_WORDS:
        await state.clear()
        await message.answer("Отменено.")
        return

    # Команда вместо фамилии: подсказываем и остаёмся в шаге.
    if query.startswith("/"):
        await message.answer(ASK_QUERY_TEXT, parse_mode="HTML")
        return

    matches = match_names(query)
    if not matches:
        await message.answer(
            NOT_FOUND.format(query=escape(query)), parse_mode="HTML",
        )
        return

    if len(matches) > MAX_CHOICES:
        await message.answer(
            TOO_MANY.format(count=len(matches)), parse_mode="HTML",
        )
        return

    await state.update_data(teacher_matches=matches)
    await message.answer(
        "Выбери своё ФИО:", reply_markup=names_kb(matches),
    )
@router.callback_query(F.data.startswith(CB_PICK_PREFIX))
async def cb_pick_name(callback: CallbackQuery, state: FSMContext, conn,
                       settings=None) -> None:
    """Создать заявку с выбранным ФИО из справочника."""
    if callback.from_user is None or callback.data is None:
        return

    raw_index = callback.data[len(CB_PICK_PREFIX):]
    data = await state.get_data()
    matches = data.get("teacher_matches") or []
    try:
        index = int(raw_index)
    except ValueError:
        index = -1

    if index < 0 or index >= len(matches):
        # Список мог устареть (перезапуск бота, новая подборка).
        await callback.answer("Список устарел, напиши фамилию ещё раз",
                              show_alert=True)
        return

    full_name = str(matches[index])
    result = db.apply_teacher(conn, callback.from_user.id, full_name)
    await state.clear()

    if not result["ok"]:
        text = f"❌ {escape(str(result['error']))}"
    else:
        text = (
            "✅ <b>Заявка отправлена</b>\n\n"
            f"ФИО: <b>{escape(full_name)}</b>\n\n"
            "Админ рассмотрит её в ближайшее время.\n"
            "Отменить: /teacher_cancel"
        )
        # Уведомляем админа: без этого заявка ждала бы, пока он сам зайдёт.
        await notify_admin_about_application(
            callback.bot, conn, callback.from_user.id, full_name,
            settings=settings,
        )

    if callback.message is not None:
        await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message(Command("teacher_status"))
async def cmd_teacher_status(message: Message, conn) -> None:
    """Показать статус своей заявки."""
    if message.from_user is None:
        return
    teacher = db.get_teacher(conn, message.from_user.id)
    if teacher is None:
        await message.answer("У тебя нет заявки. Подать: /teacher_apply")
        return

    await message.answer(
        "<b>Заявка преподавателя</b>\n\n"
        f"ФИО: {escape(str(teacher['full_name']))}\n"
        f"Статус: {status_rus(str(teacher['status']))}\n"
        f"Подана: {escape(str(teacher['applied_at'])[:16])}",
        parse_mode="HTML",
    )


@router.message(Command("teacher_cancel"))
async def cmd_teacher_cancel(message: Message, conn) -> None:
    """Отменить свою заявку (только пока она не рассмотрена)."""
    if message.from_user is None:
        return

    if db.cancel_teacher_application(conn, message.from_user.id):
        await message.answer("✅ Заявка отменена.")
        return

    # Либо заявки нет, либо она уже рассмотрена.
    teacher = db.get_teacher(conn, message.from_user.id)
    if teacher is None:
        await message.answer("У тебя нет активной заявки.")
    else:
        await message.answer(
            "Заявку нельзя отменить: "
            f"статус {status_rus(str(teacher['status']))}.\n"
            "Написать админу: @W1nqu4",
            parse_mode="HTML",
        )


# --- уведомление админу ---

async def notify_admin_about_application(bot, conn, tg_id: int,
                                         full_name: str,
                                         settings=None) -> bool:
    """Сообщить админам о новой заявке преподавателя.

    Без уведомления заявка лежала бы до тех пор, пока админ сам не заглянет в
    список, — преподаватель ждал бы неизвестно сколько.

    Args:
        bot: объект Bot.
        conn: соединение SQLite.
        tg_id: кто подал заявку.
        full_name: ФИО из справочника.
        settings: настройки с ``admin_ids`` (в хендлерах приходит автоматически).

    Returns:
        True, если уведомление доставлено хотя бы одному админу.
    """
    admin_ids = tuple(getattr(settings, "admin_ids", ()) or ())
    pending = len(db.list_pending_teachers(conn))
    text = (
        "👨‍🏫 <b>Новая заявка преподавателя</b>\n\n"
        f"ФИО: <b>{escape(full_name)}</b>\n"
        f"От: id <code>{tg_id}</code>\n"
        f"Заявок в очереди: {pending}\n\n"
        "Одобрить или отклонить: /teachers"
    )

    if not admin_ids:
        logger.warning(
            "teacher application: ADMIN_IDS пуст, уведомление не отправлено",
            extra={"tg_id": tg_id},
        )
        return False

    delivered = False
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML",
                                   reply_markup=application_kb())
            delivered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "teacher application notify failed",
                extra={"admin_id": admin_id, "error": repr(exc)},
            )
    return delivered