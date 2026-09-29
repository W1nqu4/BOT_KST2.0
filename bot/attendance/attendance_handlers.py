"""Обработчики посещаемости: отметки, сводки, /mark, /report_week (этап 2).

Три потока:

- **опрос в чате** — «✅ Я на паре» (публичный список виден всем в чате);
- **личка** — ``/attendance`` (отметиться во время пары) и
  ``/my_attendance`` (сводка за месяц);
- **староста** — ``/mark`` (ручная отметка с переключением статуса) и
  ``/report_week`` (отчёт за неделю).

Правила: ``absent`` автоматически не ставится (только староста вручную),
сообщение-опрос не закрепляется, опрос уходит лишь в привязанный чат.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.attendance import attendance_db as att
from bot.attendance import attendance_keyboards as att_kb
from bot.attendance import attendance_service as att_svc
from bot.attendance import attendance_texts as atext
from bot.attendance import db as att_db
from bot.config import KRASNOYARSK

logger = logging.getLogger(__name__)

router = Router(name="attendance_marks")


def _tg_id(message: Message) -> int:
    """tg_id автора сообщения."""
    return message.from_user.id if message.from_user else 0


def _cb_tg_id(callback: CallbackQuery) -> int:
    """tg_id автора callback-а."""
    return callback.from_user.id


def is_group_admin(conn, tg_id: int) -> bool:
    """Староста или зам? Им доступны /mark и /report_week."""
    student = att_db.get_student(conn, tg_id)
    if student is None:
        return False
    return str(student["role"]) in ("starosta", "deputy")


@router.callback_query(F.data.startswith(att_kb.CB_MARK_PREFIX))
async def mark_present(callback: CallbackQuery, conn, bot) -> None:
    """«✅ Я на паре» — отметка из опроса в чате.

    Публичность: после отметки сообщение-опрос редактируется, и список
    отметившихся видят все в чате.
    """
    payload = (callback.data or "").removeprefix(att_kb.CB_MARK_PREFIX)
    parts = payload.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    date_iso, para_raw = parts
    try:
        para = int(para_raw)
    except ValueError:
        await callback.answer()
        return

    tg_id = _cb_tg_id(callback)
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.ALERT_NOT_REGISTERED, show_alert=True)
        return

    group = str(student["group_name"])
    poll = att.get_poll(conn, group, date_iso, para)
    if poll is None or poll.get("is_closed"):
        await callback.answer(atext.ALERT_POLL_CLOSED, show_alert=True)
        return
    if att.attendance_exists(conn, group, date_iso, para, tg_id):
        await callback.answer(atext.ALERT_ALREADY_MARKED, show_alert=True)
        return

    status = att_svc.status_for_mark(poll)
    result = att_svc.apply_mark(
        conn, group, date_iso, para, tg_id, str(student["full_name"]),
        status=status, marked_by=tg_id,
    )
    if not result["ok"]:
        await callback.answer(atext.ALERT_ALREADY_MARKED, show_alert=True)
        return

    await att_svc.update_poll_message(conn, bot, poll)
    await callback.answer(
        atext.ALERT_MARKED_LATE if status == "late" else atext.ALERT_MARKED
    )
@router.message(Command("attendance"))
async def cmd_attendance(message: Message, conn) -> None:
    """``/attendance`` — отметиться на паре или посмотреть пары дня."""
    tg_id = _tg_id(message)
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext.NEED_GROUP, parse_mode="HTML")
        return

    group = str(student["group_name"])
    now = datetime.now(KRASNOYARSK)
    today = now.date()

    paras = att_svc.list_day_paras(conn, group, today)
    if not paras:
        await message.answer(atext.NO_PARA_IN_SCHEDULE, parse_mode="HTML")
        return

    marked = {
        int(row["para"])
        for row in att.get_attendance_for_day(conn, group, today.isoformat())
        if int(row["tg_id"]) == tg_id
    }
    rows = [{**item, "marked": int(item["para"]) in marked} for item in paras]

    para = att_svc.current_para(now)
    if para is None:
        # Пары нет — показываем расписание дня и объясняем правило.
        await message.answer(atext.NO_PARA_NOW, parse_mode="HTML")
        await message.answer(
            atext.render_day_attendance(today, rows), parse_mode="HTML",
            reply_markup=att_kb.day_kb(paras, today.isoformat()),
        )
        return

    await message.answer(
        f"<b>{para} пара идёт прямо сейчас.</b>\n\n"
        + atext.render_day_attendance(today, rows),
        parse_mode="HTML",
        reply_markup=att_kb.mark_kb(today.isoformat(), para),
    )


@router.callback_query(F.data.startswith(att_kb.CB_OPEN_PREFIX))
async def open_mark(callback: CallbackQuery, conn) -> None:
    """«Отметиться» из личного экрана: показать кнопку отметки на пару."""
    payload = (callback.data or "").removeprefix(att_kb.CB_OPEN_PREFIX)
    parts = payload.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    date_iso, para_raw = parts
    try:
        para = int(para_raw)
    except ValueError:
        await callback.answer()
        return

    student = att_db.get_student(conn, _cb_tg_id(callback))
    if student is None:
        await callback.answer(atext.ALERT_NOT_REGISTERED, show_alert=True)
        return

    group = str(student["group_name"])
    poll = att.get_poll(conn, group, date_iso, para)
    if poll is None or poll.get("is_closed"):
        await callback.answer(atext.ALERT_POLL_CLOSED, show_alert=True)
        return

    if callback.message is not None:
        await callback.message.answer(
            f"<b>{para} пара</b> · {escape(date_iso)}\n\n{atext.POLL_HINT}",
            parse_mode="HTML",
            reply_markup=att_kb.mark_kb(date_iso, para),
        )
    await callback.answer()


@router.message(Command("my_attendance"))
async def cmd_my_attendance(message: Message, conn) -> None:
    """``/my_attendance`` — сводка посещаемости за текущий месяц."""
    tg_id = _tg_id(message)
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext.NEED_GROUP, parse_mode="HTML")
        return

    summary = att_svc.student_month_summary(conn, tg_id)
    if not any(summary["counts"].values()):
        await message.answer(atext.NO_ATTENDANCE_YET, parse_mode="HTML")
        return

    await message.answer(
        atext.render_my_attendance(summary["month_title"], summary["counts"],
                                   summary["by_subject"]),
        parse_mode="HTML",
        reply_markup=att_kb.my_attendance_kb(),
    )


@router.callback_query(F.data == att_kb.CB_MY)
async def cb_my_attendance(callback: CallbackQuery, conn) -> None:
    """Кнопка «📊 Моя посещаемость» из «Моя группа»."""
    if callback.message is not None:
        await cmd_my_attendance(callback.message, conn)
    await callback.answer()


@router.callback_query(F.data == att_kb.CB_TODAY)
async def cb_today_paras(callback: CallbackQuery, conn) -> None:
    """Кнопка «🗓 Пары сегодня» из сводки."""
    if callback.message is not None:
        await cmd_attendance(callback.message, conn)
    await callback.answer()
def parse_date_arg(raw: str, today: date | None = None) -> str | None:
    """Разобрать дату из аргумента ``/mark``.

    Принимает ``YYYY-MM-DD`` и слово «сегодня».

    Returns:
        Дата в ISO или None.
    """
    value = (raw or "").strip().lower()
    base = today or datetime.now(KRASNOYARSK).date()
    if value in ("сегодня", "today"):
        return base.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@router.message(Command("mark"))
async def cmd_mark(message: Message, command: CommandObject, conn) -> None:
    """``/mark дата пара`` — ручная отметка старосты.

    Показывает список студентов с чекбоксами; нажатие переключает статус
    циклом: present → late → absent → excused → present.
    """
    tg_id = _tg_id(message)
    if not is_group_admin(conn, tg_id):
        await message.answer(atext.MANAGE_DENIED, parse_mode="HTML")
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext.NEED_GROUP, parse_mode="HTML")
        return

    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(atext.MARK_NEED_ARGS, parse_mode="HTML")
        return

    date_iso = parse_date_arg(args[0])
    try:
        para = int(args[1])
    except ValueError:
        await message.answer(atext.MARK_NEED_ARGS, parse_mode="HTML")
        return
    if date_iso is None:
        await message.answer(atext.MARK_NEED_ARGS, parse_mode="HTML")
        return

    group = str(student["group_name"])
    day = date.fromisoformat(date_iso)
    lesson = att_svc.get_lesson_for_para(conn, group, day, para)
    if lesson is None:
        await message.answer(atext.MARK_NO_LESSON, parse_mode="HTML")
        return

    students = att_svc.list_mark_students(conn, group, date_iso, para)
    if not students:
        await message.answer(atext.MARK_NO_STUDENTS, parse_mode="HTML")
        return

    await message.answer(
        atext.render_mark_list(group, day, para, str(lesson["subject"]),
                               students),
        parse_mode="HTML",
        reply_markup=att_kb.mark_list_kb(students, date_iso, para),
    )


@router.callback_query(F.data.startswith(att_kb.CB_CYCLE_PREFIX))
async def cycle_mark(callback: CallbackQuery, conn) -> None:
    """Нажатие на студента в /mark: переключить статус по циклу."""
    tg_id = _cb_tg_id(callback)
    if not is_group_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.NEED_GROUP, show_alert=True)
        return

    payload = (callback.data or "").removeprefix(att_kb.CB_CYCLE_PREFIX)
    parts = payload.rsplit(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    date_iso, para_raw, index_raw = parts
    try:
        para = int(para_raw)
        index = int(index_raw)
        day = date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    group = str(student["group_name"])
    students = att_svc.list_mark_students(conn, group, date_iso, para)
    if not 0 <= index < len(students):
        await callback.answer("Список устарел, открой /mark заново",
                              show_alert=True)
        return

    target = students[index]
    new_status = att_svc.cycle_status(target.get("status"))
    att_svc.set_status(conn, group, date_iso, para, int(target["tg_id"]),
                       str(target["full_name"]), new_status, marked_by=tg_id)

    lesson = att_svc.get_lesson_for_para(conn, group, day, para) or {}
    updated = att_svc.list_mark_students(conn, group, date_iso, para)
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                atext.render_mark_list(group, day, para,
                                       str(lesson.get("subject") or ""),
                                       updated),
                parse_mode="HTML",
                reply_markup=att_kb.mark_list_kb(updated, date_iso, para),
            )
        except Exception as exc:
            logger.debug("mark list not edited", extra={"error": repr(exc)})

    await callback.answer(
        f"{atext.STATUS_LABELS.get(new_status, new_status)}: "
        f"{target['full_name']}"
    )
@router.message(Command("report_week"))
async def cmd_report_week(message: Message, conn) -> None:
    """``/report_week`` — отчёт за неделю для старосты."""
    tg_id = _tg_id(message)
    if not is_group_admin(conn, tg_id):
        await message.answer(atext.MANAGE_DENIED, parse_mode="HTML")
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext.NEED_GROUP, parse_mode="HTML")
        return

    await send_week_report(message, conn, str(student["group_name"]))


async def send_week_report(message: Message, conn, group: str) -> None:
    """Отправить отчёт за неделю (общий код для команды и кнопки)."""
    report = att_svc.group_week_report(conn, group)
    if not report["truants"] and not report["good"]:
        await message.answer(atext.NO_REPORT_DATA, parse_mode="HTML")
        return

    await message.answer(
        atext.render_week_report(group, report["week_title"],
                                 report["truants"], report["good"]),
        parse_mode="HTML",
        reply_markup=att_kb.my_attendance_kb(),
    )


@router.callback_query(F.data.startswith(att_kb.CB_REQUEST_PREFIX))
async def request_poll(callback: CallbackQuery, conn, bot) -> None:
    """Староста запускает опрос досрочно (кнопка «📣 Запустить опрос»)."""
    tg_id = _cb_tg_id(callback)
    if not is_group_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    payload = (callback.data or "").removeprefix(att_kb.CB_REQUEST_PREFIX)
    parts = payload.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    date_iso, para_raw = parts
    try:
        para = int(para_raw)
        day = date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.NEED_GROUP, show_alert=True)
        return

    group = str(student["group_name"])
    result = await att_svc.start_poll(conn, bot, group, day, para, force=True)
    if result["ok"]:
        await callback.answer("Опрос отправлен в чат группы")
        return

    errors = {
        "no_chat": "У группы нет привязанного чата (/setup)",
        "no_lesson": atext.MARK_NO_LESSON,
        "already_exists": "Опрос на эту пару уже есть",
        "send_failed": "Не удалось отправить опрос",
    }
    await callback.answer(errors.get(str(result["error"]), "Не получилось"),
                          show_alert=True)