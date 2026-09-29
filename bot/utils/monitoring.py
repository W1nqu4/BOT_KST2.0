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


# --- notify_admin: личные алерты владельцу (этап 1 посещаемости) ---

# Кулдаун по ключу «модуль + класс ошибки», секунды. Ключи держим в памяти
# процесса: это оперативные алерты о конкретных сбоях, и после перезапуска
# полезно узнать о проблеме снова, а не молчать сутки.
NOTIFY_THROTTLE_SECONDS = 60

# Сколько символов traceback показывать (остальное не читают, а сообщение
# рискует не пролезть в лимит Telegram).
NOTIFY_TRACEBACK_LIMIT = 500

# Момент последней отправки по ключу: {(module, error_class): timestamp}.
_notify_last_sent: dict[tuple[str, str], float] = {}


def reset_notify_throttle() -> None:
    """Сбросить кулдаун notify_admin (для тестов)."""
    _notify_last_sent.clear()


def _first_admin_id(settings) -> int | None:
    """tg_id первого админа из ``settings.admin_ids`` (или None)."""
    admin_ids = getattr(settings, "admin_ids", ()) or ()
    return int(admin_ids[0]) if admin_ids else None


def notify_admin_message(text: str, error: Exception | None = None,
                         module: str = "",
                         moment: datetime | None = None) -> str:
    """Собрать текст алерта владельцу.

    Args:
        text: что случилось (человекочитаемо).
        error: исключение, если есть.
        module: имя модуля/задачи, где произошло.
        moment: момент времени (для тестов).

    Returns:
        HTML-текст сообщения.
    """
    now = moment or datetime.now(TIMEZONE)
    error_class = type(error).__name__ if error is not None else "—"
    traceback_text = ""
    if error is not None:
        import traceback

        raw = "".join(traceback.format_exception(
            type(error), error, error.__traceback__
        ))
        traceback_text = escape(raw[:NOTIFY_TRACEBACK_LIMIT])

    lines = [
        "⚠️ <b>Ошибка в боте</b>",
        "",
        f"🕐 {now.strftime('%d.%m.%Y %H:%M')} (Krasnoyarsk)",
        f"📍 Модуль: <b>{escape(module)}</b>",
        f"❌ <code>{escape(error_class)}</code>",
        "",
        escape(text),
    ]
    if traceback_text:
        lines.extend(["", f"<code>{traceback_text}</code>"])
    lines.extend(["", "По вопросам: @W1nqu4"])
    return "\n".join(lines)


async def notify_admin(bot, settings, text: str,
                       error: Exception | None = None,
                       module: str = "",
                       throttle_seconds: int = NOTIFY_THROTTLE_SECONDS,
                       now: float | None = None) -> bool:
    """Сообщить владельцу об ошибке в личку (первому из ``admin_ids``).

    Кулдаун считается по ключу «модуль + класс ошибки»: одна и та же ошибка
    не долбит в личку чаще, чем раз в ``throttle_seconds``, а разные ошибки
    приходят независимо.

    Args:
        bot: объект Bot.
        settings: настройки (нужен ``admin_ids``).
        text: что случилось.
        error: исключение, если есть.
        module: имя модуля/задачи.
        throttle_seconds: пауза для повторной такой же ошибки.
        now: текущий monotonic-момент (для тестов).

    Returns:
        True, если сообщение отправлено (или False при кулдауне/ошибке).
    """
    import time

    admin_id = _first_admin_id(settings)
    if admin_id is None:
        logger.warning("notify_admin skipped: admin_ids is empty",
                       extra={"src_module": module})
        return False

    error_class = type(error).__name__ if error is not None else "—"
    key = (module, error_class)
    moment = time.monotonic() if now is None else now

    last = _notify_last_sent.get(key)
    if last is not None and moment - last < throttle_seconds:
        logger.info("notify_admin throttled",
                    extra={"src_module": module, "error_class": error_class})
        return False

    _notify_last_sent[key] = moment

    try:
        await bot.send_message(
            admin_id,
            notify_admin_message(text, error=error, module=module),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("notify_admin delivery failed",
                       extra={"admin_id": admin_id, "error": repr(exc)})
        return False

    logger.info("notify_admin sent",
                extra={"admin_id": admin_id, "src_module": module,
                       "error_class": error_class})
    return True


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
        text = (
            f"Источник не обновляется: {fails} неудачных попыток подряд. "
            "Проверь, не изменился ли формат DOCX/HTML на сайте техникума."
        )
        await alert_admin(bot, settings, text)
        set_alert_flag(conn, "parser_fails", moment)
        sent.append("parser_fails")
        # Дублируем владельцу в личку: ADMIN_CHAT_ID может быть не задан,
        # а о поломке парсера знать нужно (этап 1: notify_admin).
        await notify_admin(bot, settings, text,
                           module="parser.substitutions")

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