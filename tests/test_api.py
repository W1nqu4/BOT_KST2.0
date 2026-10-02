"""Тесты HTTP API Mini App: авторизация, эндпоинты и раздача статики.

Поднимается настоящий aiohttp-сервер (``aiohttp.test_utils``), поэтому
проверяются маршрутизация, заголовки и коды ответов.

initData подписывается по алгоритму Telegram
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app)
токеном из фейковых настроек: так проверяется реальная цепочка авторизации,
а не подмена ``safe_parse_webapp_init_data``.

Время фиксируется через ``monkeypatch`` на ``bot.api.routes._now`` — иначе
тесты зависели бы от дня запуска (расписание привязано к дню недели, а
активная пара — к звонкам).
"""

import hashlib
import hmac
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.attendance import attendance_db as att
from bot.api import routes as api_routes
from bot.attendance.models import MODE_CHAT, ROLE_STAROSTA, STATUS_PRESENT
from bot.config import KRASNOYARSK, MIN_ATTESTATION_LESSONS
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import cache_service, deadline_service
from bot.web import create_app

# Токен для подписи initData в тестах: настройки подделываются, сети нет.
TOKEN = "123456:TEST_TOKEN_FOR_API"

GROUP = "26КАД"
TG_ID = 555

# Фиксированное «сейчас»: среда, 7 октября 2026, 09:03 — идёт 1 пара
# (звонки будней: 09:00–10:35), окно ответа (5 минут) ещё открыто.
NOW = datetime(2026, 10, 7, 9, 3, tzinfo=KRASNOYARSK)
TODAY = NOW.date()
# Понедельник недели TODAY: 5 октября 2026.
WEEK_START = date(2026, 10, 5)

WEEKDAY_NAMES = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница",
                 "Суббота")


class FakeSettings:
    """Настройки для тестов: нужны только токен и список админов."""

    bot_token = TOKEN
    public_base_url = "https://example.test"
    admin_ids: tuple[int, ...] = ()
    port = 8080


def make_init_data(tg_id: int = TG_ID, token: str = TOKEN,
                   username: str = "ivan_petrov") -> str:
    """Собрать подписанный initData так, как это делает клиент Telegram."""
    fields = {
        "auth_date": "1790875823",
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": tg_id, "first_name": "Иван",
                            "username": username}),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def auth_headers(tg_id: int = TG_ID, token: str = TOKEN) -> dict:
    """Заголовки с валидным initData."""
    return {"X-Telegram-Init-Data": make_init_data(tg_id, token)}
@pytest.fixture()
def frozen_now(monkeypatch):
    """Зафиксировать «сейчас» для ручек Mini App.

    ``bot.api.routes._now`` — единственная точка «текущего момента» в API,
    поэтому подмена здесь делает тесты независимыми от дня запуска.
    """
    monkeypatch.setattr(api_routes, "_now", lambda: NOW)
    return NOW


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями, пользователем, группой и студентом."""
    connection = get_connection(tmp_path / "test_api.db")
    apply_migrations(connection)
    with transaction(connection):
        connection.execute(
            "INSERT INTO users (tg_id, group_name, full_name, created_at)"
            " VALUES (?, ?, ?, '2026-09-01T00:00:00+07:00')",
            (TG_ID, GROUP, "Петров Иван"),
        )
        connection.execute(
            "INSERT INTO study_groups"
            " (group_name, invite_code, starosta_tg_id, created_at,"
            "  created_by, attendance_mode)"
            " VALUES (?, '123456', ?, '2026-09-01T00:00:00+07:00', ?, ?)",
            (GROUP, TG_ID, TG_ID, MODE_CHAT),
        )
        connection.execute(
            "INSERT INTO students (tg_id, group_name, full_name, role,"
            " joined_at) VALUES (?, ?, 'Петров И. А.', ?,"
            " '2026-09-01T00:00:00+07:00')",
            (TG_ID, GROUP, ROLE_STAROSTA),
        )
    yield connection
    connection.close()


@pytest.fixture()
def schedule(conn) -> None:
    """Расписание группы: среда (и только она), две пары каждую неделю."""
    rows = [
        {"group_name": GROUP, "day_of_week": 3, "para_number": 1,
         "subject": "ОД.07 Математика", "teacher": "Соколова Елена Викторовна",
         "room": "204", "week_type": ""},
        {"group_name": GROUP, "day_of_week": 3, "para_number": 2,
         "subject": "ОД.12 Химия", "teacher": "Витюгова Наталья Владимировна",
         "room": "313А", "week_type": ""},
    ]
    cache_service.save_schedule(conn, rows)


@pytest.fixture()
async def client(conn, schedule, frozen_now):
    """Тестовый HTTP-клиент с настроенным приложением и временем."""
    server = TestServer(create_app(conn, FakeSettings()))
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def make_client(conn, settings=None) -> TestClient:
    """Поднять ещё один клиент к тому же соединению (сценарии с настройками)."""
    server = TestServer(create_app(conn, settings or FakeSettings()))
    test_client = TestClient(server)
    await test_client.start_server()
    return test_client


async def add_user(conn, tg_id: int, group: str,
                   name: str = "Без группы") -> None:
    """Добавить пользователя расписания без записи в ``students``."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, full_name, created_at)"
            " VALUES (?, ?, ?, 'x')", (tg_id, group, name)
        )


