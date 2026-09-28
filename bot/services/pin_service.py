"""Закрепление расписания в чате и снятие после последней пары (шаг 4).

Бот закрепляет сообщение с расписанием на сегодня, чтобы оно висело сверху
у всех участников, и открепляет его, когда пары закончились — иначе
закреплённое вчера расписание вводило бы в заблуждение.

Как определяется момент открепления:

- берём пары группы на сегодня из расписания (:func:`get_lessons_for_day`,
  то есть с учётом чётности);
- находим время окончания ПОСЛЕДНЕЙ пары по звонкам нужного дня
  (в субботу звонки другие — см. :data:`bot.config.BELL_TIMES_SATURDAY`);
- ждём ещё :data:`UNPIN_AFTER_MINUTES` минут (запас на «дошёл до телефона»)
  и открепляем.

Отдельно снимаем закрепление, если оно относится к другой дате: такое
сообщение устарело независимо от расписания.

В личке закреплять нельзя (Telegram не поддерживает) — только чаты.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time

from bot import db
from bot.config import (
    BELL_TIMES_SATURDAY,
    BELL_TIMES_WEEKDAY,
    KRASNOYARSK,
)
from bot.services.schedule_service import _sleep, get_lessons_for_day

logger = logging.getLogger(__name__)

# Запас после последней пары, минуты: студент успевает дойти до телефона,
# а расписание остаётся закреплённым ровно на учебный день.
UNPIN_AFTER_MINUTES = 5

# Период проверки. Раз в 5 минут: момент открепления не критичен, а частые
# проходы дают лишние запросы к Telegram.
UNPIN_INTERVAL_SECONDS = 5 * 60

# Причина, по которой открепляем (в логах — чтобы понять, что произошло).
REASON_STALE_DATE = "stale_date"
REASON_NO_LESSONS = "no_lessons"
REASON_LESSONS_OVER = "lessons_over"


def last_lesson_end_time(day: date, paras: list[int]) -> time | None:
    """Время окончания последней пары в указанный день.

    В субботу звонки отличаются: 3 и 4 пары идут одним уроком, 5 пары нет.
    Поэтому для субботы берётся :data:`bot.config.BELL_TIMES_SATURDAY`.

    Args:
        day: дата (определяет, будни это или суббота).
        paras: номера пар дня (например ``[1, 2, 3]``).

    Returns:
        Время окончания последней пары или None, если пар нет либо для
        последней пары в этот день нет звонков (пример: пара 5 в субботу).
    """
    if not paras:
        return None

    max_para = max(paras)
    bells = BELL_TIMES_SATURDAY if day.weekday() == 5 else BELL_TIMES_WEEKDAY
    pair = bells.get(max_para)
    if not pair:
        return None

    try:
        hh, mm = pair[1].split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        logger.warning("could not parse bell time",
                       extra={"para": max_para, "value": pair[1]})
        return None


def lesson_paras_for_day(conn, group: str, day: date) -> list[int]:
    """Номера пар группы на дату (учёт чётности — как на экране расписания)."""
    lessons = get_lessons_for_day(conn, group, day)
    return [int(lesson["para_number"]) for lesson in lessons]
async def _try_unpin(bot, conn, chat_id: int, message_id: int,
                     reason: str = "") -> bool:
    """Открепить сообщение и снять отметку в БД.

    Ошибку Telegram игнорируем: закрепление могли снять вручную, и тогда
    ``unpinChatMessage`` вернёт ошибку — это норма, а не сбой. Отметку в БД
    снимаем в любом случае, иначе цикл будет пытаться открепить её вечно.

    Args:
        bot: объект Bot.
        conn: соединение SQLite.
        chat_id: чат.
        message_id: id закреплённого сообщения.
        reason: причина (для лога).

    Returns:
        True, если Telegram подтвердил открепление.
    """
    ok = False
    try:
        await bot.unpin_chat_message(chat_id, message_id=message_id)
        ok = True
        logger.info("pinned schedule unpinned",
                    extra={"chat_id": chat_id, "message_id": message_id,
                           "reason": reason})
    except Exception as exc:
        logger.warning("unpin failed",
                       extra={"chat_id": chat_id, "message_id": message_id,
                              "reason": reason, "error": repr(exc)})
    finally:
        db.clear_pinned_message(conn, chat_id)
    return ok


async def unpin_due_chats(conn, bot, now: datetime) -> int:
    """Один проход открепления: снять устаревшие закрепления.

    Логика по каждому чату с закреплением:

    1. закреплено на ПРОШЛУЮ дату → снять (сообщение устарело);
    2. закреплено на БУДУЩУЮ дату → оставить: вечером рассылка закрепляет
       расписание на ЗАВТРА (см. :func:`next_school_day`), и снимать его
       сразу нельзя — пары этой даты ещё не начались;
    3. на дату закрепления пар нет → снять сразу;
    4. пары уже кончились (+запас) → снять;
    5. иначе — оставить.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now: текущий момент (по Красноярску) — параметр для тестов.

    Returns:
        Количество откреплённых сообщений.
    """
    today = now.date()
    today_iso = today.isoformat()
    now_minutes = now.hour * 60 + now.minute
    unpinned = 0

    for chat in db.get_all_group_chats(conn):
        chat_id = int(chat["chat_id"])
        pinned = db.get_pinned_message(conn, chat_id)
        if not pinned:
            continue

        message_id = int(pinned["pinned_message_id"])
        pinned_date = str(pinned.get("pinned_date_iso") or "")

        if pinned_date and pinned_date < today_iso:
            # Расписание прошлого дня: снять независимо от времени.
            if await _try_unpin(bot, conn, chat_id, message_id,
                                REASON_STALE_DATE):
                unpinned += 1
            continue

        if pinned_date and pinned_date > today_iso:
            # Закреплено расписание на будущую дату — оно актуально.
            continue

        # Закрепление на сегодня: смотрим, кончились ли пары.
        paras = lesson_paras_for_day(conn, str(chat["group_name"]), today)
        end = last_lesson_end_time(today, paras)

        if end is None:
            if await _try_unpin(bot, conn, chat_id, message_id,
                                REASON_NO_LESSONS):
                unpinned += 1
            continue

        end_minutes = end.hour * 60 + end.minute
        if now_minutes > end_minutes + UNPIN_AFTER_MINUTES:
            if await _try_unpin(bot, conn, chat_id, message_id,
                                REASON_LESSONS_OVER):
                unpinned += 1

    return unpinned


async def unpin_after_lessons_loop(conn, bot, now_provider=None) -> None:
    """Бесконечный цикл открепления расписания после последней пары.

    Ошибка прохода не роняет задачу: логируется, и цикл продолжается.
    ``asyncio.CancelledError`` пролетает наружу — иначе задачу нельзя
    корректно остановить при завершении приложения.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now_provider: функция «текущий момент» (для тестов).

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    moment_provider = now_provider or (lambda: datetime.now(KRASNOYARSK))
    while True:
        try:
            await unpin_due_chats(conn, bot, moment_provider())
        except asyncio.CancelledError:
            logger.info("unpin_after_lessons_loop cancelled")
            raise
        except Exception:
            logger.exception("unpin loop failed")
        await _sleep(UNPIN_INTERVAL_SECONDS)