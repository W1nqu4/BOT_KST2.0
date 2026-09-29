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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.attendance import keyboards as att_kb
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

# Callback-данные выбора группы из экрана расписания.
CB_SET_GROUP = "sched:setgroup"

# Запрос номера группы (ответ на «🔢 Указать группу»).
ASK_GROUP_TEXT = "Введи номер группы (например, <code>25КАД</code>):"

# Экран «группа не указана». Показывается вместо прежнего молчания: без
# ``users.group_name`` расписание не построить, поэтому предлагаем указать
# группу вручную или вступить в группу посещаемости по коду старосты —
# вступление подтягивает группу для расписания автоматически.
NO_GROUP_TEXT = (
    "📆 <b>Расписание</b>\n\n"
    "Чтобы показывать расписание, укажи свою группу.\n\n"
    "Если у тебя есть код от старосты — вступи в группу через "
    "«📊 Моя группа», расписание подключится автоматически."
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


def no_group_kb() -> InlineKeyboardMarkup:
    """Клавиатура экрана «группа не указана».

    Layout:
        [🔢 Указать группу]
        [📊 Ввести код от старосты]
        [🏠 Меню]

    «📊 Ввести код от старосты» ведёт в уже существующую регистрацию
    посещаемости (``grp:enter_code``): вступление в группу подтягивает
    ``users.group_name``, и расписание начинает работать само.

    Returns:
        InlineKeyboardMarkup с тремя кнопками.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Указать группу",
                              callback_data=CB_SET_GROUP)],
        [InlineKeyboardButton(text="📊 Ввести код от старосты",
                              callback_data=att_kb.CB_ENTER_CODE)],
        [InlineKeyboardButton(text="🏠 Меню",
                              callback_data=inline_kb.CB_MENU)],
    ])


async def _require_group_or_prompt(event: Message | CallbackQuery, conn,
                                   tg_id: int) -> str | None:
    """Группа пользователя или экран «укажи группу».

    Заменяет прежнее молчание: раньше без ``users.group_name`` обработчики
    расписания просто возвращались, и кнопка «📆 Расписание» выглядела
    сломанной. Теперь вместо этого приходит экран с двумя способами указать
    группу.

    Args:
        event: сообщение или callback — куда отвечать и чем подтверждать.
        conn: соединение SQLite.
        tg_id: Telegram id пользователя.

    Returns:
        Имя группы, если она задана, иначе None (экран уже отправлен).
    """
    group = db.get_user_group(conn, tg_id)
    if group:
        return group

    # Подтягиваем группу у тех, кто вступил в группу посещаемости до
    # появления связки: у них есть ``students.group_name``, а ``users`` — нет,
    # хотя обе таблицы описывают одну и ту же учебную группу.
    from bot.attendance import db as att_db

    student = att_db.get_student(conn, tg_id)
    if student is not None:
        student_group = str(student["group_name"] or "")
        if student_group:
            db.update_user_group_only(conn, tg_id, student_group)
            logger.info("schedule group restored from attendance",
                        extra={"group": student_group, "tg_id": tg_id})
            return student_group

    kb = no_group_kb()
    if isinstance(event, CallbackQuery):
        if event.message is not None:
            await event.message.answer(NO_GROUP_TEXT, parse_mode="HTML",
                                       reply_markup=kb)
        await event.answer()
        return None

    await event.answer(NO_GROUP_TEXT, parse_mode="HTML", reply_markup=kb)
    return None


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

    tg_id = message.from_user.id if message.from_user else 0
    group = await _require_group_or_prompt(message, conn, tg_id)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    await send_day(message, conn, group, date.today(), state)


@router.message(F.text == BTN_SCHEDULE)
async def btn_schedule(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📆 Расписание» — сегодня + навигация по дням."""
    from bot.state import SCREEN_SCHEDULE_DAY, set_last_screen

    tg_id = message.from_user.id if message.from_user else 0
    group = await _require_group_or_prompt(message, conn, tg_id)
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)
    await send_day(message, conn, group, date.today(), state)


@router.callback_query(F.data == CB_SET_GROUP)
async def cb_set_group(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔢 Указать группу» — запустить ввод группы для расписания.

    Состояние переиспользуется из регистрации расписания
    (:class:`bot.handlers.start.GroupForm`), поэтому ввод обрабатывает
    ``process_group`` вместе с проверкой по списку групп из расписания.
    """
    from bot.handlers.start import GroupForm

    await state.set_state(GroupForm.waiting_group)
    await state.update_data(from_schedule=True)
    if callback.message is not None:
        await callback.message.answer(ASK_GROUP_TEXT, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == inline_kb.CB_TODAY)
async def cb_today(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Устаревший callback «🔄 Сегодня» — показываем сегодняшний день."""
    from bot.state import SCREEN_SCHEDULE_TODAY, set_last_screen

    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
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
    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
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
    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
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
    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
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
    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
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

    group = await _require_group_or_prompt(
        callback, conn, callback.from_user.id,
    )
    if group is None:
        return
    await set_last_screen(state, SCREEN_SCHEDULE_DAY)
    target = await get_screen_date(state) or date.today()
    if callback.message is not None:
        await send_day(callback.message, conn, group, target, state)
    await callback.answer()
