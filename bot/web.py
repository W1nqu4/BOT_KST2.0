"""aiohttp-сервер: /health и /calendar/{token}.ics (шаг 9).

Сервер живёт рядом с polling: один процесс, общее соединение с SQLite.
Эндпоинты:

- ``/health`` — мониторинг (Amvera проверяет его перед переключением трафика):
  200, если БД отвечает, иначе 503;
- ``/calendar/{token}.ics`` — подписка. Токен — единственный идентификатор
  пользователя для внешнего клиента, Telegram id в ссылку не попадает.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from aiohttp import web

from bot import db
from bot.config import ICS_HORIZON_DAYS, ICS_PUBLISHED_TTL, ICS_TIMEZONE
from bot.services import cache_service, deadline_service, ics_service

logger = logging.getLogger(__name__)

# Ключи приложения: aiohttp 3.9+ рекомендует AppKey вместо строковых ключей
# (строковые дают NotAppKeyWarning и не типизируются).
CONN_KEY: web.AppKey = web.AppKey("conn")
SETTINGS_KEY: web.AppKey = web.AppKey("settings")

# Имя файла в Content-Disposition: клиенты используют его при скачивании.
ICS_FILENAME = "schedule.ics"


def _json_response(payload: dict, status: int = 200) -> web.Response:
    """JSON-ответ с UTF-8 (ensure_ascii=False, чтобы кириллица читалась)."""
    return web.json_response(
        payload, status=status,
        dumps=lambda data: json.dumps(data, ensure_ascii=False),
    )


async def handle_health(request: web.Request) -> web.Response:
    """Проверка живости: статус БД, версия схемы, кэш, пользователи.

    Returns:
        200 с JSON, если БД отвечает; 503 — если запрос к ней упал.
    """
    conn = request.app[CONN_KEY]
    try:
        users_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1"
        ).fetchone()[0]
        schema_version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
        last_update = cache_service.get_meta(conn, cache_service.META_LAST_SCHEDULE)
        last_subs = cache_service.get_meta(
            conn, cache_service.META_LAST_SUBSTITUTIONS
        )
        lessons = conn.execute(
            "SELECT COUNT(*) FROM schedule_cache"
        ).fetchone()[0]
        history_rows = db.count_substitution_history(conn)
        history_earliest = db.earliest_substitution_history_date(conn)
    except Exception as exc:
        logger.exception("health check failed")
        return _json_response(
            {"status": "error", "db": "error", "error": repr(exc)}, status=503
        )

    settings = request.app[SETTINGS_KEY]
    public_base_url = getattr(settings, "public_base_url", "") if settings else ""

    return _json_response({
        "status": "ok",
        "db": "ok",
        "schema_version": int(schema_version),
        "users_count": int(users_count),
        "last_schedule_update": last_update,
        "last_substitutions_update": last_subs,
        "lessons_cached": int(lessons),
        "substitution_history_rows": int(history_rows),
        "substitution_history_earliest": history_earliest,
        "public_base_url": public_base_url,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


async def handle_calendar(request: web.Request) -> web.Response:
    """Отдать .ics-файл по токену подписки.

    Returns:
        200 с ``text/calendar``; 404 — токен неизвестен или у пользователя
        не выбрана группа.
    """
    conn = request.app[CONN_KEY]
    token = request.match_info.get("token", "")
    tg_id = ics_service.get_tg_id_by_token(conn, token)
    if tg_id is None:
        logger.info("calendar: unknown token",
                    extra={"token_prefix": token[:8]})
        return web.Response(status=404, text="Ссылка не найдена")

    group = db.get_user_group(conn, tg_id)
    if not group:
        return web.Response(
            status=404,
            text="Укажите группу в боте: /start → введите номер группы",
        )

    deadlines = deadline_service.list_active(conn, tg_id)
    try:
        ics_text = ics_service.build_ics(conn, group, deadlines=deadlines)
    except Exception:
        logger.exception("calendar: ics generation failed",
                         extra={"group": group})
        return web.Response(status=500, text="Не удалось собрать календарь")

    return web.Response(
        text=ics_text,
        content_type="text/calendar",
        charset="utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{ICS_FILENAME}"',
            "Cache-Control": "no-cache, must-revalidate",
            "X-PUBLISHED-TTL": ICS_PUBLISHED_TTL,
        },
    )


def create_app(conn, settings=None) -> web.Application:
    """Собрать aiohttp-приложение.

    Args:
        conn: соединение SQLite (общее с polling).
        settings: настройки приложения (в тестах может отсутствовать).

    Returns:
        Готовое ``web.Application`` с двумя маршрутами.
    """
    app = web.Application()
    app[CONN_KEY] = conn
    app[SETTINGS_KEY] = settings
    app.router.add_get("/health", handle_health)
    app.router.add_get("/calendar/{token}.ics", handle_calendar)
    logger.info(
        "web app created",
        extra={"horizon_days": ICS_HORIZON_DAYS, "timezone": ICS_TIMEZONE},
    )
    return app
