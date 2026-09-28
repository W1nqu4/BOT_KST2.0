"""Мониторинг и алерты администратору (шаг 12).

Задача — сообщить владельцу о проблемах раньше, чем о них напишут студенты:

- парсер источника падал подряд N раз;
- кэш расписания слишком старый;
- файл БД за сутки вырос кратно (признак утечки или шторма).

Чтобы алерты не превращались в спам, факт отправки запоминается в таблице
``meta``: повторное уведомление по той же причине уходит не чаще, чем раз
в сутки.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from html import escape

from bot.config import TIMEZONE
from bot.db import transaction
from bot.services import cache_service

logger = logging.getLogger(__name__)

# Порог «кэш устарел», часы.
STALE_CACHE_HOURS = 24

# Порог роста размера БД за сутки (в разах).
DB_GROWTH_FACTOR = 2.0

# Ключи в таблице meta.
META_ALERT_PREFIX = "alert:"
META_PARSER_FAILS = "parser_fails"
META_DB_SIZE = "db_size"
META_DB_SIZE_DATE = "db_size_date"

# Сколько подряд падений парсера считаем проблемой.
PARSER_FAIL_THRESHOLD = 2

# Сколько часов не повторять один и тот же алерт.
ALERT_COOLDOWN_HOURS = 24


async def alert_admin(bot, settings, text: str) -> bool:
    """Отправить алерт в админ-чат.

    Args:
        bot: объект Bot.
        settings: настройки (нужен ``admin_chat_id``).
        text: текст сообщения (экранируется).

    Returns:
        True, если сообщение отправлено.
    """
    chat_id = getattr(settings, "admin_chat_id", None) if settings else None
    if not chat_id:
        logger.warning("alert not sent: admin_chat_id is not configured",
                       extra={"text": text})
        return False

    try:
        await bot.send_message(
            chat_id, f"⚠️ <b>Алерт</b>\n{escape(text)}", parse_mode="HTML",
        )
        logger.info("alert sent", extra={"chat_id": chat_id})
        return True
    except Exception as exc:
        logger.warning("alert delivery failed", extra={"error": repr(exc)})
        return False


def get_alert_flag(conn, key: str) -> str | None:
    """Когда последний раз отправляли алерт с этим ключом."""
    return cache_service.get_meta(conn, f"{META_ALERT_PREFIX}{key}")


def set_alert_flag(conn, key: str, moment: datetime | None = None) -> None:
    """Запомнить факт отправки алерта."""
    stamp = (moment or datetime.now(TIMEZONE)).isoformat(timespec="seconds")
    with transaction(conn):
        cache_service._set_meta(conn, f"{META_ALERT_PREFIX}{key}", stamp)


def should_alert(conn, key: str, now: datetime | None = None) -> bool:
    """Пора ли снова алертить (кулдаун по ключу)."""
    raw = get_alert_flag(conn, key)
    if not raw:
        return True
    moment = now or datetime.now(TIMEZONE)
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=moment.tzinfo)
    delta_hours = (moment - last).total_seconds() / 3600
    return delta_hours >= ALERT_COOLDOWN_HOURS


def cache_age_hours(conn) -> float | None:
    """Возраст кэша расписания в часах (None, если меты нет)."""
    seconds = cache_service.cache_age_seconds(
        conn, cache_service.META_LAST_SCHEDULE
    )
    return None if seconds is None else seconds / 3600


def db_size_bytes(db_path: str) -> int:
    """Размер БД в байтах, включая WAL-журнал.

    В режиме WAL свежие данные лежат в ``bot.db-wal``, поэтому размер только
    основного файла не отражает реальный объём (и алерт роста БД никогда бы
    не сработал). Считаем сумму ``.db``, ``.db-wal`` и ``.db-shm``.

    Args:
        db_path: путь к файлу БД.

    Returns:
        Суммарный размер в байтах (0, если файлов нет).
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(f"{db_path}{suffix}")
        except OSError:
            continue
    return total


