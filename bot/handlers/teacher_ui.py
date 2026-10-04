"""UI преподавателя: своё расписание, группы и посещаемость.

Доступ только у одобренных преподавателей (``db.is_teacher``): до одобрения
никаких прав нет. Все команды дополнительно проверяют, что группа действительно
принадлежит этому преподавателю — чужую смотреть нельзя.

Особенность: команда ``/attendance`` уже занята студенческим сценарием
(:mod:`bot.attendance.attendance_handlers`). Здесь она объявлена с фильтром
:class:`IsTeacher` и роутер подключается РАНЬШЕ студенческого, поэтому:

- преподавателю ``/attendance <группа>`` открывает посещаемость группы;
- студент проходит мимо (фильтр не пропускает) и попадает в свой хендлер —
  прежнее поведение сохраняется.

Правило безопасности: статус отметки можно менять ТОЛЬКО ``absent → present``.
Преподаватель исправляет ошибку старосты, но не может «наказать» студента,
проставив прогул задним числом.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.attendance import attendance_db as att_db
from bot.attendance import attestation_service as att_svc
from bot.attendance import texts as att_texts
from bot.config import KRASNOYARSK
from bot.db import get_teacher_lessons_for_day
from bot.keyboards import inline as inline_kb
from bot.keyboards import reply as reply_kb

logger = logging.getLogger(__name__)

router = Router(name="teacher_ui")

# Сколько дней показывать в «моём расписании».
WEEK_DAYS = 6

# Лимит длины сообщения Telegram (4096) с запасом на разметку.
TEACHER_MESSAGE_LIMIT = 3500

# Callback-данные.
CB_GROUP_PREFIX = "teacher_group:"
CB_FIX_PREFIX = "teacher_fix_absent:"

# Разделитель в callback: teacher_fix_absent:GROUP:DATE:PARA:TG_ID
CB_SEP = ":"

# Что показывать, если команду вызвал не преподаватель.
NOT_A_TEACHER = "Команда только для преподавателей."

# Названия месяцев для заголовка посещаемости.
MONTHS_RU = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
    "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def month_title(target: date) -> str:
    """Заголовок месяца: «Октябрь 2026»."""
    return f"{MONTHS_RU[target.month]} {target.year}"


def _tg_id(message: Message) -> int:
    """Telegram id автора сообщения (0, если его нет)."""
    return message.from_user.id if message.from_user else 0


def require_teacher(conn, tg_id: int) -> dict | None:
    """Одобренный преподаватель или None.

    Отдельная функция, чтобы во всех командах проверка была одинаковой:
    ``pending`` и ``rejected`` доступа не дают.
    """
    teacher = db.get_teacher(conn, tg_id)
    if teacher is None or teacher["status"] != db.TEACHER_APPROVED:
        return None
    return teacher


class IsTeacher(BaseFilter):
    """Пропускает только одобренных преподавателей.

    Нужен для ``/attendance``: команда есть и у студентов, поэтому фильтр
    определяет, чей это вызов, не меняя поведение студентов.
    """

    async def __call__(self, message: Message, conn) -> bool:
        """Проверить роль преподавателя."""
        if message.from_user is None or conn is None:
            return False
        return require_teacher(conn, message.from_user.id) is not None


def lesson_line(lesson: dict) -> str:
    """Строка одной пары: номер, группа, кабинет, предмет и время."""
    parts = [f"{lesson['para_number']} пара", str(lesson["group_name"])]
    room = (lesson.get("room") or "").strip()
    if room:
        parts.append(f"каб. {room}")

    lines = ["📚 " + " · ".join(escape(part) for part in parts)]
    subject = (lesson.get("subject") or "").strip()
    if subject:
        lines.append(escape(subject))
    time_range = (lesson.get("time_range") or "").strip()
    if time_range:
        lines.append(f"⏰ {time_range}")
    return "\n".join(lines)


def render_teacher_day(full_name: str, target: date,
                       lessons: list[dict]) -> str:
    """Расписание преподавателя на день.

    Отличие от студенческого: вместо «моей группы» в карточке стоит группа,
    в которой проходит пара (преподаватель ведёт несколько).

    Args:
        full_name: ФИО преподавателя.
        target: дата.
        lessons: результат ``get_teacher_lessons_for_day``.

    Returns:
        HTML-текст. День без пар — отдельная строка.
    """
    header = (
        f"📅 <b>{escape(inline_kb.day_name(target.isoweekday()))}, "
        f"{target.strftime('%d.%m.%Y')}</b>"
    )
    if not lessons:
        return f"{header}\n\n🎉 Пар нет."

    blocks = [header]
    blocks.extend(lesson_line(lesson) for lesson in lessons)
    return "\n\n".join(blocks)


def render_teacher_week(conn, full_name: str, start: date,
                        days: int = WEEK_DAYS) -> list[str]:
    """Расписание преподавателя на несколько дней, разбитое на сообщения.

    Все дни собираются в одно сообщение — так преподаватель видит неделю
    целиком, а не получает шесть отдельных писем (четыре из которых были бы
    «пар нет»). Длинный текст режется по границе дня: разрывать карточку пары
    посередине нельзя.

    Args:
        conn: соединение SQLite.
        full_name: ФИО преподавателя.
        start: первый день.
        days: сколько дней показывать.

    Returns:
        Список сообщений (минимум одно).
    """
    limit = TEACHER_MESSAGE_LIMIT
    messages: list[str] = []
    current = ""

    day = start
    for _ in range(days):
        lessons = get_teacher_lessons_for_day(conn, full_name, day)
        block = render_teacher_day(full_name, day, lessons)

        if current and len(current) + len(block) + 2 > limit:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
        day = day + timedelta(days=1)

    if current:
        messages.append(current)
    return messages


async def send_teacher_week(message: Message, conn, full_name: str,
                            start: date) -> None:
    """Отправить расписание преподавателя на WEEK_DAYS дней вперёд.

    Чётность недели учитывает ``get_teacher_lessons_for_day``: пары «по чётным»
    не попадают в нечётную неделю, поэтому один и тот же день через неделю
    покажет другой набор занятий.

    Args:
        message: куда отвечать.
        conn: соединение SQLite.
        full_name: ФИО преподавателя.
        start: первый день.
    """
    for chunk in render_teacher_week(conn, full_name, start):
        await message.answer(chunk, parse_mode="HTML")


async def send_teacher_groups(message: Message, conn, full_name: str) -> None:
    """Список групп преподавателя кнопками (нажатие ведёт к посещаемости)."""
    groups = db.get_teacher_groups(conn, full_name)
    if not groups:
        await message.answer(
            "👥 <b>Твои группы</b>\n\n"
            "В расписании пока нет пар с твоим ФИО.",
            parse_mode="HTML",
        )
        return

    rows = [[InlineKeyboardButton(
        text=f"👥 {group}",
        callback_data=f"{CB_GROUP_PREFIX}{group}",
    )] for group in groups]
    await message.answer(
        f"👥 <b>Твои группы ({len(groups)})</b>\n\n"
        "Нажми на группу, чтобы увидеть посещаемость.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(Command("my_lessons"))
async def cmd_my_lessons(message: Message, conn) -> None:
    """Расписание преподавателя на 6 дней (только для него)."""
    teacher = require_teacher(conn, _tg_id(message))
    if teacher is None:
        await message.answer(NOT_A_TEACHER)
        return

    await message.answer(
        f"🎓 <b>Моё расписание</b>\n<i>{escape(teacher['full_name'])}</i>",
        parse_mode="HTML",
        reply_markup=reply_kb.teacher_main_kb(),
    )
    await send_teacher_week(message, conn, str(teacher["full_name"]),
                            date.today())


@router.callback_query(F.data.startswith(CB_FIX_PREFIX))
async def cb_teacher_fix_absent(callback: CallbackQuery, conn) -> None:
    """Исправить прогул: ``absent → present`` (и только так).

    Правило безопасности: преподаватель не может «наказать» студента, поставив
    прогул задним числом, — он лишь исправляет ошибку старосты. Поэтому любое
    другое направление изменения отклоняется.
    """
    if callback.from_user is None:
        return

    teacher = require_teacher(conn, callback.from_user.id)
    if teacher is None:
        await callback.answer("Только для преподавателей", show_alert=True)
        return

    parts = (callback.data or "").split(CB_SEP)
    if len(parts) != 5:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    _, group, date_iso, para_raw, student_raw = parts

    # Чужие группы недоступны: препод правит только свои занятия.
    my_groups = db.get_teacher_groups(conn, str(teacher["full_name"]))
    if group not in my_groups:
        await callback.answer("Не твоя группа", show_alert=True)
        return

    try:
        para = int(para_raw)
        student_tg_id = int(student_raw)
    except ValueError:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    row = att_db.get_attendance(conn, group, date_iso, para, student_tg_id)
    if row is None:
        await callback.answer("Отметки нет", show_alert=True)
        return

    status = str(row["status"])
    if status != "absent":
        await callback.answer(
            "Можно менять только «прогул» на «присутствовал»", show_alert=True,
        )
        return

    att_db.mark_attendance(
        conn, group, date_iso, para, student_tg_id,
        full_name=str(row["full_name"] or ""),
        status="present", marked_by=callback.from_user.id, method="teacher",
    )

    # Обновляем и текст, и клавиатуру: исправленной отметки в кнопках быть
    # не должно, иначе повторное нажатие даст ошибку.
    if callback.message is not None:
        markup = absent_kb(conn, group, date.fromisoformat(date_iso))
        try:
            await callback.message.edit_text(
                (callback.message.text or "")
                + f"\n\n✅ Исправлено: {escape(str(row['full_name'] or ''))} "
                  f"· {para} пара — прогул → присутствовал",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception:  # noqa: BLE001
            logger.debug("could not edit attendance message", exc_info=True)
    await callback.answer("Исправлено")


@router.message(Command("my_groups"))
async def cmd_my_groups(message: Message, conn) -> None:
    """Группы, где преподаватель ведёт занятия (только для него)."""
    teacher = require_teacher(conn, _tg_id(message))
    if teacher is None:
        await message.answer(NOT_A_TEACHER)
        return

    await send_teacher_groups(message, conn, str(teacher["full_name"]))


@router.message(CommandStart(), IsTeacher())
async def cmd_start_teacher(message: Message, state: FSMContext, conn) -> None:
    """``/start`` для преподавателя: приветствие и его меню.

    Перехват живёт здесь, а не в студенческом приветствии
    (:mod:`bot.attendance.handlers`): роутер подключён раньше, поэтому
    преподаватель получает своё меню, а студент проходит мимо по фильтру
    :class:`IsTeacher` и видит обычное приветствие. Так студенческий код
    остаётся нетронутым.
    """
    await state.clear()
    teacher = require_teacher(conn, _tg_id(message))
    if teacher is None:
        return

    name = str(teacher["full_name"])
    await message.answer(
        f"👋 С возвращением, <b>{escape(name)}</b>!\n\n"
        "Меню преподавателя: своё расписание, группы и посещаемость.",
        parse_mode="HTML",
        reply_markup=reply_kb.teacher_main_kb(),
    )


@router.message(F.text == reply_kb.BTN_TEACHER_LESSONS)
async def btn_teacher_lessons(message: Message, conn) -> None:
    """Кнопка «🎓 Моё расписание»."""
    await cmd_my_lessons(message, conn)


@router.message(F.text == reply_kb.BTN_TEACHER_GROUPS)
async def btn_teacher_groups(message: Message, conn) -> None:
    """Кнопка «👥 Мои группы»."""
    await cmd_my_groups(message, conn)


@router.message(F.text == reply_kb.BTN_TEACHER_ATTENDANCE)
async def btn_teacher_attendance(message: Message, conn) -> None:
    """Кнопка «📊 Посещаемость» — список групп для выбора."""
    teacher = require_teacher(conn, _tg_id(message))
    if teacher is None:
        await message.answer(NOT_A_TEACHER)
        return

    await send_teacher_groups(message, conn, str(teacher["full_name"]))


def render_group_attendance(conn, group: str, today: date | None = None) -> str:
    """Текст посещаемости группы за текущий месяц.

    Данные берём из :func:`bot.attendance.attestation_service.
    get_group_attestation_report` — он уже считает «в зоне риска» и «отличников»
    по всем предметам группы, поэтому логика не дублируется.

    Args:
        conn: соединение SQLite.
        group: учебная группа.
        today: базовая дата (для тестов); по умолчанию — сегодня.

    Returns:
        HTML-текст отчёта.
    """
    from bot.attendance import db as att_group_db

    base = today or date.today()
    period_start, period_end = att_svc.period_for_month(base)
    report = att_svc.get_group_attestation_report(
        conn, group, period_start, period_end
    )

    students = att_group_db.get_group_students(conn, group)
    lines = [
        f"📊 <b>Посещаемость {escape(group)}</b>",
        f"<i>{month_title(base)}</i>",
        "",
        f"Всего студентов: {len(students)}",
    ]

    at_risk = report.get("at_risk") or []
    if at_risk:
        lines.extend(["", "⚠️ <b>Прогуливают:</b>"])
        for item in at_risk[:10]:
            subjects = item.get("subjects") or []
            detail = ""
            if subjects:
                first = subjects[0]
                subject = escape(str(first.get("subject", "")))
                fails = first.get("fails")
                total = first.get("total")
                detail = f" ({subject} {fails}/{total})" if total else ""
            lines.append(
                f"  • {escape(str(item['full_name']))} — "
                f"{item.get('at_risk_count', 0)} проблемных предметов{detail}"
            )
        if len(at_risk) > 10:
            lines.append(f"  и ещё {len(at_risk) - 10}")

    excellent = report.get("excellent") or []
    if excellent:
        lines.extend(["", "✅ <b>Отличная посещаемость:</b>"])
        for item in excellent[:10]:
            lines.append(f"  • {escape(str(item['full_name']))} — все предметы ✅")
        if len(excellent) > 10:
            lines.append(f"  и ещё {len(excellent) - 10}")

    if not at_risk and not excellent:
        lines.extend(["", att_texts.NO_MARKS_TEXT if hasattr(
            att_texts, "NO_MARKS_TEXT") else "Отметок за месяц пока нет."])

    return "\n".join(lines)


def absent_kb(conn, group: str, today: date | None = None
              ) -> InlineKeyboardMarkup | None:
    """Кнопки «исправить прогул» для отмеченных absent за сегодня.

    Показываем только прогулы: преподаватель исправляет статус в одном
    направлении (``absent → present``), поэтому других кнопок и не нужно.

    Args:
        conn: соединение SQLite.
        group: группа.
        today: дата (по умолчанию — сегодня).

    Returns:
        Клавиатура или None, если прогулов нет.
    """
    base = today or date.today()
    date_iso = base.isoformat()
    rows: list[list[InlineKeyboardButton]] = []

    for row in att_db.get_attendance_for_day(conn, group, date_iso):
        if str(row["status"]) != "absent":
            continue
        para = int(row["para"])
        tg_id = int(row["tg_id"])
        name = str(row["full_name"] or tg_id)
        rows.append([InlineKeyboardButton(
            text=f"✅ {name[:24]} · {para} пара",
            callback_data=CB_SEP.join([
                "teacher_fix_absent", group, date_iso, str(para), str(tg_id),
            ]),
        )])

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_group_attendance(message: Message, conn, group: str) -> None:
    """Отправить отчёт по посещаемости группы и кнопки исправления прогулов."""
    await message.answer(
        render_group_attendance(conn, group),
        parse_mode="HTML",
        reply_markup=absent_kb(conn, group),
    )


@router.message(Command("attendance"), IsTeacher())
async def cmd_teacher_attendance(message: Message, command: CommandObject,
                                 conn) -> None:
    """``/attendance [группа]`` — посещаемость группы (только преподаватель).

    Фильтр :class:`IsTeacher` пропускает только одобренных преподавателей:
    студент эту команду здесь не перехватывает и попадает в свой хендлер
    (см. докстроку модуля).
    """
    teacher = require_teacher(conn, _tg_id(message))
    if teacher is None:
        # Достаточно осторожный ответ: не раскрываем, что команда есть.
        await message.answer(NOT_A_TEACHER)
        return

    my_groups = db.get_teacher_groups(conn, str(teacher["full_name"]))
    group = (command.args or "").strip().upper()

    if not group:
        await send_teacher_groups(message, conn, str(teacher["full_name"]))
        return

    if group not in my_groups:
        await message.answer(
            f"🤔 Ты не ведёшь группу {escape(group)}.\n"
            f"Твои группы: {escape(', '.join(my_groups)) or '—'}",
            parse_mode="HTML",
        )
        return

    await send_group_attendance(message, conn, group)


@router.callback_query(F.data.startswith(CB_GROUP_PREFIX))
async def cb_teacher_group(callback: CallbackQuery, conn) -> None:
    """Показать посещаемость группы по кнопке из «Моих групп»."""
    if callback.message is None or callback.from_user is None:
        return

    teacher = require_teacher(conn, callback.from_user.id)
    if teacher is None:
        await callback.answer("Только для преподавателей", show_alert=True)
        return

    group = (callback.data or "").split(CB_SEP, 1)[1]
    my_groups = db.get_teacher_groups(conn, str(teacher["full_name"]))
    if group not in my_groups:
        await callback.answer("Не твоя группа", show_alert=True)
        return

    await send_group_attendance(callback.message, conn, group)
    await callback.answer()