# --- авторизация ---

async def test_api_schedule_without_init_data_401(client) -> None:
    """Без заголовка X-Telegram-Init-Data — 401 и код ошибки."""
    response = await client.get("/api/schedule/today")
    assert response.status == 401
    assert (await response.json())["error"] == "invalid_init_data"


async def test_api_schedule_bad_signature_401(client) -> None:
    """Подпись чужим токеном не проходит: 401."""
    response = await client.get(
        "/api/schedule/today", headers=auth_headers(token="999:WRONG")
    )
    assert response.status == 401
    assert (await response.json())["error"] == "invalid_init_data"


async def test_api_schedule_garbage_init_data_401(client) -> None:
    """Мусор вместо initData — 401, без падения сервера."""
    response = await client.get(
        "/api/schedule/today", headers={"X-Telegram-Init-Data": "not-a-query"}
    )
    assert response.status == 401


async def test_api_requires_token_in_settings(conn, schedule) -> None:
    """Без BOT_TOKEN в настройках — 500, а не 401.

    Это ошибка сервера: клиент прислал корректный запрос, но проверять
    подпись нечем.
    """

    class NoTokenSettings:
        bot_token = ""
        admin_ids: tuple[int, ...] = ()

    test_client = await make_client(conn, NoTokenSettings())
    try:
        response = await test_client.get("/api/schedule/today",
                                         headers=auth_headers())
        assert response.status == 500
        assert (await response.json())["error"] == "server_token_missing"
    finally:
        await test_client.close()


async def test_api_cors_preflight(client) -> None:
    """OPTIONS /api/... → 204 с разрешающими заголовками."""
    response = await client.options(
        "/api/deadlines",
        headers={"Origin": "http://localhost:3000",
                 "Access-Control-Request-Method": "POST"},
    )
    assert response.status == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "X-Telegram-Init-Data" in response.headers[
        "Access-Control-Allow-Headers"
    ]
    assert "POST" in response.headers["Access-Control-Allow-Methods"]


async def test_api_cors_headers_on_get(client) -> None:
    """Обычный GET тоже получает CORS-заголовки (иначе dev-фронт их не прочтёт)."""
    response = await client.get("/api/health")
    assert response.headers["Access-Control-Allow-Origin"] == "*"


# --- /api/health ---

async def test_api_health_ok(client) -> None:
    """GET /api/health → 200 {'status': 'ok'} без авторизации."""
    response = await client.get("/api/health")
    assert response.status == 200
    payload = await response.json()
    assert payload["status"] == "ok"
    assert "checked_at" in payload
# --- расписание ---

async def test_api_schedule_today_schema(client) -> None:
    """Схема ответа /api/schedule/today: день и поля пары."""
    response = await client.get("/api/schedule/today", headers=auth_headers())
    assert response.status == 200
    payload = await response.json()

    assert payload["group"] == GROUP
    assert payload["date"] == TODAY.isoformat()
    assert payload["weekday"] == "Среда"
    assert payload["week_type"] == "нечет"  # 7 октября — нечётное число
    assert payload["is_today"] is True

    lessons = payload["lessons"]
    assert [lesson["para"] for lesson in lessons] == [1, 2]
    first = lessons[0]
    assert set(first) == {
        "para", "subject", "teacher", "room", "time", "status",
        "old_subject", "is_cancelled", "is_self_study",
    }
    assert first["subject"] == "ОД.07 Математика"
    assert first["teacher"] == "Соколова Е.В."
    assert first["room"] == "204"
    assert first["status"] == "planned"
    assert first["time"]  # звонки: «09:00–10:35»
    assert first["old_subject"] is None
    assert first["is_cancelled"] is False
    assert first["is_self_study"] is False


