"""Команды и кнопки голосования за посещаемость.

Поток:

- староста: ``/vote сегодня 3`` или кнопка «📣 Запустить голосование» →
  выбор пары → голосование уходит в чат группы;
- студент: жмёт фамилию одногруппника (или свою), счётчик растёт, при
  достижении порога студент зачитывается автоматически;
- закрытие: кнопка «🔒 Закрыть досрочно» или автоматически через 45 минут
  от начала пары (фоновая задача в ``attendance_service.tick``).

Голосовать может только студент той же группы: группа берётся у
голосующего, а не из callback, поэтому подделать чужие данные нельзя.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.attendance import attendance_service as att_svc
from bot.attendance import db as att_db
from bot.attendance import vote_keyboards as vote_kb
from bot.attendance import vote_service as vote_svc
from bot.attendance import vote_texts as vtext
from bot.config import KRASNOYARSK

logger = logging.getLogger(__name__)

router = Router(name="attendance_vote")


class VoteFlow(StatesGroup):
    """Состояния запуска голосования (выбор пары старостой)."""

    waiting_para = State()


def _tg_id(message: Message) -> int:
    """tg_id автора сообщения."""
    return message.from_user.id if message.from_user else 0


def is_vote_admin(conn, tg_id: int) -> bool:
    """Староста или зам? Голосование запускают только они.

    Роли проверяются так же, как в ``/mark`` и ``/report_week``: правила
    доступа должны совпадать во всех старостинских командах.
    """
    student = att_db.get_student(conn, tg_id)
    if student is None:
        return False
    return str(student["role"]) in ("starosta", "deputy")


def parse_vote_args(raw: str,
                    today: date | None = None) -> tuple[str, int] | None:
    """Разобрать аргументы ``/vote``: дата и номер пары.

    Понимает «сегодня», ``29.09``, ``29.09.2026`` и ``2026-09-29``. Год без
    указания — текущий; если получившаяся дата оказывается позже сегодняшней
    больше чем на полгода, берём прошлый год (чтобы «29.12» в январе не
    улетало в будущее).

    Args:
        raw: строка аргументов («сегодня 3», «29.09 2»).
        today: базовая дата (для тестов).

    Returns:
        ``(дата в ISO, номер пары)`` или None, если разобрать не удалось.
    """
    base = today or datetime.now(KRASNOYARSK).date()
    parts = (raw or "").split()
    if len(parts) != 2:
        return None

    raw_date, raw_para = parts
    try:
        para = int(raw_para)
    except ValueError:
        return None
    if not 1 <= para <= 7:
        return None

    day = _parse_vote_date(raw_date, base)
    if day is None:
        return None
    return day.isoformat(), para


def _parse_vote_date(raw: str, base: date) -> date | None:
    """Разобрать дату аргумента ``/vote`` (см. :func:`parse_vote_args`).

    Формат ``%d.%m`` без года разбирается вручную (день и месяц), а не через
    ``strptime``: в Python 3.15 такой разбор без года станет ошибкой
    (``DeprecationWarning`` уже сейчас), и поведение зависело бы от
    подставленного года по умолчанию.
    """
    value = (raw or "").strip().lower()
    if value in ("сегодня", "today"):
        return base

    if re.fullmatch(r"\d{1,2}\.\d{1,2}", value):
        day_str, month_str = value.split(".")
        try:
            parsed = date(base.year, int(month_str), int(day_str))
        except ValueError:
            return None
        # «29.12» в январе — это декабрь прошлого года, а не будущего.
        if (parsed - base).days > 183:
            try:
                parsed = parsed.replace(year=base.year - 1)
            except ValueError:
                return None
        return parsed

    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
async def start_vote_for_para(conn, bot, message: Message, group: str,
                              date_iso: str, para: int, tg_id: int) -> None:
    """Запустить голосование по паре: собрать отсутствующих, отправить в чат.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        message: сообщение старосты (куда отвечать об ошибках).
        group: группа.
        date_iso: дата пары.
        para: номер пары.
        tg_id: кто запускает (староста).
    """
    from datetime import date as _date

    day = _date.fromisoformat(date_iso)

    lesson = att_svc.get_lesson_for_para(conn, group, day, para)
    if lesson is None:
        await message.answer(vtext.NO_LESSON, parse_mode="HTML")
        return

    if vote_svc.vote_poll_exists(conn, group, date_iso, para):
        await message.answer(vtext.VOTE_EXISTS, parse_mode="HTML")
        return

    chat = att_svc.group_chat_for(conn, group)
    if chat is None:
        await message.answer(vtext.NO_CHAT, parse_mode="HTML")
        return

    missing = vote_svc.missing_students(conn, group, date_iso, para)
    if not missing:
        await message.answer(vtext.NO_MISSING, parse_mode="HTML")
        return

    subject = str(lesson["subject"])
    students_total = len(att_db.get_group_students(conn, group))
    threshold = vote_svc.vote_threshold(students_total)

    text = vtext.render_vote_poll(
        group=group, day=day, para=para, subject=subject,
        candidates=missing, votes={}, threshold=threshold, attested=set(),
        total=students_total,
    )
    chat_id = int(chat["chat_id"])
    try:
        sent = await bot.send_message(
            chat_id, text, parse_mode="HTML",
            reply_markup=vote_kb.vote_kb(group, date_iso, para, missing),
        )
    except Exception as exc:
        logger.warning("vote message send failed",
                       extra={"chat_id": chat_id, "group": group,
                              "error": repr(exc)})
        await message.answer(
            "⚠️ Не удалось отправить голосование в чат группы.",
            parse_mode="HTML",
        )
        return

    poll_id = vote_svc.create_vote_poll(
        conn, group, date_iso, para, chat_id, started_by=tg_id,
        closes_at=vote_svc.vote_closes_at(day, para).isoformat(
            timespec="seconds"),
        message_id=getattr(sent, "message_id", None),
    )
    logger.info("vote poll started",
                extra={"group": group, "date": date_iso, "para": para,
                       "poll_id": poll_id, "candidates": len(missing)})
    await message.answer(
        f"🗳 Голосование запущено в чате группы.\n"
        f"Кандидатов: <b>{len(missing)}</b>, "
        f"порог зачёта: <b>{threshold}</b>.",
        parse_mode="HTML",
    )
@router.message(Command("vote"))
async def cmd_vote(message: Message, command: CommandObject, conn, bot) -> None:
    """``/vote дата пара`` — запустить голосование (только староста/зам).

    Без аргументов показывает подсказку формата.
    """
    tg_id = _tg_id(message)
    if not is_vote_admin(conn, tg_id):
        await message.answer(vtext.VOTE_DENIED, parse_mode="HTML")
        return

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(vtext.VOTE_FORMAT, parse_mode="HTML")
        return

    parsed = parse_vote_args(raw)
    if parsed is None:
        await message.answer(vtext.VOTE_FORMAT, parse_mode="HTML")
        return

    date_iso, para = parsed
    student = att_db.get_student(conn, tg_id)
    if student is None:
        await message.answer(atext_need_group(), parse_mode="HTML")
        return

    await start_vote_for_para(conn, bot, message, str(student["group_name"]),
                              date_iso, para, tg_id)


def atext_need_group() -> str:
    """Текст «ты не в группе» (тот же, что в остальных разделах)."""
    from bot.attendance import attendance_texts as atext

    return atext.NEED_GROUP


@router.callback_query(F.data == vote_kb.CB_START)
async def cb_vote_start(callback: CallbackQuery, state: FSMContext,
                        conn) -> None:
    """«📣 Запустить голосование» — показать пары сегодня для выбора."""
    from datetime import date as _date

    from bot.attendance import attendance_texts as atext
    from bot.attendance import attendance_service as att_svc_mod

    tg_id = callback.from_user.id
    if not is_vote_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.NEED_GROUP, show_alert=True)
        return

    group = str(student["group_name"])
    today = datetime.now(KRASNOYARSK).date()
    lessons = att_svc_mod.get_lessons_for_day(conn, group, today)
    if not lessons:
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(vtext.NO_LESSONS_TODAY,
                                          parse_mode="HTML")
        return

    await state.set_state(VoteFlow.waiting_para)
    if callback.message is not None:
        await callback.message.answer(
            vtext.pick_para_prompt(today), parse_mode="HTML",
            reply_markup=vote_kb.start_vote_kb(lessons, today),
        )
    await callback.answer()


@router.callback_query(F.data.startswith(vote_kb.CB_PICK_PREFIX))
async def cb_vote_pick(callback: CallbackQuery, state: FSMContext, conn,
                       bot) -> None:
    """``vote:pick:{date}:{para}`` — староста выбрал пару."""
    from bot.attendance import attendance_texts as atext

    tg_id = callback.from_user.id
    if not is_vote_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    payload = (callback.data or "").removeprefix(vote_kb.CB_PICK_PREFIX)
    parts = payload.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    date_iso, raw_para = parts
    try:
        para = int(raw_para)
        date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    student = att_db.get_student(conn, tg_id)
    if student is None or callback.message is None:
        await callback.answer()
        return

    await state.clear()
    await start_vote_for_para(conn, bot, callback.message,
                              str(student["group_name"]), date_iso, para,
                              tg_id)
    await callback.answer()
@router.callback_query(F.data.startswith(vote_kb.CB_CONFIRM_PREFIX))
async def cb_vote_confirm(callback: CallbackQuery, conn, bot) -> None:
    """``vote:confirm:{date}:{para}:{target}`` — подтвердить студента.

    Группа берётся у голосующего (не из callback): иначе можно было бы
    проголосовать в чужой группе, подделав данные кнопки.
    """
    from bot.attendance import attendance_texts as atext

    payload = (callback.data or "").removeprefix(vote_kb.CB_CONFIRM_PREFIX)
    parts = payload.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    date_iso, raw_para, raw_target = parts
    try:
        para = int(raw_para)
        target_tg_id = int(raw_target)
        date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    voter = att_db.get_student(conn, callback.from_user.id)
    if voter is None:
        await callback.answer(atext.NEED_GROUP, show_alert=True)
        return

    group = str(voter["group_name"])
    poll = vote_svc.get_vote_poll(conn, group, date_iso, para)
    if poll is None:
        await callback.answer(vtext.ALERT_VOTE_CLOSED, show_alert=True)
        return
    if poll.get("is_closed"):
        await callback.answer(vtext.ALERT_VOTE_CLOSED, show_alert=True)
        return

    # Голосующий должен быть в той же группе, что и голосование.
    if not _same_group(conn, group, target_tg_id):
        await callback.answer(vtext.ALERT_NOT_IN_GROUP, show_alert=True)
        return

    target = att_db.get_student(conn, target_tg_id)
    target_name = str(target["full_name"]) if target else "без имени"

    result = vote_svc.add_vote(conn, group, date_iso, para, target_tg_id,
                               target_name, callback.from_user.id)
    if not result["ok"]:
        await callback.answer(vtext.ALERT_ALREADY_VOTED, show_alert=True)
        return

    await vote_svc.update_vote_message(conn, bot, poll)

    if result["attested"]:
        # Объявляем в чат: студент зачтён автоматически.
        await _announce_attested(conn, bot, poll, target_name, result["count"])
        await callback.answer(vtext.ALERT_ATTESTED)
    else:
        await callback.answer(vtext.ALERT_VOTED)


def _same_group(conn, group: str, tg_id: int) -> bool:
    """Состоит ли студент в указанной группе."""
    student = att_db.get_student(conn, tg_id)
    return student is not None and str(student["group_name"]) == group


async def _announce_attested(conn, bot, poll: dict, full_name: str,
                             count: int) -> None:
    """Сообщить в чат, что студент зачтён по голосованию."""
    try:
        await bot.send_message(
            int(poll["chat_id"]), vtext.attested_notice(full_name, count),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("attested notice failed",
                       extra={"error": repr(exc), "poll_id": poll.get("id")})


@router.callback_query(F.data.startswith(vote_kb.CB_CLOSE_PREFIX))
async def cb_vote_close(callback: CallbackQuery, conn, bot) -> None:
    """``vote:close:{date}:{para}`` — закрыть голосование (староста)."""
    from bot.attendance import attendance_texts as atext

    tg_id = callback.from_user.id
    if not is_vote_admin(conn, tg_id):
        await callback.answer(atext.MANAGE_DENIED, show_alert=True)
        return

    payload = (callback.data or "").removeprefix(vote_kb.CB_CLOSE_PREFIX)
    parts = payload.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    date_iso, raw_para = parts
    try:
        para = int(raw_para)
        date.fromisoformat(date_iso)
    except ValueError:
        await callback.answer()
        return

    student = att_db.get_student(conn, tg_id)
    if student is None:
        await callback.answer(atext.NEED_GROUP, show_alert=True)
        return

    poll = vote_svc.get_vote_poll(conn, str(student["group_name"]), date_iso,
                                  para)
    if poll is None:
        await callback.answer(vtext.ALERT_VOTE_CLOSED, show_alert=True)
        return

    result = await vote_svc.close_poll(conn, bot, poll)
    if not result["closed"]:
        await callback.answer(vtext.ALERT_VOTE_CLOSED, show_alert=True)
        return
    await callback.answer("🔒 Голосование закрыто")