def check_db_growth(conn, db_path: str,
                    today: str | None = None) -> tuple[bool, int, int]:
    """Сравнить размер БД с записанным ранее.

    Размер фиксируется раз в сутки: если он вырос больше чем в
    :data:`DB_GROWTH_FACTOR` раз за день, это повод для алерта.

    Args:
        conn: соединение SQLite.
        db_path: путь к файлу БД.
        today: сегодняшняя дата в ISO (для тестов).

    Returns:
        ``(подозрительный_рост, текущий_размер, прошлый_размер)``.
    """
    day = today or datetime.now(TIMEZONE).date().isoformat()
    current = db_size_bytes(db_path)

    stored_day = cache_service.get_meta(conn, META_DB_SIZE_DATE)
    stored_size = cache_service.get_meta(conn, META_DB_SIZE)

    if stored_day == day:
        # Сегодня уже записывали — сравнивать не с чем.
        return False, current, int(stored_size or 0)

    previous = int(stored_size or 0)
    with transaction(conn):
        cache_service._set_meta(conn, META_DB_SIZE, str(current))
        cache_service._set_meta(conn, META_DB_SIZE_DATE, day)

    if previous and current > previous * DB_GROWTH_FACTOR:
        return True, current, previous
    return False, current, previous


def note_parser_result(conn, ok: bool) -> int:
    """Запомнить результат прохода парсера.

    Args:
        conn: соединение SQLite.
        ok: успешен ли проход.

    Returns:
        Сколько падений подряд накопилось (0 при успехе).
    """
    fails = int(cache_service.get_meta(conn, META_PARSER_FAILS) or 0)
    fails = 0 if ok else fails + 1
    with transaction(conn):
        cache_service._set_meta(conn, META_PARSER_FAILS, str(fails))
    return fails


async def check_health(conn, bot, settings,
                       now: datetime | None = None) -> list[str]:
    """Проверить состояние и отправить алерты при проблемах.

    Проверки:

    1. парсер источника падал подряд :data:`PARSER_FAIL_THRESHOLD` раз;
    2. возраст кэша расписания больше :data:`STALE_CACHE_HOURS`;
    3. размер БД вырос больше чем в :data:`DB_GROWTH_FACTOR` раз за сутки.

    Каждый алерт отправляется не чаще раза в :data:`ALERT_COOLDOWN_HOURS`.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        settings: настройки.
        now: текущий момент (для тестов).

    Returns:
        Список ключей отправленных алертов.
    """
    moment = now or datetime.now(TIMEZONE)
    sent: list[str] = []

    # 1. Падения парсера.
    fails = int(cache_service.get_meta(conn, META_PARSER_FAILS) or 0)
    if fails >= PARSER_FAIL_THRESHOLD and should_alert(
        conn, "parser_fails", moment
    ):
        await alert_admin(
            bot, settings,
            f"Источник не обновляется: {fails} неудачных попыток подряд. "
            "Проверь, не изменился ли формат DOCX/HTML на сайте техникума.",
        )
        set_alert_flag(conn, "parser_fails", moment)
        sent.append("parser_fails")

    # 2. Устаревший кэш.
    age = cache_age_hours(conn)
    if age is not None and age > STALE_CACHE_HOURS and should_alert(
        conn, "stale_cache", moment
    ):
        await alert_admin(
            bot, settings,
            f"Кэш расписания не обновлялся {age:.0f} ч. "
            "Студенты видят устаревшие данные.",
        )
        set_alert_flag(conn, "stale_cache", moment)
        sent.append("stale_cache")

    # 3. Резкий рост БД.
    db_path = getattr(settings, "db_path", "") if settings else ""
    if db_path:
        grew, current, previous = check_db_growth(
            conn, db_path, today=moment.date().isoformat()
        )
        if grew and should_alert(conn, "db_growth", moment):
            await alert_admin(
                bot, settings,
                f"Размер БД вырос с {previous / 1024:.0f} КБ до "
                f"{current / 1024:.0f} КБ за сутки.",
            )
            set_alert_flag(conn, "db_growth", moment)
            sent.append("db_growth")

    if sent:
        logger.info("health alerts sent", extra={"alerts": sent})
    return sent


async def health_loop(conn, bot, settings, interval: int = 3600) -> None:
    """Бесконечный цикл проверок (раз в час).

    Ошибки одного прохода не роняют задачу; ``asyncio.CancelledError``
    пролетает наружу для корректной остановки приложения.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        settings: настройки.
        interval: пауза между проверками, сек.

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    from bot.services.schedule_service import _sleep

    while True:
        try:
            await check_health(conn, bot, settings)
        except asyncio.CancelledError:
            logger.info("health_loop cancelled")
            raise
        except Exception:
            logger.exception("health_loop iteration failed")
        await _sleep(interval)
        return 0