async def test_api_schedule_today_without_group_400(conn, schedule,
                                                    frozen_now) -> None:
    """Пользователь без группы → 400 {'error': 'group_not_set'}."""
    other_tg_id = 777
    await add_user(conn, other_tg_id, "")

    test_client = await make_client(conn)
    try:
        response = await test_client.get(
            "/api/schedule/today", headers=auth_headers(other_tg_id)
        )
        assert response.status == 400
        assert (await response.json())["error"] == "group_not_set"
    finally:
        await test_client.close()


async def test_api_schedule_day_ok(client) -> None:
    """GET /api/schedule/day?date=... отдаёт запрошенный день."""
    target = date(2026, 10, 14)  # среда следующей недели
    response = await client.get(
        f"/api/schedule/day?date={target.isoformat()}", headers=auth_headers()
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["date"] == target.isoformat()
    assert payload["weekday"] == "Среда"
    assert len(payload["lessons"]) == 2
    assert payload["is_today"] is False


async def test_api_schedule_day_bad_date_400(client) -> None:
    """Невалидная дата → 400 {'error': 'bad_date'}."""
    for raw in ("2026-13-45", "вчера", "01.10.2026", "2026-02-30"):
        response = await client.get(
            f"/api/schedule/day?date={raw}", headers=auth_headers()
        )
        assert response.status == 400, raw
        assert (await response.json())["error"] == "bad_date"


async def test_api_schedule_day_without_date_400(client) -> None:
    """Без параметра date → 400 (день не угадываем)."""
    response = await client.get("/api/schedule/day", headers=auth_headers())
    assert response.status == 400
    assert (await response.json())["error"] == "bad_date"


async def test_api_schedule_week_six_days(client) -> None:
    """GET /api/schedule/week → 6 дней от понедельника недели TODAY."""
    response = await client.get("/api/schedule/week", headers=auth_headers())
    assert response.status == 200
    payload = await response.json()

    assert payload["group"] == GROUP
    assert payload["week_start"] == WEEK_START.isoformat()

    days = payload["days"]
    assert len(days) == 6
    assert [day["weekday"] for day in days] == list(WEEKDAY_NAMES)
    assert [day["date"] for day in days] == [
        (WEEK_START + timedelta(days=offset)).isoformat()
        for offset in range(6)
    ]

    by_date = {day["date"]: day for day in days}
# --- дедлайны ---

async def test_api_deadlines_empty(client) -> None:
    """Нет дедлайнов → пустой список, а не 404."""
    response = await client.get("/api/deadlines", headers=auth_headers())
    assert response.status == 200
    assert await response.json() == {"items": []}


async def test_api_deadline_create_and_list(client) -> None:
    """POST создаёт дедлайн (201), GET возвращает его с days_left."""
    response = await client.post(
        "/api/deadlines",
        headers=auth_headers(),
        json={"subject": "Математика", "task": "Контрольная",
              "teacher": "Соколова Елена Викторовна", "date": "2026-10-10"},
    )
    assert response.status == 201
    payload = await response.json()
    assert payload["ok"] is True

    item = payload["item"]
    assert set(item) == {"id", "subject", "task", "teacher", "date",
                         "days_left"}
    assert item["subject"] == "Математика"
    assert item["task"] == "Контрольная"
    assert item["teacher"] == "Соколова Е.В."
    assert item["date"] == "2026-10-10"
    # days_left считается от реального «сегодня» (сервис дедлайнов живёт по
    # календарю, а не по замороженному времени API), поэтому сверяем формулу.
    expected_days = (date(2026, 10, 10) - date.today()).days
    assert item["days_left"] == expected_days

    listed = await (await client.get("/api/deadlines",
                                     headers=auth_headers())).json()
    assert [entry["id"] for entry in listed["items"]] == [item["id"]]


async def test_api_deadline_create_without_task_400(client) -> None:
    """Ни предмета, ни задачи → 400 {'error': 'empty_task'}."""
    response = await client.post(
        "/api/deadlines", headers=auth_headers(),
        json={"subject": "  ", "task": "", "date": "2026-10-10"},
    )
    assert response.status == 400
    assert (await response.json())["error"] == "empty_task"


async def test_api_deadline_create_bad_date_400(client) -> None:
    """Мусор в дате → 400 {'error': 'bad_date'}."""
    response = await client.post(
        "/api/deadlines", headers=auth_headers(),
        json={"task": "Реферат", "date": "5 октября"},
    )
    assert response.status == 400
    assert (await response.json())["error"] == "bad_date"


async def test_api_deadline_create_bad_body_400(client) -> None:
    """Не-JSON в теле → 400 {'error': 'bad_body'}."""
    response = await client.post(
        "/api/deadlines", headers={**auth_headers(),
                                   "Content-Type": "application/json"},
        data="{not json",
    )
    assert response.status == 400
    assert (await response.json())["error"] == "bad_body"


async def test_api_deadline_create_without_date_ok(client) -> None:
    """Без даты дедлайн создаётся: срок пустой, days_left = None."""
    response = await client.post(
        "/api/deadlines", headers=auth_headers(),
        json={"task": "Прочитать главу"},
    )
    assert response.status == 201
    item = (await response.json())["item"]
    assert item["date"] == ""
    assert item["days_left"] is None


async def test_api_deadline_delete_ok(client, conn) -> None:
    """DELETE своего дедлайна → 200, и он исчезает из списка."""
    deadline_id = deadline_service.add(conn, TG_ID, "История", "", "Реферат",
                                       "2026-10-10")

    response = await client.delete(
        f"/api/deadlines/{deadline_id}", headers=auth_headers()
    )
    assert response.status == 200
    assert (await response.json())["ok"] is True
    assert deadline_service.get(conn, deadline_id, TG_ID) is None
    assert (await (await client.get("/api/deadlines",
                                    headers=auth_headers())).json())["items"] == []


async def test_api_deadline_delete_foreign_404(client, conn) -> None:
    """Чужой дедлайн удалить нельзя: 404 (владелец проверяется)."""
    other_tg_id = 888
    # На ``deadlines.tg_id`` есть внешний ключ на ``users``, поэтому чужой
    # пользователь должен существовать.
    await add_user(conn, other_tg_id, GROUP, "Чужой Студент")
    deadline_id = deadline_service.add(conn, other_tg_id, "История", "",
                                       "Чужой реферат", "2026-10-10")

    response = await client.delete(
        f"/api/deadlines/{deadline_id}", headers=auth_headers()
    )
    assert response.status == 404
    assert (await response.json())["error"] == "not_found"
    # Чужая запись осталась на месте.
    assert deadline_service.get(conn, deadline_id, other_tg_id) is not None


async def test_api_deadline_delete_unknown_404(client) -> None:
    """Несуществующий id → 404, а не 500."""
    response = await client.delete("/api/deadlines/999999",
                                   headers=auth_headers())
    assert response.status == 404
    assert (await response.json())["error"] == "not_found"


# --- профиль ---

async def test_api_profile(client) -> None:
    """GET /api/profile → имя и группа из students, роль из students.role."""
    response = await client.get("/api/profile", headers=auth_headers())
    assert response.status == 200
    payload = await response.json()
    assert payload["tg_id"] == TG_ID
    assert payload["name"] == "Петров И. А."
    assert payload["group"] == GROUP
    assert payload["role"] == ROLE_STAROSTA


async def test_api_profile_admin_wins_over_group_role(conn, schedule,
                                                      frozen_now) -> None:
    """ADMIN_IDS перекрывает роль в группе: староста видится админом."""

    class AdminSettings(FakeSettings):
        admin_ids = (TG_ID,)

    test_client = await make_client(conn, AdminSettings())
    try:
        payload = await (await test_client.get(
            "/api/profile", headers=auth_headers()
        )).json()
        assert payload["role"] == "admin"
    finally:
        await test_client.close()


# --- посещаемость ---

async def test_api_attendance_zero_without_marks(client) -> None:
    """Студент без отметок: нули и предметы группы из расписания."""
    response = await client.get("/api/attendance", headers=auth_headers())
    assert response.status == 200
    payload = await response.json()

    assert payload["present"] == 0
    assert payload["late"] == 0
    assert payload["absent"] == 0
    assert payload["excused"] == 0
    assert payload["total"] == 0
    assert payload["percent"] == 0
    assert payload["subjects"], "предметы группы должны быть в ответе"
    for item in payload["subjects"]:
        assert set(item) == {"name", "attended", "required", "total_lessons",
                             "is_attested", "need_more"}
        assert item["attended"] == 0
        assert item["total_lessons"] == 0
        # required — норматив аттестации, а не число прошедших пар.
        assert item["required"] == MIN_ATTESTATION_LESSONS
        assert item["need_more"] == MIN_ATTESTATION_LESSONS
        assert item["is_attested"] is False


async def test_api_attendance_counts_and_percent(client, conn) -> None:
    """Отметки учитываются: два «present» дают 100% и аттестацию по 1 паре."""
    date_iso = TODAY.isoformat()
    att.mark_attendance(conn, GROUP, date_iso, 1, TG_ID, "Петров И. А.",
                        status="present", marked_by=TG_ID, method="self",
                        subject="ОД.07 Математика")
    att.mark_attendance(conn, GROUP, date_iso, 2, TG_ID, "Петров И. А.",
                        status="present", marked_by=TG_ID, method="self",
                        subject="ОД.12 Химия")

    payload = await (await client.get(
        "/api/attendance", headers=auth_headers()
    )).json()
    assert payload["present"] == 2
    assert payload["total"] == 2
    assert payload["percent"] == 100

    math = next(item for item in payload["subjects"]
                if item["name"] == "ОД.07 Математика")
    assert math["attended"] == 1
    assert math["total_lessons"] == 1
    # Норматив (3) не путается с числом прошедших пар (1).
    assert math["required"] == MIN_ATTESTATION_LESSONS
    assert math["need_more"] == MIN_ATTESTATION_LESSONS - 1
    assert math["is_attested"] is False


async def test_api_attendance_attested_after_three_lessons(client,
                                                           conn) -> None:
    """3 посещённые пары по предмету → аттестован, need_more = 0."""
    # Три разные пары внутри периода аттестации (1–7 октября, замороженные
    # часы API): 1, 5 и 7 октября.
    for day, para in ((date(2026, 10, 1), 1), (date(2026, 10, 5), 1),
                      (date(2026, 10, 7), 1)):
        att.mark_attendance(conn, GROUP, day.isoformat(), para, TG_ID,
                            "Петров И. А.", status=STATUS_PRESENT,
                            marked_by=TG_ID, method="self",
                            subject="ОД.07 Математика")

    payload = await (await client.get(
        "/api/attendance", headers=auth_headers()
    )).json()
    math = next(item for item in payload["subjects"]
                if item["name"] == "ОД.07 Математика")
    assert math["attended"] == MIN_ATTESTATION_LESSONS
    assert math["required"] == MIN_ATTESTATION_LESSONS
    assert math["total_lessons"] == MIN_ATTESTATION_LESSONS
    assert math["is_attested"] is True
    assert math["need_more"] == 0


async def test_api_attendance_absent_lowers_percent(client, conn) -> None:
    """Пропуск не зачитывается: 1 «был» + 1 «не был» → 50%."""
    date_iso = TODAY.isoformat()
    att.mark_attendance(conn, GROUP, date_iso, 1, TG_ID, "Петров И. А.",
                        status=STATUS_PRESENT, marked_by=TG_ID, method="self",
                        subject="ОД.07 Математика")
    att.mark_attendance(conn, GROUP, date_iso, 2, TG_ID, "Петров И. А.",
                        status="absent", marked_by=TG_ID, method="self",
                        subject="ОД.12 Химия")

    payload = await (await client.get(
        "/api/attendance", headers=auth_headers()
    )).json()
    assert payload["present"] == 1
    assert payload["absent"] == 1
    assert payload["total"] == 2
    assert payload["percent"] == 50


async def test_api_attendance_not_in_group_400(conn, schedule,
                                               frozen_now) -> None:
    """Пользователь не в группе посещаемости → 400 not_in_group."""
    other_tg_id = 999
    await add_user(conn, other_tg_id, GROUP)

    test_client = await make_client(conn)
    try:
        response = await test_client.get(
            "/api/attendance", headers=auth_headers(other_tg_id)
        )
        assert response.status == 400
        assert (await response.json())["error"] == "not_in_group"
    finally:
        await test_client.close()


# --- активная пара ---

async def test_api_active_lesson_payload(client) -> None:
    """Внутри пары отдаётся предмет, кабинет, время и открытое окно."""
    response = await client.get("/api/attendance/active",
                                headers=auth_headers())
    assert response.status == 200
    payload = await response.json()

    active = payload["active"]
    assert active is not None
    assert active["para"] == 1
    assert active["subject"] == "ОД.07 Математика"
    assert active["room"] == "204"
    assert active["time_range"]
    # 1 пара идёт с 09:00 по будням, окно ответа — 5 минут.
    assert active["closes_at"].startswith("2026-10-07T09:05")
    assert active["answered"] is None
    assert active["is_open"] is True


async def test_api_active_none_without_lesson(conn, schedule,
                                              monkeypatch) -> None:
    """Вне звонков пары нет → {'active': None}."""
    moment = datetime(2026, 10, 7, 20, 0, tzinfo=KRASNOYARSK)
    test_client = await make_client(conn)
    try:
        monkeypatch.setattr(api_routes, "_now", lambda: moment)
        payload = await (await test_client.get(
            "/api/attendance/active", headers=auth_headers()
        )).json()
        assert payload == {"active": None}
    finally:
        await test_client.close()


async def test_api_active_none_when_group_has_no_lesson(conn, schedule,
                                                       monkeypatch) -> None:
    """Пара по звонкам идёт, но у группы её нет → {'active': None}."""
    # Понедельник, 12:55 — 3 пара по звонкам, но расписание только на среду.
    moment = datetime(2026, 10, 5, 12, 55, tzinfo=KRASNOYARSK)
    test_client = await make_client(conn)
    try:
        monkeypatch.setattr(api_routes, "_now", lambda: moment)
        payload = await (await test_client.get(
            "/api/attendance/active", headers=auth_headers()
        )).json()
        assert payload == {"active": None}
    finally:
        await test_client.close()
async def test_api_answer_yes_marks_present(client, conn) -> None:
    """POST /api/attendance/answer «yes» → present «на паре» (200)."""
    response = await client.post(
        "/api/attendance/answer", headers=auth_headers(),
        json={"answer": "yes"},
    )
    assert response.status == 200
    assert await response.json() == {"ok": True, "status": STATUS_PRESENT}

    row = att.get_attendance(conn, GROUP, TODAY.isoformat(), 1, TG_ID)
    assert row is not None
    assert row["status"] == STATUS_PRESENT
    assert row["method"] == "self"
    assert row["marked_by"] == TG_ID
    assert row["full_name"] == "Петров И. А."
    # Предмет сохраняется вместе с отметкой — для сводки по предметам.
    assert row["subject"] == "ОД.07 Математика"


async def test_api_answer_no_marks_absent(client, conn) -> None:
    """Ответ «no» пишет absent: «меня нет» — полноценный ответ."""
    response = await client.post(
        "/api/attendance/answer", headers=auth_headers(),
        json={"answer": "no"},
    )
    assert response.status == 200
    assert (await response.json())["status"] == "absent"

    row = att.get_attendance(conn, GROUP, TODAY.isoformat(), 1, TG_ID)
    assert row is not None and row["status"] == "absent"


async def test_api_answer_twice_409(client, conn) -> None:
    """Повторный ответ на ту же пару → 409 already_answered."""
    first = await client.post("/api/attendance/answer", headers=auth_headers(),
                              json={"answer": "yes"})
    assert first.status == 200

    second = await client.post("/api/attendance/answer", headers=auth_headers(),
                               json={"answer": "no"})
    assert second.status == 409
    assert (await second.json())["error"] == "already_answered"
    # Статус не перезаписан.
    row = att.get_attendance(conn, GROUP, TODAY.isoformat(), 1, TG_ID)
    assert row["status"] == STATUS_PRESENT


async def test_api_answer_bad_value_400(client) -> None:
    """Ответ, отличный от yes/no, → 400 bad_answer."""
    for value in ("maybe", "", None, 1):
        response = await client.post(
            "/api/attendance/answer", headers=auth_headers(),
            json={"answer": value},
        )
        assert response.status == 400, value
        assert (await response.json())["error"] == "bad_answer"


async def test_api_answer_without_active_lesson_400(conn, schedule,
                                                    monkeypatch) -> None:
    """Вне пары отвечать не на что → 400 no_active_lesson."""
    moment = datetime(2026, 10, 7, 20, 0, tzinfo=KRASNOYARSK)
    test_client = await make_client(conn)
    try:
        monkeypatch.setattr(api_routes, "_now", lambda: moment)
        response = await test_client.post(
            "/api/attendance/answer", headers=auth_headers(),
            json={"answer": "yes"},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "no_active_lesson"
    finally:
        await test_client.close()


async def test_api_answer_after_window_400(conn, schedule,
                                           monkeypatch) -> None:
    """После 5 минут окно закрыто: ответ не принимается (poll_closed)."""
    # 1 пара: 09:00–10:35, окно ответа закрывается в 09:05.
    moment = datetime(2026, 10, 7, 9, 30, tzinfo=KRASNOYARSK)
    test_client = await make_client(conn)
    try:
        monkeypatch.setattr(api_routes, "_now", lambda: moment)
        response = await test_client.post(
            "/api/attendance/answer", headers=auth_headers(),
            json={"answer": "yes"},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "poll_closed"
        assert att.get_attendance(conn, GROUP, TODAY.isoformat(), 1,
                                  TG_ID) is None
    finally:
        await test_client.close()


# --- раздача статики /app/ ---

def webapp_built() -> bool:
    """Собран ли фронт (нужен ``pnpm build`` в webapp/)."""
    from bot.web import WEBAPP_DIR

    return (WEBAPP_DIR / "index.html").is_file()


async def test_api_app_index(client) -> None:
    """GET /app/ отдаёт собранный index.html."""
    if not webapp_built():
        pytest.skip("webapp/out не собран — нужен pnpm build")

    response = await client.get("/app/")
    assert response.status == 200
    assert "html" in response.headers["Content-Type"]
    body = await response.text()
    assert "<html" in body
    # Скрипт Telegram подключается в layout — без него WebApp не работает.
    assert "telegram-web-app.js" in body


async def test_api_app_bare_path_opens_spa(client) -> None:
    """/app без слэша тоже открывает SPA, а не отдаёт 403 от каталога."""
    if not webapp_built():
        pytest.skip("webapp/out не собран — нужен pnpm build")

    response = await client.get("/app")
    assert response.status == 200
    assert "html" in response.headers["Content-Type"]


async def test_api_app_icon(client) -> None:
    """Иконка из metadata отдаётся по /app/icon.svg (путь с basePath)."""
    if not webapp_built():
        pytest.skip("webapp/out не собран — нужен pnpm build")

    response = await client.get("/app/icon.svg")
    assert response.status == 200
    assert "svg" in response.headers["Content-Type"]


async def test_api_app_next_asset(client) -> None:
    """Ассеты Next отдаются по /app/_next/... с корректным типом."""
    if not webapp_built():
        pytest.skip("webapp/out не собран — нужен pnpm build")

    from bot.web import WEBAPP_DIR

    html = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    asset = re.search(r"/app/_next/static/[^\"']+\.js", html)
    assert asset is not None, "в index.html нет ссылок на _next/static"

    response = await client.get(asset.group(0))
    assert response.status == 200
    assert "javascript" in response.headers["Content-Type"]


async def test_api_app_missing_file_404(client) -> None:
    """Несуществующий файл под /app/ → 404, а не index.html вслепую."""
    if not webapp_built():
        pytest.skip("webapp/out не собран — нужен pnpm build")

    response = await client.get("/app/no-such-file.js")
    assert response.status == 404


async def test_api_static_endpoints_alive(client) -> None:
    """Существующие эндпоинты не сломаны: /health и /calendar отвечают."""
    assert (await client.get("/health")).status == 200
    # Неизвестный токен → 404 с текстом (как было до Mini App).
    response = await client.get("/calendar/nope.ics")
    assert response.status == 404


async def test_api_answer_without_student_400(conn, schedule,
                                              monkeypatch) -> None:
    """Пользователь не в группе посещаемости → 400 not_in_group."""
    other_tg_id = 4242
    await add_user(conn, other_tg_id, GROUP)

    test_client = await make_client(conn)
    try:
        response = await test_client.post(
            "/api/attendance/answer", headers=auth_headers(other_tg_id),
            json={"answer": "yes"},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "not_in_group"
    finally:
        await test_client.close()
