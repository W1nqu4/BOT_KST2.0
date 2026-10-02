"""aiohttp-сервер: /health, /calendar/{token}.ics, API Mini App и /app/.

Сервер живёт рядом с polling: один процесс, общее соединение с SQLite.
Эндпоинты:

- ``/health`` — мониторинг (Railway проверяет его перед переключением трафика):
  200, если БД отвечает, иначе 503;
- ``/calendar/{token}.ics`` — подписка. Токен — единственный идентификатор
  пользователя для внешнего клиента, Telegram id в ссылку не попадает;
- ``/api/...`` — Mini App (см. :mod:`bot.api.routes`): расписание, дедлайны,
  профиль и посещаемость. Авторизация — подписанный Telegram initData;
- ``/app/...`` — собранная SPA (``webapp/out``), если её собрали: статика
  отдаётся как файлы, ``/app`` и ``/app/`` — ``index.html``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from bot import db
from bot.api.keys import CONN_KEY, SETTINGS_KEY
from bot.api.routes import register_api_routes
from bot.config import ICS_HORIZON_DAYS, ICS_PUBLISHED_TTL, ICS_TIMEZONE
from bot.services import cache_service, deadline_service, ics_service

logger = logging.getLogger(__name__)

# Соединение и настройки: определены в bot.api.keys (чтобы пакет API не
# импортировал bot.web и не было цикла). Реэкспорт — тесты берут их отсюда.
__all__ = [
    "CONN_KEY",
    "SETTINGS_KEY",
    "WEBAPP_DIR",
    "create_app",
    "handle_calendar",
    "handle_health",
]

# Имя файла в Content-Disposition: клиенты используют его при скачивании.
ICS_FILENAME = "schedule.ics"

# Собранный фронт: bot/web.py → bot/ → корень проекта → webapp/out.
# В Docker копируется в /app/webapp/out (см. Dockerfile).
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp" / "out"

# Префикс раздачи Mini App: совпадает с basePath в webapp/next.config.mjs.
WEBAPP_PREFIX = "/app/"


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
        groups_with_schedule = db.list_available_groups(conn)
        history_rows = db.count_substitution_history(conn)
        history_dates = db.count_substitution_history_dates(conn)
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
        "groups_with_schedule": groups_with_schedule,
        "substitution_history_rows": int(history_rows),
        "substitution_history_dates_count": int(history_dates),
        "substitution_history_earliest": history_earliest,
        "substitution_history_latest":
            db.latest_substitution_history_date(conn),
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


def spa_index() -> Path:
    """Путь к ``index.html`` собранной SPA.

    Returns:
        ``webapp/out/index.html``. Проверять существование должна вызывающая
        сторона: путь нужен и для ответа 404-заглушки, когда сборки нет.
    """
    return WEBAPP_DIR / "index.html"


@web.middleware
async def cors_middleware(request: web.Request,
                          handler) -> web.StreamResponse:
    """Добавить CORS-заголовки и ответить на предполётный OPTIONS.

    Открытый ``Access-Control-Allow-Origin: *`` безопасен: защита — подпись
    Telegram initData, а не Origin (его подделать тривиально, подпись — нет).
    В продакшене фронт и API на одном origin, поэтому заголовки там просто
    не нужны, но и не мешают.

    OPTIONS перехватывается до маршрутизации: отдельные OPTIONS-роуты для
    каждого пути не нужны (браузер присылает их на любой ``/api/...``).
    """
    if request.method == "OPTIONS":
        # Предполётный запрос: тело не нужно, только разрешения.
        response: web.StreamResponse = web.Response(status=204)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = (
        "X-Telegram-Init-Data, Content-Type"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, DELETE, OPTIONS"
    )
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


def create_app(conn, settings=None) -> web.Application:
    """Собрать aiohttp-приложение.

    Args:
        conn: соединение SQLite (общее с polling).
        settings: настройки приложения (в тестах может отсутствовать).

    Returns:
        Готовое ``web.Application``: ``/health``, ``/calendar/{token}.ics``,
        ``/api/...`` и статика Mini App по ``/app/`` (если ``webapp/out``
        собран — иначе маршруты не регистрируются и ``/app/`` отдаёт 404).
    """
    app = web.Application(middlewares=(cors_middleware,))
    app[CONN_KEY] = conn
    app[SETTINGS_KEY] = settings

    app.router.add_get("/health", handle_health)
    app.router.add_get("/calendar/{token}.ics", handle_calendar)
    register_api_routes(app.router)
    _register_webapp_routes(app)

    logger.info(
        "web app created",
        extra={"horizon_days": ICS_HORIZON_DAYS, "timezone": ICS_TIMEZONE,
               "miniapp": WEBAPP_DIR.is_dir()},
    )
    return app


def _register_webapp_routes(app: web.Application) -> None:
    """Раздать собранный фронт по ``/app/``, если он есть.

    Порядок регистрации важен: ``add_get('/app')`` и ``add_get('/app/')``
    ставятся ДО ``add_static``. При обратном порядке точный маршрут
    ``/app/`` попадает в статик-ресурс, тот видит каталог и отвечает
    ``403 Forbidden`` (проверено на aiohttp 3.14) — SPA не открывается.

    Args:
        app: приложение, в роутер которого добавляются маршруты.
    """
    if not WEBAPP_DIR.is_dir() or not spa_index().is_file():
        # Фронт не собран (локальный запуск бота без pnpm build, например).
        # Молча не регистрируем: бот работает как раньше, /app/ → 404.
        logger.info("miniapp static not mounted", extra={"dir": str(WEBAPP_DIR)})
        return

    async def spa_fallback(request: web.Request) -> web.StreamResponse:
        """Отдать ``index.html`` для клиентских путей SPA."""
        return web.FileResponse(spa_index())

    app.router.add_get("/app", spa_fallback)
    app.router.add_get("/app/", spa_fallback)
    app.router.add_static(WEBAPP_PREFIX, path=str(WEBAPP_DIR), name="miniapp")
    logger.info("miniapp static mounted", extra={"dir": str(WEBAPP_DIR)})
