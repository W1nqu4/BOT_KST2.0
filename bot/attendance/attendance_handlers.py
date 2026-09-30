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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.attendance import attendance_db as att
from bot.attendance import attendance_keyboards as att_kb
from bot.attendance import attendance_service as att_svc
from bot.attendance import attendance_texts as atext
from bot.attendance import attestation_service as atts
from bot.attendance import db as att_db
from bot.attendance import keyboards as kb
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


@router.callback_query(F.data.startswith(att_kb.CB_CHECK_PREFIX))
async def cb_check(callback: CallbackQuery, conn, bot) -> None:
    """``att:check:{date}:{para}:{yes|no}`` — ответ на опрос «Да/Нет».

    «Нет» — полноценный ответ: он сразу пишет ``absent`` (не ответившие
    получат тот же статус при закрытии опроса). Группа берётся у отвечающего,
    а не из callback: подделать пару можно, группу — нет.
    """
    payload = (callback.data or "").removeprefix(att_kb.CB_CHECK_PREFIX)
    parts = payload.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    date_iso, raw_para, answer = parts
    if answer not in ("yes", "no"):
        await callback.answer()
        return
    try:
        para = int(raw_para)
        date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    tg_id = _cb_tg_id(callback)
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.ALERT_NOT_IN_GROUP, show_alert=True)
        return

    group = str(student["group_name"])
    poll = att.get_poll(conn, group, date_iso, para)
    if poll is None or poll.get("is_closed"):
        await callback.answer(atext.ALERT_CHECK_CLOSED, show_alert=True)
        return
    if att.attendance_exists(conn, group, date_iso, para, tg_id):
        await callback.answer(atext.ALERT_CHECK_ALREADY, show_alert=True)
        return

    status = "present" if answer == "yes" else "absent"
    result = att_svc.apply_mark(
        conn, group, date_iso, para, tg_id, str(student["full_name"]),
        status=status, marked_by=tg_id, method="self",
    )
    if not result["ok"]:
        await callback.answer(atext.ALERT_CHECK_ALREADY, show_alert=True)
        return

    if poll.get("mode") == "chat":
        # В режиме лички править нечего: у каждого своё сообщение.
        await att_svc.update_poll_message(conn, bot, poll)

    await callback.answer(
        atext.ALERT_CHECK_YES if answer == "yes" else atext.ALERT_CHECK_NO
    )


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
    """``/my_attendance`` — сводка посещаемости за текущий месяц.

    Команда остаётся алиасом: тот же рендер, что у кнопки «📊 Моя
    посещаемость» в «Профиле».
    """
    await send_my_attendance(message, conn, _tg_id(message), offset=0)


async def send_my_attendance(message: Message, conn, tg_id: int,
                             offset: int = 0, edit: bool = False) -> None:
    """Показать экран «Моя посещаемость» за месяц.

    Args:
        message: сообщение (или ``callback.message``), куда отвечать.
        conn: соединение SQLite.
        tg_id: студент.
        offset: 0 — текущий месяц, -1 — прошлый.
        edit: True — переписать текущее сообщение, а не отправлять новое
            (для кнопок «Обновить» и «Прошлый месяц»).
    """
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext.NEED_GROUP, parse_mode="HTML")
        return

    group = str(student["group_name"])
    period_start, period_end = atts.period_for_month(offset_months=offset)

    summary = att_svc.month_summary_for_period(conn, tg_id, group,
                                               period_start, period_end)
    attestation = atts.get_attestation_summary(conn, tg_id, group,
                                              period_start, period_end)

    if not any(summary["counts"].values()) and not attestation["items"]:
        await message.answer(atext.NO_ATTENDANCE_YET, parse_mode="HTML")
        return

    text = atext.render_my_attendance(
        summary["month_title"], summary["counts"], summary["by_subject"],
        attestation=attestation, group=group,
    )
    keyboard = kb.my_attendance_period_kb(offset)

    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML",
                                    reply_markup=keyboard)
            return
        except TelegramBadRequest:
            # Сообщение слишком старое или уже изменилось — шлём новое.
            logger.debug("could not edit attendance screen", exc_info=True)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith(kb.CB_ATT_PERIOD_PREFIX))
async def cb_attendance_period(callback: CallbackQuery, conn) -> None:
    """``att:per:{offset}`` — «Обновить» и «Прошлый месяц»."""
    raw = (callback.data or "").removeprefix(kb.CB_ATT_PERIOD_PREFIX)
    try:
        offset = int(raw)
    except ValueError:
        await callback.answer()
        return

    if callback.message is not None:
        await send_my_attendance(callback.message, conn, callback.from_user.id,
                                 offset=offset, edit=True)
    await callback.answer()


@router.callback_query(F.data == att_kb.CB_MY)
async def cb_my_attendance(callback: CallbackQuery, conn) -> None:
    """Кнопка «📊 Моя посещаемость» из старых сообщений (``att:my``).

    Основной вход — ``profile:my_attendance`` из «Профиля» (обработчик в
    :mod:`bot.attendance.handlers`), здесь только совместимость: сообщения,
    отправленные до переезда кнопки, несут callback ``att:my``.
    """
    if callback.message is not None:
        await send_my_attendance(callback.message, conn, callback.from_user.id)
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
    """Отправить отчёт за неделю (общий код для команды и кнопки).

    В отчёт добавлен блок аттестации за текущий месяц: старосте нужно видеть
    не только прогулы за неделю, но и кто рискует не получить аттестацию.
    """
    report = att_svc.group_week_report(conn, group)
    attestation = atts.get_group_attestation_report(
        conn, group, *atts.period_for_month(offset_months=0)
    )

    if (not report["truants"] and not report["good"]
            and not attestation["at_risk"] and not attestation["excellent"]):
        await message.answer(atext.NO_REPORT_DATA, parse_mode="HTML")
        return

    await message.answer(
        atext.render_week_report(group, report["week_title"],
                                 report["truants"], report["good"],
                                 attestation=attestation),
        parse_mode="HTML",
        reply_markup=kb.week_report_kb(),
    )


@router.callback_query(F.data == kb.CB_WEEK_REFRESH)
async def cb_week_refresh(callback: CallbackQuery, conn) -> None:
    """«🔄 Обновить» в отчёте за неделю — пересчитать и переписать."""
    tg_id = _cb_tg_id(callback)
    if not is_group_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    student = att_db.get_student(conn, tg_id)
    if student is None or callback.message is None:
        await callback.answer()
        return

    group = str(student["group_name"])
    report = att_svc.group_week_report(conn, group)
    attestation = atts.get_group_attestation_report(
        conn, group, *atts.period_for_month(offset_months=0)
    )
    text = atext.render_week_report(group, report["week_title"],
                                    report["truants"], report["good"],
                                    attestation=attestation)
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                         reply_markup=kb.week_report_kb())
    except TelegramBadRequest:
        logger.debug("could not edit week report", exc_info=True)
    await callback.answer()


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