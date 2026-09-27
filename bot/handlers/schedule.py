"""Расписание: «Сегодня», «Расписание» с навигацией по дням, рендер карточек.

Все данные из БД экранируются через :func:`html.escape` (названия предметов,
ФИО, кабинеты), потому что сообщения отправляются с ``parse_mode='HTML'``.
"""

import logging
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import db
from bot.keyboards import inline as inline_kb
from bot.keyboards.reply import BTN_SCHEDULE, BTN_TODAY
from bot.services.schedule_service import (
    apply_substitutions,
    get_lessons_for_day,
    week_type_for_date,
)

logger = logging.getLogger(__name__)

router = Router(name="schedule")

# Иконки карточек.
ICON_PLANNED = "📚"       # плановая пара
ICON_SUBSTITUTION = "🔁"  # замена
ICON_CANCELLED = "❌"     # отменённая пара
ICON_SELF_STUDY = "📖"    # самостоятельная работа

NO_LESSONS = (
    "🎉 <b>Пар нет</b>\n"
    "<i>Отдыхай или закрой хвосты по дедлайнам.</i>"
)


def lesson_icon(lesson: dict) -> str:
    """Иконка карточки по состоянию пары.

    Приоритет: отмена → самостоятельная работа → замена → плановая пара.
    """
    if lesson.get("is_cancelled"):
        return ICON_CANCELLED
    if lesson.get("is_self_study"):
        return ICON_SELF_STUDY
    if lesson.get("is_substitution"):
        return ICON_SUBSTITUTION
    return ICON_PLANNED


def render_day(group: str, d: date, lessons: list[dict]) -> str:
    """Собрать текст дня: шапка + карточки пар.

    Args:
        group: имя группы.
        d: дата.
        lessons: результат :func:`get_lessons_for_day` после
            :func:`apply_substitutions`.

    Returns:
        HTML-текст сообщения. Пустой день — отдельная дружелюбная строчка.
    """
    day_name = inline_kb.day_name(d.isoweekday())
    week_type = week_type_for_date(d)

    header = (
        f"📅 <b>{escape(day_name)}, {d.strftime('%d.%m.%Y')}</b>\n"
        f"🎓 Группа: <b>{escape(group)}</b>\n"
        f"🗓 Число: {d.day} → <b>{escape(week_type)}</b>"
    )

    if not lessons:
        return f"{header}\n\n{NO_LESSONS}"

    cards = [header]
    for lesson in lessons:
        cards.append(_render_card(lesson))
    return "\n\n".join(cards)


def _render_card(lesson: dict) -> str:
    """Карточка одной пары."""
    lines = [
        f"{lesson_icon(lesson)} <b>{lesson['para_number']} пара</b>",
        f"<b>{escape(lesson.get('subject') or '')}</b>",
    ]
    teacher = lesson.get("teacher") or ""
    if teacher:
        lines.append(f"👤 {escape(teacher)}")
    room = lesson.get("room") or ""
    if room:
        lines.append(f"🚪 {escape(room)}")
    time_range = lesson.get("time_range") or ""
    if time_range:
        lines.append(f"⏰ {escape(time_range)}")

    if lesson.get("is_cancelled"):
        lines.append("<i>Пара отменена</i>")
    elif lesson.get("is_self_study"):
        lines.append("<i>Самостоятельная работа</i>")
    elif lesson.get("is_substitution") and lesson.get("planned_subject"):
        lines.append(f"<i>Было: {escape(lesson['planned_subject'])}</i>")
    return "\n".join(lines)


def _require_group(message: Message, conn) -> str | None:
    """Вернуть группу пользователя; None — если он не зарегистрирован."""
    tg_id = message.from_user.id if message.from_user else 0
    return db.get_user_group(conn, tg_id)


async def send_day(message: Message, conn, group: str, d: date) -> None:
    """Отправить день расписания с навигацией по неделе."""
    lessons = get_lessons_for_day(conn, group, d)
    lessons = apply_substitutions(conn, lessons, group, d)
    await message.answer(
        render_day(group, d, lessons),
        parse_mode="HTML",
        reply_markup=inline_kb.week_nav_kb(d, 0),
    )


@router.message(F.text == BTN_TODAY)
async def btn_today(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📅 Сегодня»."""
    from bot.state import SCREEN_SCHEDULE_TODAY, set_last_screen

    group = _require_group(message, conn)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    await send_day(message, conn, group, date.today())


@router.message(F.text == BTN_SCHEDULE)
async def btn_schedule(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📆 Расписание» — сегодня + навигация по дням."""
    from bot.state import SCREEN_SCHEDULE_DAY, set_last_screen

    group = _require_group(message, conn)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)
    await send_day(message, conn, group, date.today())


@router.callback_query(F.data == inline_kb.CB_TODAY)
async def cb_today(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Callback «🔄 Сегодня»."""
    from bot.state import SCREEN_SCHEDULE_TODAY, set_last_screen

    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    if callback.message is not None:
        await send_day(callback.message, conn, group, date.today())
    await callback.answer()


@router.callback_query(F.data.startswith(inline_kb.CB_NAV_PREFIX))
async def cb_nav(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Навигация по дням: ``sched:nav:{+1|-1|1..6}``."""
    from bot.state import SCREEN_SCHEDULE_DAY, set_last_screen

    payload = (callback.data or "").removeprefix(inline_kb.CB_NAV_PREFIX)
    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)

    if payload.startswith(("+", "-")):
        try:
            shift = int(payload)
        except ValueError:
            await callback.answer()
            return
        target = date.today() + timedelta(days=shift)
    else:
        try:
            weekday = int(payload)
        except ValueError:
            await callback.answer()
            return
        if not 1 <= weekday <= 6:
            await callback.answer()
            return
        today = date.today()
        target = today + timedelta(days=weekday - today.isoweekday())

    if callback.message is not None:
        await send_day(callback.message, conn, group, target)
    await callback.answer()


@router.callback_query(F.data == inline_kb.CB_PICK_DAY)
async def cb_pick_day(callback: CallbackQuery, conn) -> None:
    """Показать кнопки выбора дня недели."""
    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return
    if callback.message is not None:
        await callback.message.answer(
            "📆 <b>Выбери день недели:</b>",
            parse_mode="HTML",
            reply_markup=inline_kb.pick_day_kb(date.today()),
        )
    await callback.answer()
