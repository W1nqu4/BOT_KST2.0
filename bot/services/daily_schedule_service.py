"""Ежедневная рассылка расписания в чаты (шаг 5).

Замены приходят не каждый день, но расписание нужно всегда: студенты
планируют следующий день независимо от того, были замены или нет. Поэтому
вечером (см. :data:`bot.config.DAILY_SCHEDULE_HOUR`) полное расписание на
завтра уходит во ВСЕ привязанные чаты.

Как это сочетается с рассылкой замен (шаг 2, ``notify_service``):

- замены уходят мгновенно, как только появились (окно 15:30–23:00);
- ежедневная задача добивает те чаты, которым ничего не пришло, потому что
  замен на их группу не было;
- двойной отправки не будет: и там, и там проверяется
  ``last_full_schedule_sent_date`` — дата, на которую расписание уже ушло.

Отправка идёт через :func:`bot.services.notify_service._send_to_chat`: он уже
снимает прежнее закрепление, отправляет сообщения, закрепляет расписание
(кроме пустого дня) и корректно обрабатывает ошибки Telegram. Дублировать
эту логику здесь нельзя — разойдётся поведение закрепления.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from bot import db
from bot.config import DAILY_SCHEDULE_HOUR, KRASNOYARSK
from bot.services.schedule_service import _sleep
from bot.services.notify_service import (
    _send_to_chat,
    build_full_schedule_for_chat,
    next_school_day,
)

logger = logging.getLogger(__name__)

# Период проверки: раз в 15 минут. Час отправки «широкий» — если бот
# запустился в 18:15 (был выключен в 18:00), расписание всё равно уйдёт.
DAILY_CHECK_INTERVAL_SECONDS = 15 * 60


async def send_daily_to_all_chats(conn, bot, target: date) -> int:
    """Отправить расписание на дату во все привязанные чаты.

    Пропускаются чаты с выключенными уведомлениями и те, куда расписание на
    эту дату уже отправлено (``last_full_schedule_sent_date``) — это защита от
    дубля, если замены уже разослали расписание раньше или если проходов
    внутри часа было несколько.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        target: дата расписания (обычно завтрашний учебный день).

    Returns:
        Количество чатов, куда расписание ушло.
    """
    target_iso = target.isoformat()
    sent = 0

    for chat in db.get_all_group_chats(conn):
        chat_id = int(chat["chat_id"])

        if not chat.get("notifications_enabled"):
            continue
        if chat.get("last_full_schedule_sent_date") == target_iso:
            continue      # на эту дату уже отправляли (рассылка замен успела)
        if db.get_group_chat(conn, chat_id) is None:
            continue      # чат отвязали между чтением списка и отправкой

        group = str(chat["group_name"])
        texts = build_full_schedule_for_chat(conn, group, target,
                                             new_subs_anyway=True)
        if not texts:
            continue

        # _send_to_chat сам снимает старое закрепление, отправляет и
        # закрепляет (пустой день не закрепляется), а при TelegramForbiddenError
        # удаляет привязку чата.
        if await _send_to_chat(conn, bot, chat_id, texts,
                               date_iso=target_iso):
            sent += 1
        db.mark_group_chat_full_sent(conn, chat_id, target_iso)

    if sent:
        logger.info("daily schedule sent",
                    extra={"date": target_iso, "chats": sent})
    return sent


async def daily_schedule_loop(conn, bot, now_provider=None) -> None:
    """Раз в день отправляет расписание на завтра во все привязанные чаты.

    Работает только в час :data:`bot.config.DAILY_SCHEDULE_HOUR` (по
    Красноярску): вечером лист замен уже опубликован, и расписание на завтра
    актуально. Проверка идёт каждые 15 минут, поэтому запуск бота в 18:15
    (после простоя в 18:00) тоже отправит расписание — но ровно один раз
    за дату благодаря дедупликации.

    Ошибка прохода не роняет задачу: логируется, и цикл продолжается.
    ``asyncio.CancelledError`` пролетает наружу для корректной остановки.

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
            now = moment_provider()
            if now.hour == DAILY_SCHEDULE_HOUR:
                await send_daily_to_all_chats(
                    conn, bot, next_school_day(now.date())
                )
        except asyncio.CancelledError:
            logger.info("daily_schedule_loop cancelled")
            raise
        except Exception:
            logger.exception("daily_schedule_loop failed")
        await _sleep(DAILY_CHECK_INTERVAL_SECONDS)