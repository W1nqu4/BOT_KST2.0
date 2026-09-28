"""Расписание: «Сегодня», «Расписание» с навигацией по дням, рендер карточек.

Все данные из БД экранируются через :func:`html.escape` (названия предметов,
ФИО, кабинеты), потому что сообщения отправляются с ``parse_mode='HTML'``.
"""
from __future__ import annotations

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
    get_nearest_lessons_for_subject,
    get_subjects_for_group,
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


async def send_day(message: Message, conn, group: str, d: date,
                   state: FSMContext | None = None) -> None:
    """Отправить день расписания с навигацией.

    Показанная дата запоминается в FSM: ◀️/▶️ листают дни от неё, а не от
    «сегодня», иначе ▶️ застревает на завтрашнем дне, а ◀️ уходит в воскресенье.
    """
    from bot.state import set_screen_date

    lessons = get_lessons_for_day(conn, group, d)
    lessons = apply_substitutions(conn, lessons, group, d)
    if state is not None:
        await set_screen_date(state, d)
    await message.answer(
        render_day(group, d, lessons),
        parse_mode="HTML",
        reply_markup=inline_kb.week_nav_kb(d, 0),
    )


@router.message(F.text == BTN_TODAY)
async def btn_today(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📅 Сегодня» — алиас «📆 Расписание» (в меню её больше нет).

    Оставлена для совместимости: если у пользователя в клиенте ещё висит
    старая reply-клавиатура, нажатие не должно молчать. Экран помечается
    как ``schedule:today`` — именно оттуда пришёл пользователь.
    """
    from bot.state import SCREEN_SCHEDULE_TODAY, set_last_screen

    group = _require_group(message, conn)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    await send_day(message, conn, group, date.today(), state)


@router.message(F.text == BTN_SCHEDULE)
async def btn_schedule(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📆 Расписание» — сегодня + навигация по дням."""
    from bot.state import SCREEN_SCHEDULE_DAY, set_last_screen

    group = _require_group(message, conn)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)
    await send_day(message, conn, group, date.today(), state)


@router.callback_query(F.data == inline_kb.CB_TODAY)
async def cb_today(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Устаревший callback «🔄 Сегодня» — показываем сегодняшний день."""
    from bot.state import SCREEN_SCHEDULE_TODAY, set_last_screen

    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    if callback.message is not None:
        await send_day(callback.message, conn, group, date.today(), state)
    await callback.answer()


@router.callback_query(F.data == inline_kb.CB_MENU)
async def cb_menu(callback: CallbackQuery) -> None:
    """«🏠 Меню» — вернуть главную reply-клавиатуру."""
    from bot.keyboards import reply as reply_kb

    if callback.message is not None:
        await callback.message.answer(
            "🏠 <b>Главное меню</b> — используй кнопки ниже.",
            parse_mode="HTML",
            reply_markup=reply_kb.main_kb(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith(inline_kb.CB_NAV_PREFIX))
async def cb_nav(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Навигация по дням: ``sched:nav:{+1|-1|1..6}``.

    Смещение ``+1``/``-1`` считается от **даты, показанной на экране**
    (FSM ``screen_date``), а не от ``date.today()``: иначе ▶️ навсегда
    остаётся на «завтра», а ◀️ уводит за пределы учебной недели.
    """
    from bot.state import (
        SCREEN_SCHEDULE_DAY,
        get_screen_date,
        set_last_screen,
    )

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
        base = await get_screen_date(state) or date.today()
        target = base + timedelta(days=shift)
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
        await send_day(callback.message, conn, group, target, state)
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


# --- «📚 Предметы»: список предметов и ближайшие пары по предмету ---

SUBJECTS_EMPTY = (
    "📚 <b>Предметы</b>\n\n"
    "Расписание ещё не загружено — список появится после первого обновления."
)

SUBJECT_NONE = "🎉 По этому предмету пар не найдено"


def render_subject_detail(subject: str, lessons: list[dict]) -> str:
    """Текст экрана предмета: заголовок + ближайшие пары.

    Args:
        subject: название предмета.
        lessons: результат :func:`get_nearest_lessons_for_subject`
            (с дополнительным полем ``date``).

    Returns:
        HTML-текст сообщения. Если пар нет — дружелюбная строчка.
    """
    header = f"📚 <b>{escape(subject)}</b>"
    if not lessons:
        return f"{header}\n\n{SUBJECT_NONE}"

    blocks = [header]
    for lesson in lessons:
        d = lesson["date"]
        para = lesson["para_number"]
        date_label = f"{d.strftime('%d.%m')} ({inline_kb.day_short(d.isoweekday())})"
        first = f"📅 {date_label} · {para} пара"
        time_range = lesson.get("time_range") or ""
        if time_range:
            first += f" · {time_range}"
        lines = [first]
        teacher = lesson.get("teacher") or ""
        if teacher:
            lines.append(f"👤 {escape(teacher)}")
        room = lesson.get("room") or ""
        if room:
            lines.append(f"🚪 {escape(room)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
@router.callback_query(F.data == inline_kb.CB_SUBJECTS)
async def cb_subjects(callback: CallbackQuery, conn) -> None:
    """«📚 Предметы» — список предметов группы студента."""
    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    subjects = get_subjects_for_group(conn, group)
    if callback.message is not None:
        if subjects:
            await callback.message.answer(
                "📚 <b>Выбери предмет:</b>",
                parse_mode="HTML",
                reply_markup=inline_kb.subjects_kb(subjects),
            )
        else:
            await callback.message.answer(
                SUBJECTS_EMPTY,
                parse_mode="HTML",
                reply_markup=inline_kb.empty_subjects_kb(),
            )
    await callback.answer()


@router.callback_query(F.data.startswith(inline_kb.CB_SUBJECT_PREFIX))
async def cb_subject_show(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """``subj:show:{idx}`` — ближайшие пары выбранного предмета.

    Индекс сверяется со свежим списком предметов группы: кнопка могла
    устареть (расписание обновилось) — тогда честно просим выбрать заново.
    """
    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    raw = (callback.data or "").removeprefix(inline_kb.CB_SUBJECT_PREFIX)
    try:
        index = int(raw)
    except ValueError:
        await callback.answer()
        return

    subjects = get_subjects_for_group(conn, group)
    if not subjects:
        await callback.answer("Расписание ещё не загружено", show_alert=True)
        return
    if not 0 <= index < len(subjects):
        await callback.answer("Список устарел, открой предметы заново",
                              show_alert=True)
        return

    subject = subjects[index]
    lessons = get_nearest_lessons_for_subject(conn, group, subject)
    if callback.message is not None:
        await callback.message.answer(
            render_subject_detail(subject, lessons),
            parse_mode="HTML",
            reply_markup=inline_kb.subject_detail_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == inline_kb.CB_SUBJECT_BACK)
async def cb_subject_back(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """«🔙 Назад» из списка предметов — вернуться к расписанию дня."""
    from bot.state import (
        SCREEN_SCHEDULE_DAY,
        get_screen_date,
        set_last_screen,
    )

    group = db.get_user_group(conn, callback.from_user.id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)
    target = await get_screen_date(state) or date.today()
    if callback.message is not None:
        await send_day(callback.message, conn, group, target, state)
    await callback.answer()
