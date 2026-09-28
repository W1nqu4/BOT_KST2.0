"""Дедлайны: CRUD через Telegram, FSM, inline-календарь (шаг 8).

Поток добавления: «📝 Дедлайны» → список → «➕ Добавить» → способ привязки
(предмет / преподаватель / произвольный) → текст задачи → дата (календарь,
вручную или без даты) → карточка.

Все callback-данные начинаются с ``dl:``. Данные из БД экранируются
``html.escape``: пользователь может ввести что угодно, включая теги.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import db
from bot.config import TIMEZONE
from bot.keyboards import inline as ikb
from bot.services import cache_service, deadline_service as dl

logger = logging.getLogger(__name__)

router = Router(name="deadlines")

# Сколько предметов/преподавателей показывать в списке выбора.
MAX_CHOICES = 12


class DeadlineAdd(StatesGroup):
    """Состояния добавления дедлайна."""

    waiting_custom_subject = State()
    waiting_task = State()
    waiting_date_manual = State()


def render_deadlines(items: list[dict], today: date | None = None) -> str:
    """Собрать список дедлайнов с группировкой по срочности.

    Args:
        items: активные дедлайны из :func:`deadline_service.list_active`.
        today: база отсчёта (для тестов).

    Returns:
        HTML-текст сообщения.
    """
    if not items:
        return (
            "📝 <b>Дедлайны</b>\n\n"
            "Пока пусто. Добавь первый — кнопкой ниже 👇"
        )

    lines = ["📝 <b>Дедлайны</b>", ""]
    for emoji, title, chunk in dl.group_by_urgency(items, today):
        lines.append(f"{emoji} <b>{escape(title)}</b>")
        for item in chunk:
            lines.append(render_deadline_line(item, today))
        lines.append("")
    return "\n".join(lines).rstrip()


def render_deadline_line(item: dict, today: date | None = None) -> str:
    """Строка одного дедлайна.

    Формат: ``<b>{task}</b> — {subject} ({teacher}), {dd.mm}`` плюс пометка
    срочности (``N дн. назад`` / ``сегодня`` / ``завтра``).

    Args:
        item: словарь дедлайна (может содержать ``days_left``).
        today: база отсчёта (для тестов).

    Returns:
        HTML-строка без ведущего перевода строки.
    """
    task = escape(item.get("task") or "без названия")
    subject = escape(item.get("subject") or "")
    teacher = escape(item.get("teacher") or "")
    date_iso = item.get("deadline_date")

    parts = [f"<b>{task}</b>"]
    detail = subject
    if teacher:
        detail = f"{detail} ({teacher})" if detail else f"({teacher})"
    if date_iso:
        try:
            pretty = datetime.fromisoformat(date_iso).strftime("%d.%m")
        except ValueError:
            pretty = date_iso
        detail = f"{detail}, {pretty}" if detail else pretty
    if detail:
        parts.append(f"— {detail}")

    remaining = item.get("days_left")
    if remaining is None:
        remaining = dl.days_left(date_iso, today)

    if remaining is None:
        parts.append("<i>без даты</i>")
    elif remaining < 0:
        days_ago = abs(remaining)
        parts.append(f"<i>{days_ago} дн. назад</i>")
    elif remaining == 0:
        parts.append("<i>сегодня</i>")
    elif remaining == 1:
        parts.append("<i>завтра</i>")

    return "  " + " ".join(parts)


def _today() -> date:
    """Сегодняшняя дата в поясе техникума."""
    return datetime.now(TIMEZONE).date()


def _tg_id(event) -> int:
    """Telegram id автора события (сообщения или callback)."""
    user = getattr(event, "from_user", None)
    return user.id if user is not None else 0


async def send_deadlines(target, conn, tg_id: int) -> None:
    """Отправить список дедлайнов с клавиатурой.

    Args:
        target: Message или CallbackQuery.message.
        conn: соединение SQLite.
        tg_id: владелец.
    """
    items = dl.list_active(conn, tg_id)
    await target.answer(
        render_deadlines(items, _today()),
        parse_mode="HTML",
        reply_markup=ikb.deadline_list_kb(items),
    )


@router.message(F.text == "📝 Дедлайны")
async def btn_deadlines(message: Message, conn, state: FSMContext) -> None:
    """Кнопка «📝 Дедлайны» из главного меню."""
    from bot.state import SCREEN_DEADLINES_LIST, set_last_screen

    await set_last_screen(state, SCREEN_DEADLINES_LIST)
    await send_deadlines(message, conn, _tg_id(message))


@router.callback_query(F.data == "dl:list")
async def cb_list(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Показать список дедлайнов (и выйти из любого шага добавления)."""
    from bot.state import SCREEN_DEADLINES_LIST, set_last_screen

    await state.clear()
    await set_last_screen(state, SCREEN_DEADLINES_LIST)
    if callback.message is not None:
        await send_deadlines(callback.message, conn, _tg_id(callback))
    await callback.answer()


