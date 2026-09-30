"""Расписание преподавателя: /teacher (поиск по фамилии).

Поток: ``/teacher Иванов`` — поиск сразу; ``/teacher`` без аргумента — FSM
ввод. Найдено несколько — кнопки выбора, один — расписание сразу.

Данные берутся из ``schedule_cache`` (без замен: у преподавателя замены
приходят по группам, а не по нему). ФИО, предметы и кабинеты экранируются
через :func:`html.escape` — сообщения уходят с ``parse_mode='HTML'``.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import db
from bot.keyboards import inline as inline_kb

logger = logging.getLogger(__name__)

router = Router(name="teacher")

# Сколько преподавателей показывать кнопками, если совпадений много.
MAX_CHOICES = 12


class TeacherSearch(StatesGroup):
    """Состояния поиска преподавателя."""

    waiting_query = State()


ASK_QUERY = (
    "Введи фамилию преподавателя (например, <code>Кудрявцева</code>):"
)

ASK_QUERY_AGAIN = (
    "Напиши фамилию текстом — например, <code>Кудрявцева</code>."
)

NO_LESSONS_TEXT = "🎉 У преподавателя нет пар в этом семестре"


def not_found_text(query: str) -> str:
    """Текст «преподаватель не найден» с экранированным запросом."""
    return (
        f"🤔 Не нашёл преподавателя <b>{escape(query)}</b>.\n"
        "Попробуй только фамилию."
    )


def choices_text(query: str, count: int) -> str:
    """Текст выбора, когда подходящих преподавателей несколько."""
    return (
        f"👥 По запросу <b>{escape(query)}</b> нашлось "
        f"преподавателей: <b>{count}</b>.\n\nВыбери нужного:"
    )


def render_teacher_line(lesson: dict) -> str:
    """Строка одной пары: ``N пара · предмет · группа · каб. X``.

    Пустые поля пропускаются (у некоторых пар нет кабинета). Чётность
    добавляется пометкой в скобках: у одного преподавателя одна и та же
    пара в одном дне бывает и в «Чет», и в «нечет» — без пометки строки
    выглядели бы дублями.

    Args:
        lesson: строка из :func:`bot.db.get_lessons_for_teacher`.

    Returns:
        HTML-строка с отступом в два пробела.
    """
    parts = [f"{int(lesson['para_number'])} пара"]

    subject = lesson.get("subject") or ""
    if subject:
        parts.append(escape(subject))
    group = lesson.get("group_name") or ""
    if group:
        parts.append(escape(group))
    room = lesson.get("room") or ""
    if room:
        parts.append(f"каб. {escape(room)}")

    line = "  " + " · ".join(parts)

    week_type = str(lesson.get("week_type") or "").strip()
    if week_type:
        line += f" ({escape(week_type)})"
    return line


def render_teacher_schedule(fio: str, lessons: list[dict]) -> str:
    """Собрать расписание преподавателя: шапка + дни с парами.

    Args:
        fio: полное ФИО (как его вернул :func:`bot.db.find_teachers`).
        lessons: результат :func:`bot.db.get_lessons_for_teacher`.

    Returns:
        HTML-текст сообщения. Если пар нет — отдельная дружелюбная строка.
    """
    header = f"👤 <b>{escape(fio)}</b>"
    if not lessons:
        return f"{header}\n\n{NO_LESSONS_TEXT}"

    blocks = [header]
    days = sorted({int(lesson["day_of_week"]) for lesson in lessons})
    for day_of_week in days:
        day_lessons = [lesson for lesson in lessons
                       if int(lesson["day_of_week"]) == day_of_week]
        day_lessons.sort(key=lambda item: int(item["para_number"]))

        lines = [f"📅 <b>{escape(inline_kb.day_name(day_of_week))}</b>"]
        lines.extend(render_teacher_line(lesson) for lesson in day_lessons)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


async def _send_schedule(message: Message, conn, fio: str) -> None:
    """Показать расписание преподавателя с кнопками возврата."""
    lessons = db.get_lessons_for_teacher(conn, fio)
    await message.answer(
        render_teacher_schedule(fio, lessons),
        parse_mode="HTML",
        reply_markup=inline_kb.teacher_detail_kb(),
    )


async def _search(message: Message, state: FSMContext, conn,
                  query: str) -> None:
    """Обработать запрос: 0 — не нашёл, 1 — расписание, много — кнопки."""
    found = db.find_teachers(conn, query)

    if not found:
        await message.answer(not_found_text(query), parse_mode="HTML")
        return

    if len(found) == 1:
        await state.clear()
        await _send_schedule(message, conn, found[0])
        return

    # Список кладём в FSM: в callback уходит индекс, а не ФИО (короткая
    # строка надёжнее — фамилия с инициалами в 64 байта может не влезть).
    await state.update_data(teacher_choices=found[:MAX_CHOICES])
    await message.answer(
        choices_text(query, len(found)),
        parse_mode="HTML",
        reply_markup=inline_kb.teacher_pick_kb(found[:MAX_CHOICES]),
    )


async def _ask_query(message: Message, state: FSMContext) -> None:
    """Перевести пользователя в режим ввода фамилии."""
    await state.set_state(TeacherSearch.waiting_query)
    await message.answer(ASK_QUERY, parse_mode="HTML")


@router.message(Command("teacher"))
async def cmd_teacher(message: Message, state: FSMContext, conn,
                      command: CommandObject) -> None:
    """``/teacher`` — расписание любого преподавателя.

    Без аргумента переходим в FSM и просим фамилию; с аргументом
    (``/teacher Иванов``) ищем сразу.
    """
    raw = (command.args or "").strip()
    if not raw:
        await _ask_query(message, state)
        return
    await _search(message, state, conn, raw)


@router.message(TeacherSearch.waiting_query)
async def process_query(message: Message, state: FSMContext, conn) -> None:
    """Ввод фамилии после ``/teacher`` без аргумента."""
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        # Команда вместо фамилии: не глотаем её, а просим ввести текст.
        await message.answer(ASK_QUERY_AGAIN, parse_mode="HTML")
        return
    await _search(message, state, conn, raw)


@router.callback_query(F.data == inline_kb.CB_TEACHER_AGAIN)
async def cb_teacher_again(callback: CallbackQuery, state: FSMContext) -> None:
    """«📆 Другой преподаватель» — просим фамилию заново."""
    await state.set_state(TeacherSearch.waiting_query)
    if callback.message is not None:
        await callback.message.answer(ASK_QUERY, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith(inline_kb.CB_TEACHER_PICK_PREFIX))
async def cb_teacher_pick(callback: CallbackQuery, state: FSMContext,
                          conn) -> None:
    """``teacher:pick:{индекс}`` — расписание выбранного преподавателя.

    Индекс сверяется со свежим списком из FSM: кнопка могла устареть
    (расписание обновилось, список стал короче) — тогда честно просим
    ввести фамилию заново.
    """
    raw = (callback.data or "").removeprefix(inline_kb.CB_TEACHER_PICK_PREFIX)
    try:
        index = int(raw)
    except ValueError:
        await callback.answer()
        return

    choices = (await state.get_data()).get("teacher_choices") or []
    if not 0 <= index < len(choices):
        await callback.answer("Список устарел, введи фамилию заново",
                              show_alert=True)
        return

    fio = str(choices[index])
    await state.clear()
    if callback.message is not None:
        await _send_schedule(callback.message, conn, fio)
    await callback.answer()