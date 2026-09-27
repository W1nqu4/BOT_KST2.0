"""Регистрация: /start, ввод группы, дашборд.

Поток пользователя:

1. /start — если группы нет, бот просит её ввести (FSM, GroupForm.waiting_group);
2. ввод текста проверяется: нормализация → формат → наличие в расписании;
3. если группы нет в расписании — показываем до трёх похожих (difflib);
4. сохранение и дашборд с главным меню.

Данные из БД экранируются через :func:`html.escape` — в названиях групп и
предметов возможны символы ``<``, ``>``, ``&``.
"""

import difflib
import logging
import re
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import db
from bot.keyboards import inline as inline_kb
from bot.keyboards import reply as reply_kb
from bot.keyboards.inline import group_suggestions_kb
from bot.parsers.groups import normalize_group_name
from bot.services.schedule_service import (
    get_lessons_for_day,
    week_type_for_date,
)

logger = logging.getLogger(__name__)

router = Router(name="start")

# Допустимый формат группы: буквы/цифры/дефис/слэш, 2..12 символов.
GROUP_PATTERN = re.compile(r"^[А-ЯЁA-Z0-9\-/]{2,12}$")

# Сколько похожих групп предлагать.
MAX_SUGGESTIONS = 3


class GroupForm(StatesGroup):
    """Состояния регистрации."""

    waiting_group = State()


GREETING = (
    "👋 <b>Привет!</b> Это бот расписания КСТ.\n"
    "Введи номер группы (например, <code>26КАД</code>):"
)


def is_valid_group(text: str) -> bool:
    """Проверить формат группы после нормализации.

    Args:
        text: уже нормализованное имя группы.

    Returns:
        True, если имя подходит под ``GROUP_PATTERN``.
    """
    return bool(GROUP_PATTERN.match(text))


def suggest_groups(raw: str, available: list[str]) -> list[str]:
    """Найти до трёх похожих групп в расписании (difflib).

    Args:
        raw: то, что ввёл пользователь (после нормализации).
        available: список групп из кэша расписания.

    Returns:
        Список ближайших названий (может быть пустым).
    """
    return difflib.get_close_matches(raw, available, n=MAX_SUGGESTIONS, cutoff=0.5)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, conn) -> None:
    """Обработать /start: приветствие и просьба о группе либо дашборд."""
    tg_id = message.from_user.id if message.from_user else 0
    group = db.get_user_group(conn, tg_id)

    if not group:
        await state.set_state(GroupForm.waiting_group)
        await message.answer(GREETING, parse_mode="HTML")
        return

    await state.clear()
    await message.answer(
        greet_text(message.from_user.full_name if message.from_user else ""),
        parse_mode="HTML",
        reply_markup=reply_kb.main_kb(),
    )
@router.message(GroupForm.waiting_group)
async def process_group(message: Message, state: FSMContext, conn) -> None:
    """Проверить введённую группу, сохранить и показать дашборд."""
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await message.answer(
            "Сначала выбери группу: напиши её номер, например <code>26КАД</code>.",
            parse_mode="HTML",
        )
        return

    group = normalize_group_name(raw)
    available = db.list_available_groups(conn)

    if not is_valid_group(group):
        await message.answer(
            "❌ Такой номер группы не похож на настоящий.\n"
            "Пример: <code>26КАД</code>. Попробуй ещё раз:",
            parse_mode="HTML",
        )
        return

    if available and group not in available:
        suggestions = suggest_groups(group, available)
        text = f"🤔 Группы <code>{escape(group)}</code> нет в расписании."
        if suggestions:
            text += "\n\nПохожие группы:"
            await message.answer(
                text, parse_mode="HTML",
                reply_markup=group_suggestions_kb(suggestions),
            )
        else:
            text += "\nПроверь номер и напиши ещё раз."
            await message.answer(text, parse_mode="HTML")
        return

    db.upsert_user(
        conn, message.from_user.id if message.from_user else 0, group,
        message.from_user.full_name if message.from_user else "",
    )
    await state.clear()

    await message.answer(
        f"✅ Группа <b>{escape(group)}</b> сохранена.",
        parse_mode="HTML",
        reply_markup=reply_kb.main_kb(),
    )
    await send_dashboard(message, conn, group)


@router.callback_query(F.data.startswith("group:pick:"))
async def pick_suggested_group(callback: CallbackQuery, state: FSMContext,
                               conn) -> None:
    """Пользователь выбрал группу из предложенных кнопкой."""
    group = (callback.data or "").removeprefix("group:pick:")
    db.upsert_user(
        conn, callback.from_user.id, group, callback.from_user.full_name,
    )
    await state.clear()
    if callback.message is not None:
        await callback.message.answer(
            f"✅ Группа <b>{escape(group)}</b> сохранена.",
            parse_mode="HTML",
            reply_markup=reply_kb.main_kb(),
        )
        await send_dashboard(callback.message, conn, group)
    await callback.answer()


@router.callback_query(F.data == inline_kb.CB_CHANGE_GROUP)
async def change_group(callback: CallbackQuery, state: FSMContext) -> None:
    """«Сменить группу» — снова просим номер группы."""
    await state.set_state(GroupForm.waiting_group)
    if callback.message is not None:
        await callback.message.answer(GREETING, parse_mode="HTML")
    await callback.answer()


def greet_text(full_name: str) -> str:
    """Текст приветствия для уже зарегистрированного пользователя."""
    name = escape(full_name) if full_name else "студент"
    return f"👋 С возвращением, <b>{name}</b>!"


async def send_dashboard(message: Message, conn, group: str) -> None:
    """Отправить дашборд: группа, чётность на сегодня, число пар.

    Args:
        message: сообщение, в ответ на которое отправляем.
        conn: соединение SQLite.
        group: группа пользователя.
    """
    today = date.today()
    lessons = get_lessons_for_day(conn, group, today)
    week_type = week_type_for_date(today)
    day = inline_kb.day_name(today.isoweekday())

    text = (
        "🎓 <b>Группа:</b> {group}\n"
        "📅 <b>Сегодня:</b> {day}, {dmy}\n"
        "🗓 <b>Число:</b> {num} → <b>{wt}</b>\n"
        "📚 <b>Пар сегодня:</b> {count}\n\n"
        "Выбери раздел меню ниже 👇"
    ).format(
        group=escape(group),
        day=escape(day),
        dmy=today.strftime("%d.%m.%Y"),
        num=today.day,
        wt=escape(week_type),
        count=len(lessons),
    )
    await message.answer(
        text, parse_mode="HTML", reply_markup=inline_kb.dashboard_kb(),
    )