@router.callback_query(F.data == "dl:add")
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Спросить, как привязать дедлайн."""
    from bot.state import SCREEN_DEADLINES_ADD, set_last_screen

    await state.clear()
    await set_last_screen(state, SCREEN_DEADLINES_ADD)
    if callback.message is not None:
        await callback.message.answer(
            "➕ <b>Новый дедлайн</b>\n\nКак привязать?",
            parse_mode="HTML",
            reply_markup=ikb.deadline_type_kb(),
        )
    await callback.answer()


def _group_of(conn, tg_id: int) -> str | None:
    """Группа пользователя (нужна для списка предметов и преподавателей)."""
    return db.get_user_group(conn, tg_id)


def group_subjects(conn, group: str) -> list[str]:
    """Уникальные предметы группы из кэша расписания (отсортированы)."""
    rows = conn.execute(
        "SELECT DISTINCT subject FROM schedule_cache"
        " WHERE group_name = ? AND subject <> ''"
        " ORDER BY subject",
        (group,),
    ).fetchall()
    return [str(row["subject"]) for row in rows]


def group_teachers(conn, group: str) -> list[str]:
    """Уникальные преподаватели группы (первый из списка у подгрупп)."""
    rows = conn.execute(
        "SELECT DISTINCT teacher FROM schedule_cache"
        " WHERE group_name = ? AND teacher <> ''",
        (group,),
    ).fetchall()
    result: set[str] = set()
    for row in rows:
        for part in str(row["teacher"]).split(","):
            part = part.strip()
            if part:
                result.add(part)
    return sorted(result)


@router.callback_query(F.data == "dl:type:subject")
async def cb_type_subject(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Список предметов группы."""
    tg_id = _tg_id(callback)
    group = _group_of(conn, tg_id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    subjects = group_subjects(conn, group)[:MAX_CHOICES]
    if not subjects:
        await callback.answer("Расписание ещё не загружено", show_alert=True)
        return

    await state.update_data(subjects=subjects)
    items = [(index, name) for index, name in enumerate(subjects)]
    if callback.message is not None:
        await callback.message.answer(
            "📚 <b>Выбери предмет:</b>",
            parse_mode="HTML",
            reply_markup=ikb.pick_from_list_kb(items, "dl:subj"),
        )
    await callback.answer()


@router.callback_query(F.data == "dl:type:teacher")
async def cb_type_teacher(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Список преподавателей группы."""
    tg_id = _tg_id(callback)
    group = _group_of(conn, tg_id)
    if group is None:
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    teachers = group_teachers(conn, group)[:MAX_CHOICES]
    if not teachers:
        await callback.answer("Расписание ещё не загружено", show_alert=True)
        return

    await state.update_data(teachers=teachers)
    items = [(index, name) for index, name in enumerate(teachers)]
    if callback.message is not None:
        await callback.message.answer(
            "🧑‍🏫 <b>Выбери преподавателя:</b>",
            parse_mode="HTML",
            reply_markup=ikb.pick_from_list_kb(items, "dl:teach"),
        )
    await callback.answer()


@router.callback_query(F.data == "dl:type:custom")
async def cb_type_custom(callback: CallbackQuery, state: FSMContext) -> None:
    """Произвольный заголовок — спрашиваем текст."""
    await state.set_state(DeadlineAdd.waiting_custom_subject)
    if callback.message is not None:
        await callback.message.answer(
            "✏️ Напиши название (например, <code>Курсовая по МДК</code>):",
            parse_mode="HTML",
        )
    await callback.answer()
@router.callback_query(F.data.startswith("dl:subj:"))
async def cb_pick_subject(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал предмет из списка."""
    index = int((callback.data or "").removeprefix("dl:subj:"))
    data = await state.get_data()
    subjects = data.get("subjects") or []
    if index >= len(subjects):
        await callback.answer("Список устарел, выбери заново", show_alert=True)
        return
    await state.update_data(subject=subjects[index], teacher="")
    await _ask_task(callback, state)


@router.callback_query(F.data.startswith("dl:teach:"))
async def cb_pick_teacher(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал преподавателя из списка."""
    index = int((callback.data or "").removeprefix("dl:teach:"))
    data = await state.get_data()
    teachers = data.get("teachers") or []
    if index >= len(teachers):
        await callback.answer("Список устарел, выбери заново", show_alert=True)
        return
    await state.update_data(subject="", teacher=teachers[index])
    await _ask_task(callback, state)


async def _ask_task(callback: CallbackQuery, state: FSMContext) -> None:
    """Спросить текст задачи (после выбора привязки)."""
    await state.set_state(DeadlineAdd.waiting_task)
    if callback.message is not None:
        await callback.message.answer(
            "📌 Что нужно сделать?", parse_mode="HTML",
        )
    await callback.answer()


@router.message(DeadlineAdd.waiting_custom_subject)
async def process_custom_subject(message: Message, state: FSMContext) -> None:
    """Произвольное название — сохраняем и спрашиваем задачу."""
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("Название не может быть пустым. Напиши ещё раз:")
        return
    await state.update_data(subject=subject, teacher="")
    await state.set_state(DeadlineAdd.waiting_task)
    await message.answer("📌 Что нужно сделать?", parse_mode="HTML")


@router.message(DeadlineAdd.waiting_task)
async def process_task(message: Message, state: FSMContext) -> None:
    """Текст задачи — сохраняем и спрашиваем дату."""
    task = (message.text or "").strip()
    if not task:
        await message.answer("Опиши задачу хотя бы коротко:")
        return
    await state.update_data(task=task)
    await message.answer(
        "📅 <b>Как укажешь дату?</b>",
        parse_mode="HTML",
        reply_markup=ikb.date_source_kb(),
    )


@router.callback_query(F.data == "dl:date:calendar")
async def cb_date_calendar(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Показать inline-календарь на текущий месяц."""
    today = _today()
    if callback.message is not None:
        await callback.message.answer(
            "📅 <b>Выбери дату:</b>",
            parse_mode="HTML",
            reply_markup=ikb.build_calendar_kb(today.year, today.month, "dl"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("dl:cal:nav:"))
async def cb_calendar_nav(callback: CallbackQuery, state: FSMContext) -> None:
    """Листать месяцы календаря."""
    payload = (callback.data or "").removeprefix("dl:cal:nav:")
    try:
        year_s, month_s = payload.split("-")
        year, month = int(year_s), int(month_s)
    except ValueError:
        await callback.answer()
        return
    if not 1 <= month <= 12:
        await callback.answer()
        return
    if callback.message is not None:
        await callback.message.edit_reply_markup(
            reply_markup=ikb.build_calendar_kb(year, month, "dl"),
        )
    await callback.answer()


@router.callback_query(F.data == "dl:cal:ignore")
async def cb_calendar_ignore(callback: CallbackQuery) -> None:
    """Нажатие на шапку календаря или день вне месяца — ничего не делаем."""
    await callback.answer()


@router.callback_query(F.data.startswith("dl:cal:pick:"))
async def cb_calendar_pick(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Дата выбрана в календаре → создаём дедлайн."""
    date_iso = (callback.data or "").removeprefix("dl:cal:pick:")
    if callback.message is not None:
        await finalize(callback.message, conn, state, _tg_id(callback), date_iso)
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "dl:cal:manual")
async def cb_calendar_manual(callback: CallbackQuery, state: FSMContext) -> None:
    """Ввод даты текстом."""
    await state.set_state(DeadlineAdd.waiting_date_manual)
    if callback.message is not None:
        await callback.message.answer(
            "✏️ Напиши дату: <code>ДД.ММ.ГГГГ</code> или "
            "<code>ДД.ММ</code> (например, <code>15.10</code>).",
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "dl:date:manual")
async def cb_date_manual(callback: CallbackQuery, state: FSMContext) -> None:
    """То же, что и «✏️ Вручную» из календаря."""
    await cb_calendar_manual(callback, state)


@router.callback_query(F.data == "dl:date:none")
async def cb_date_none(callback: CallbackQuery, conn, state: FSMContext) -> None:
    """Дедлайн без даты."""
    if callback.message is not None:
        await finalize(callback.message, conn, state, _tg_id(callback), None)
    await state.clear()
    await callback.answer()


@router.message(DeadlineAdd.waiting_date_manual)
async def process_date_manual(message: Message, conn, state: FSMContext) -> None:
    """Разобрать дату из текста и создать дедлайн."""
    date_iso = dl.parse_manual_date(message.text or "", _today())
    if date_iso is None:
        await message.answer(
            "❌ Не понял дату. Примеры: <code>15.10.2026</code>, "
            "<code>15.10</code>.",
            parse_mode="HTML",
        )
        return
    await finalize(message, conn, state, _tg_id(message), date_iso)
    await state.clear()
async def finalize(target, conn, state: FSMContext, tg_id: int,
                   date_iso: str | None) -> int | None:
    """Создать дедлайн из накопленных данных FSM и показать карточку.

    Args:
        target: Message, куда отвечаем.
        conn: соединение SQLite.
        state: FSM-контекст с ``subject`` / ``teacher`` / ``task``.
        tg_id: владелец.
        date_iso: срок в ISO или None.

    Returns:
        ``id`` созданного дедлайна либо None, если данных не хватило.
    """
    data = await state.get_data()
    subject = (data.get("subject") or "").strip()
    teacher = (data.get("teacher") or "").strip()
    task = (data.get("task") or "").strip()

    if not task:
        await target.answer(
            "⚠️ Не хватает описания задачи. Начни заново: /start → 📝 Дедлайны.",
            parse_mode="HTML",
        )
        return None

    if not subject and not teacher:
        subject = "Без предмета"

    deadline_id = dl.add(conn, tg_id, subject, teacher, task, date_iso)
    item = dl.get(conn, deadline_id, tg_id) or {}
    item["days_left"] = dl.days_left(date_iso, _today())

    await target.answer(
        "✅ <b>Дедлайн добавлен</b>\n\n" + render_deadline_line(item, _today()),
        parse_mode="HTML",
        reply_markup=ikb.deadline_confirm_kb(deadline_id),
    )
    return deadline_id


@router.callback_query(F.data.startswith("dl:del_confirmed:"))
async def cb_delete_confirmed(callback: CallbackQuery, conn) -> None:
    """Подтверждённое удаление: мягко удаляем и показываем список."""
    try:
        deadline_id = int((callback.data or "").removeprefix("dl:del_confirmed:"))
    except ValueError:
        await callback.answer()
        return

    removed = dl.soft_delete(conn, deadline_id, _tg_id(callback))
    if callback.message is not None:
        await send_deadlines(callback.message, conn, _tg_id(callback))
    await callback.answer("Удалено" if removed else "Уже удалено")


@router.callback_query(F.data.startswith("dl:del:"))
async def cb_delete_ask(callback: CallbackQuery, conn) -> None:
    """Спросить подтверждение удаления (показываем, что именно удаляем)."""
    try:
        deadline_id = int((callback.data or "").removeprefix("dl:del:"))
    except ValueError:
        await callback.answer()
        return

    item = dl.get(conn, deadline_id, _tg_id(callback))
    if item is None:
        await callback.answer("Дедлайн не найден", show_alert=True)
        return

    if callback.message is not None:
        await callback.message.answer(
            f"Удалить дедлайн <b>{escape(item['task'])}</b>?",
            parse_mode="HTML",
            reply_markup=ikb.deadline_delete_confirm_kb(deadline_id),
        )
    await callback.answer()
