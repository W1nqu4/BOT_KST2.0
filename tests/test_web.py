"""Тесты aiohttp-сервера: /health и /calendar/{token}.ics (шаг 9).

Используется ``aiohttp.test_utils``: поднимается настоящий сервер на
localhost, поэтому проверяются и маршрутизация, и заголовки ответа.
"""

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.db import get_connection, transaction
from bot.migrations import MIGRATIONS, apply_migrations
from bot.services import cache_service, deadline_service as dl
from bot.services import ics_service
from bot.web import CONN_KEY, SETTINGS_KEY, create_app

GROUP = "26КАД"


@pytest.fixture()
def conn(tmp_path: Path):
    """Соединение к БД с миграциями и зарегистрированным пользователем."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (1, ?, '2026-09-01T00:00:00+07:00')", (GROUP,),
        )
    yield c
    c.close()


@pytest.fixture()
async def client(conn, parsed_schedule):
    """Тестовый HTTP-клиент aiohttp.

    ``TestServer`` работает в том же event loop, что и тест, поэтому серверу
    передаётся то же соединение — как в приложении.
    """
    cache_service.save_schedule(conn, parsed_schedule)
    server = TestServer(create_app(conn))
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


# --- /health ---

async def test_health_ok(client) -> None:
    """GET /health → 200 и JSON с ожидаемыми ключами."""
    response = await client.get("/health")
    assert response.status == 200
    payload = await response.json()

    assert payload["status"] == "ok"
    assert payload["db"] == "ok"
    for key in ("schema_version", "users_count", "last_schedule_update",
                "lessons_cached", "checked_at"):
        assert key in payload, f"нет ключа {key}"
    assert payload["schema_version"] == max(MIGRATIONS)
    assert payload["users_count"] == 1
    assert payload["lessons_cached"] > 1000


async def test_health_reports_substitution_history(client, conn,
                                                   parsed_schedule) -> None:
    """Задача B: /health отдаёт статистику истории замен."""
    payload = await (await client.get("/health")).json()
    assert "substitution_history_rows" in payload
    assert "substitution_history_earliest" in payload
    # История пуста — значит 0 и None.
    assert payload["substitution_history_rows"] == 0
    assert payload["substitution_history_earliest"] is None

    from bot import db

    db.save_substitution_history(conn, GROUP, "2026-09-28", [{
        "para": 2, "old_subject": "A", "new_subject": "B",
        "teacher": "T", "room": "1", "is_cancelled": False,
        "is_self_study": False,
    }])

    payload = await (await client.get("/health")).json()
    assert payload["substitution_history_rows"] == 1
    assert payload["substitution_history_earliest"] == "2026-09-28"
    assert payload["substitution_history_dates_count"] == 1
    assert payload["substitution_history_latest"] == "2026-09-28"


async def test_health_reports_groups_with_schedule(client,
                                                   parsed_schedule) -> None:
    """Диагностика: /health отдаёт список групп из расписания.

    Нужна, чтобы видеть, что обе параллельные группы (26КАД и 026КАД) на
    месте и не склеились в одну.
    """
    payload = await (await client.get("/health")).json()

    assert "groups_with_schedule" in payload
    groups = payload["groups_with_schedule"]
    assert isinstance(groups, list)
    assert groups, "в БД есть группы расписания"
    assert len(groups) == len(set(groups)), "группы должны быть уникальны"
    assert groups == sorted(groups), "список отсортирован"


async def test_health_content_type_json(client) -> None:
    response = await client.get("/health")
    assert "json" in response.headers["Content-Type"]


async def test_health_reports_public_base_url(conn, parsed_schedule) -> None:
    """Задача 2: /health отдаёт public_base_url из настроек (для диагностики).

    Локально ``settings`` может отсутствовать — тогда поле есть, но пустое,
    поэтому проверяются оба случая.
    """
    cache_service.save_schedule(conn, parsed_schedule)

    class _Settings:
        public_base_url = "https://kst24-kst24.up.railway.app"

    server = TestServer(create_app(conn, _Settings()))
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        payload = await (await test_client.get("/health")).json()
        assert payload["public_base_url"] == "https://kst24-kst24.up.railway.app"
    finally:
        await test_client.close()


async def test_health_public_base_url_empty_without_settings(client) -> None:
    """Без настроек поле присутствует и пустое (не падает)."""
    payload = await (await client.get("/health")).json()
    assert payload["public_base_url"] == ""


# --- /calendar/{token}.ics ---

async def test_calendar_ok(client, conn) -> None:
    """Валидный токен → 200, text/calendar и корректные заголовки."""
    token = ics_service.get_or_create_token(conn, 1)

    response = await client.get(f"/calendar/{token}.ics")
    assert response.status == 200
    assert "text/calendar" in response.headers["Content-Type"]
    assert response.headers["Cache-Control"] == "no-cache, must-revalidate"
    assert "X-PUBLISHED-TTL" in response.headers
    assert "schedule.ics" in response.headers["Content-Disposition"]

    body = await response.text()
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert "BEGIN:VEVENT" in body


async def test_calendar_invalid_token_404(client) -> None:
    response = await client.get("/calendar/deadbeef.ics")
    assert response.status == 404
    assert "не найдена" in await response.text()


async def test_calendar_empty_token_404(client) -> None:
    """Пустой токен (маршрут не совпадёт) → 404."""
    response = await client.get("/calendar/.ics")
    assert response.status == 404


async def test_calendar_user_without_group_404(client, conn) -> None:
    """Пользователь без группы получает 404 с подсказкой."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (2, '', 'x')"
        )
        conn.execute(
            "INSERT INTO calendar_tokens (tg_id, token, created_at)"
            " VALUES (2, 'nogrouptoken', 'x')"
        )

    response = await client.get("/calendar/nogrouptoken.ics")
    assert response.status == 404
    assert "Укажите группу" in await response.text()


async def test_calendar_includes_deadlines(client, conn) -> None:
    """Дедлайны пользователя попадают в его подписку."""
    token = ics_service.get_or_create_token(conn, 1)
    dl.add(conn, 1, "Химия", "", "Сдать курсовую", "2026-10-05")

    response = await client.get(f"/calendar/{token}.ics")
    body = await response.text()
    assert "Сдать курсовую" in body


async def test_calendar_no_rrule(client, conn) -> None:
    """В подписке нет RRULE: чёт/нечет по числу месяца."""
    token = ics_service.get_or_create_token(conn, 1)

    response = await client.get(f"/calendar/{token}.ics")
    assert "RRULE" not in await response.text()