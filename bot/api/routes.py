"""Эндпоинты ``/api/...`` для Mini App (ручки, сборка и регистрация маршрутов).

Роуты тонкие: разбор запроса, вызов сервиса, ответ. Логика — в
:mod:`bot.api.schedule`, :mod:`bot.api.deadlines`, :mod:`bot.api.attendance`
и :mod:`bot.api.profile`, поэтому её видно в тестах без HTTP.

Авторизация — декоратор :func:`bot.api.auth.auth_required`: он разбирает
``X-Telegram-Init-Data`` и кладёт ``tg_id`` в запрос. ``/api/health`` открыт
намеренно — это проверка живости, как ``/health``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from aiohttp import web

from bot import db
from bot.attendance import db as att_db
from bot.api import attendance as att_api
from bot.api import deadlines as deadlines_api
from bot.api import profile as profile_api
from bot.api import schedule as schedule_api
from bot.api.auth import auth_required, tg_id_of
from bot.api.keys import CONN_KEY, SETTINGS_KEY
from bot.api.responses import error_response, json_response
from bot.config import KRASNOYARSK
from bot.services import deadline_service

# Коды ошибок HTTP-слоя.
ERR_GROUP_NOT_SET = "group_not_set"
ERR_BAD_DATE = "bad_date"
ERR_BAD_BODY = "bad_body"
ERR_NOT_FOUND = "not_found"

# Повторный ответ на пару — конфликт, а не ошибка ввода.
STATUS_CONFLICT = 409


def _conn(request: web.Request):
    """Соединение SQLite из приложения."""
    return request.app[CONN_KEY]


def _settings(request: web.Request):
    """Настройки приложения (может быть None в тестах)."""
    return request.app[SETTINGS_KEY]


def _now() -> datetime:
    """Текущий момент в поясе техникума (единая точка для ручек)."""
    return datetime.now(KRASNOYARSK)


def _group_or_error(conn, tg_id: int) -> tuple[str | None, str | None]:
    """Группа пользователя или ``(None, код ошибки)`` для ответа 400.

    Отсутствие пользователя и есть «группа не выбрана»: в схеме
    ``users.group_name`` объявлен NOT NULL, записи без группы не бывает.
    """
    group = db.get_user_group(conn, tg_id)
    if not group:
        return None, ERR_GROUP_NOT_SET
    return group, None


def _parse_date_arg(raw: str) -> tuple[date | None, str | None]:
    """Разобрать ``?date=YYYY-MM-DD``.

    Returns:
        ``(дата, None)`` либо ``(None, код_ошибки)``.
    """
    text = (raw or "").strip()
    if not text:
        return None, ERR_BAD_DATE
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, ERR_BAD_DATE


async def _json_body(request: web.Request) -> tuple[dict | None, str | None]:
    """Прочитать JSON-тело запроса.

    Returns:
        ``(тело, None)`` либо ``(None, код_ошибки)``. Не-объект (список,
        число) тоже считается ошибкой: контракт API — объект.
    """
    try:
        body = await request.json()
    except Exception:
        return None, ERR_BAD_BODY
    if not isinstance(body, dict):
        return None, ERR_BAD_BODY
    return body, None


# --- /api/health ---

async def handle_health(request: web.Request) -> web.Response:
    """``GET /api/health`` → ``{'status': 'ok'}`` (без авторизации)."""
    return json_response({
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


@auth_required
async def handle_schedule_today(request: web.Request) -> web.Response:
    """``GET /api/schedule/today`` — расписание группы на сегодня."""
    conn = _conn(request)
    group, error = _group_or_error(conn, tg_id_of(request))
    if error:
        return error_response(error, status=400)

    today = _now().date()
    return json_response(schedule_api.day_payload(conn, group, today,
                                                  today=today))


@auth_required
async def handle_schedule_day(request: web.Request) -> web.Response:
    """``GET /api/schedule/day?date=YYYY-MM-DD`` — расписание на дату."""
    conn = _conn(request)
    group, error = _group_or_error(conn, tg_id_of(request))
    if error:
        return error_response(error, status=400)

    target, date_error = _parse_date_arg(request.query.get("date", ""))
    if date_error:
        return error_response(date_error, status=400)

    return json_response(schedule_api.day_payload(conn, group, target,
                                                  today=_now().date()))


@auth_required
async def handle_schedule_week(request: web.Request) -> web.Response:
    """``GET /api/schedule/week`` — понедельник–суббота текущей недели."""
    conn = _conn(request)
    group, error = _group_or_error(conn, tg_id_of(request))
    if error:
        return error_response(error, status=400)

    return json_response(
        schedule_api.week_payload(conn, group, _now().date())
    )


# --- дедлайны ---

@auth_required
async def handle_deadlines_list(request: web.Request) -> web.Response:
    """``GET /api/deadlines`` — активные дедлайны пользователя."""
    return json_response(
        deadlines_api.deadlines_payload(_conn(request), tg_id_of(request))
    )


@auth_required
async def handle_deadline_create(request: web.Request) -> web.Response:
    """``POST /api/deadlines`` — создать дедлайн (201 при успехе)."""
    conn = _conn(request)
    tg_id = tg_id_of(request)

    body, body_error = await _json_body(request)
    if body_error:
        return error_response(body_error, status=400)

    fields, error = deadlines_api.validate_new_deadline(body)
    if error:
        return error_response(error, status=400)

    deadline_id = deadline_service.add(
        conn, tg_id, fields["subject"], fields["teacher"], fields["task"],
        fields["date_iso"],
    )
    created = deadline_service.get(conn, deadline_id, tg_id)
    if created is None:
        # Запись не читается сразу после вставки — такого быть не должно:
        # отдаём 404, а не пустой объект, чтобы фронт показал ошибку.
        return error_response(ERR_NOT_FOUND, status=404)
    return json_response(
        {"ok": True, "item": deadlines_api.deadline_item(created)}, status=201
    )


@auth_required
async def handle_deadline_delete(request: web.Request) -> web.Response:
    """``DELETE /api/deadlines/{id}`` — удалить свой дедлайн."""
    try:
        deadline_id = int(request.match_info.get("deadline_id", ""))
    except ValueError:
        return error_response(ERR_NOT_FOUND, status=404)

    conn = _conn(request)
    # soft_delete проверяет владельца: чужой id отдаст False → 404.
    if not deadline_service.soft_delete(conn, deadline_id, tg_id_of(request)):
        return error_response(ERR_NOT_FOUND, status=404)
    return json_response({"ok": True})


# --- профиль ---

@auth_required
async def handle_profile(request: web.Request) -> web.Response:
    """``GET /api/profile`` — имя, группа и роль пользователя."""
    return json_response(profile_api.profile_payload(
        _conn(request), tg_id_of(request), _settings(request)
    ))


# --- посещаемость ---

@auth_required
async def handle_attendance(request: web.Request) -> web.Response:
    """``GET /api/attendance`` — сводка за месяц и аттестация по предметам."""
    conn = _conn(request)
    tg_id = tg_id_of(request)
    student = att_db.get_student(conn, tg_id)
    if student is None:
        return error_response(att_api.ERR_NOT_IN_GROUP, status=400)

    return json_response(att_api.month_summary_payload(
        conn, tg_id, str(student["group_name"]), today=_now().date()
    ))


@auth_required
async def handle_attendance_active(request: web.Request) -> web.Response:
    """``GET /api/attendance/active`` — идёт ли пара и отвечал ли студент."""
    return json_response(att_api.active_payload(
        _conn(request), tg_id_of(request), _now()
    ))


@auth_required
async def handle_attendance_answer(request: web.Request) -> web.Response:
    """``POST /api/attendance/answer`` — ответ «Я на паре» / «Меня нет»."""
    conn = _conn(request)
    tg_id = tg_id_of(request)

    body, body_error = await _json_body(request)
    if body_error:
        return error_response(body_error, status=400)

    payload, error = att_api.submit_answer(
        conn, tg_id, str(body.get("answer") or "").strip(), moment=_now()
    )
    if error:
        status = (STATUS_CONFLICT if error == att_api.ERR_ALREADY_ANSWERED
                  else 400)
        return error_response(error, status=status)
    return json_response(payload)


# --- маршруты ---

# Пути сгруппированы по методу: список читается целиком, а порядок регистрации
# не важен — все пути точные, без шаблонов-ловушек.
API_GET_ROUTES = (
    ("/api/health", handle_health),
    ("/api/schedule/today", handle_schedule_today),
    ("/api/schedule/day", handle_schedule_day),
    ("/api/schedule/week", handle_schedule_week),
    ("/api/deadlines", handle_deadlines_list),
    ("/api/profile", handle_profile),
    ("/api/attendance", handle_attendance),
    ("/api/attendance/active", handle_attendance_active),
)

API_POST_ROUTES = (
    ("/api/deadlines", handle_deadline_create),
    ("/api/attendance/answer", handle_attendance_answer),
)

API_DELETE_ROUTES = (
    ("/api/deadlines/{deadline_id}", handle_deadline_delete),
)


def register_api_routes(router: web.UrlDispatcher) -> None:
    """Прописать все ``/api/...`` в роутере приложения.

    OPTIONS отдельно не регистрируется: CORS-middleware перехватывает
    предполётный запрос и отвечает 204 до маршрутизации (проверено на
    aiohttp 3.14).

    Args:
        router: роутер приложения (``app.router``).
    """
    for path, handler in API_GET_ROUTES:
        router.add_get(path, handler)
    for path, handler in API_POST_ROUTES:
        router.add_post(path, handler)
    for path, handler in API_DELETE_ROUTES:
        router.add_delete(path, handler)


__all__ = ["register_api_routes"]
