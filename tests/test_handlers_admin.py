"""Тесты админки (шаг 11): /stats, /broadcast, /reparse, /users, /deactivate.

Bot подменяется заглушкой. Проверяется и доступ (только админы), и то, что
посторонним админ-команды не подтверждают своего существования.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.db import get_connection, transaction
from bot.handlers import admin as adm
from bot.keyboards import reply as reply_kb
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from bot.services import cache_service
from tests.test_handlers_dispatch import FakeBot

ADMIN_ID = 1001
USER_ID = 2002
GROUP = "26КАД"


def _settings() -> Settings:
    return Settings(
        bot_token="123:TEST", public_base_url="https://bot.example",
        port=8080, db_path="data/test.db", admin_ids=(ADMIN_ID,),
        admin_chat_id=ADMIN_ID, cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и двумя пользователями."""
    c = get_connection(tmp_path / "admin.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, full_name, created_at)"
            " VALUES (?, ?, 'Иван', '2026-09-01T00:00:00+07:00')",
            (USER_ID, GROUP),
        )
    yield c
    c.close()


@pytest.fixture(scope="module")
def shared_dp():
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed, settings=_settings())


@pytest.fixture()
def dp(shared_dp, conn):
    shared_dp.workflow_data["conn"] = conn
    shared_dp.workflow_data["settings"] = _settings()
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


# --- доступ ---

def test_is_admin_filter_accepts_admin() -> None:
    """Фильтр пропускает админа и отсекает постороннего."""
    import asyncio as _asyncio

    flt = adm.IsAdmin()
    admin_msg = _update("/stats", ADMIN_ID).message
    user_msg = _update("/stats", USER_ID).message

    assert _asyncio.run(flt(admin_msg, _settings())) is True
    assert _asyncio.run(flt(user_msg, _settings())) is False


def test_is_admin_filter_handles_no_settings() -> None:
    import asyncio as _asyncio

    assert _asyncio.run(adm.IsAdmin()(_update("/stats", ADMIN_ID).message, None)) is False


async def test_stats_for_admin(dp, conn) -> None:
    """/stats от админа: отчёт со всеми ключевыми полями."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/stats", ADMIN_ID))

    texts = _texts(bot)
    assert len(texts) == 1
    body = texts[0]
    assert "📊 <b>Статистика</b>" in body
    assert "Всего:" in body
    assert "активных за 7 дней:" in body
    assert "По группам:" in body
    assert GROUP in body
    assert "Размер БД:" in body
    assert "Расписание: обновлено" in body
    assert "Замены: обновлено" in body


async def test_stats_for_non_admin_is_neutral(dp, conn) -> None:
    """От не-админа — нейтральный ответ, без намёка на существование команды."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/stats", USER_ID))

    texts = _texts(bot)
    assert len(texts) == 1
    assert "Команда не найдена" in texts[0]
    assert "Статистика" not in texts[0]
    assert "доступ" not in texts[0].lower()


async def test_all_admin_commands_neutral_for_non_admin(dp, conn) -> None:
    """Ни одна админ-команда не выдаёт себя постороннему."""
    bot = FakeBot()
    for command in ("/stats", "/reparse", "/broadcast", "/users 26КАД",
                    "/deactivate 1"):
        bot.sent.clear()
        await dp.feed_update(bot, _update(command, USER_ID))
        texts = _texts(bot)
        assert any("Команда не найдена" in t for t in texts), command


# --- /stats: содержимое ---

def test_build_stats_text_lists_groups(conn) -> None:
    """Топ групп попадает в отчёт."""
    with transaction(conn):
        for tg_id in (3001, 3002):
            conn.execute(
                "INSERT INTO users (tg_id, group_name, created_at)"
                " VALUES (?, ?, '2026-09-01T00:00:00+07:00')", (tg_id, GROUP),
            )
    body = adm.build_stats_text(conn, "data/test.db")
    assert f"{GROUP} — 3" in body


def test_build_stats_text_missing_db_file(conn) -> None:
    """Отсутствие файла БД не ломает отчёт."""
    body = adm.build_stats_text(conn, "нет/такого/файла.db")
    assert "нет файла" in body


def test_count_active_users_since(conn) -> None:
    """Учёт глубины: пользователь 8 дней назад в недельное окно не попадает."""
    from datetime import datetime as _dt

    from bot import db as _db

    # Пользователь из фикстуры создан 01.09; база отсчёта — 02.09.
    recent = _db.count_active_users_since(
        conn, days=7, now="2026-09-02T12:00:00+07:00"
    )
    assert recent == 1, "вчерашний пользователь попадает в окно"

    # База отсчёта через 8 дней — уже вне окна.
    later = _db.count_active_users_since(
        conn, days=7, now="2026-09-09T12:00:00+07:00"
    )
    assert later == 0, "за 7 дней новых регистраций не было"


# --- /users ---

async def test_users_command_lists_users(dp, conn) -> None:
    """/users 26КАД показывает tg_id и имя."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26КАД", ADMIN_ID))

    body = _texts(bot)[0]
    assert GROUP in body
    assert str(USER_ID) in body
    assert "Иван" in body
    assert "Отключить" in body


async def test_users_command_normalizes_group(dp, conn) -> None:
    """«26 кад» и «26КАД» дают один результат."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26 кад", ADMIN_ID))
    assert str(USER_ID) in _texts(bot)[0]


async def test_users_command_empty_group(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 99XXX", ADMIN_ID))
    assert "нет активных пользователей" in _texts(bot)[0]


async def test_users_command_without_args(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users", ADMIN_ID))
    assert "Использование" in _texts(bot)[0]


async def test_users_command_truncates_after_limit(dp, conn) -> None:
    """Больше 50 пользователей → «и ещё N»."""
    with transaction(conn):
        for tg_id in range(3000, 3060):
            conn.execute(
                "INSERT INTO users (tg_id, group_name, full_name, created_at)"
                " VALUES (?, ?, 'Имя', '2026-09-01T00:00:00+07:00')",
                (tg_id, GROUP),
            )
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26КАД", ADMIN_ID))

    body = _texts(bot)[0]
    assert "и ещё" in body
    assert "11" in body, "60 - 50 показанных = 10 новых + 1 исходный"
    assert len(body) < 4096
# --- /deactivate ---

async def test_deactivate_marks_user_inactive(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update(f"/deactivate {USER_ID}", ADMIN_ID))

    assert "отключён" in _texts(bot)[0]
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 0


async def test_deactivate_unknown_user(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/deactivate 999999", ADMIN_ID))
    assert "не найден" in _texts(bot)[0]


async def test_deactivate_without_args(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/deactivate", ADMIN_ID))
    assert "Использование" in _texts(bot)[0]


# --- /broadcast ---

async def test_broadcast_flow_sends_and_reports(dp, conn) -> None:
    """/broadcast → текст → предпросмотр → подтверждение → отчёт."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (3003, '26КАД', '2026-09-01T00:00:00+07:00')"
        )
    bot = FakeBot()

    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    assert any("Пришли текст сообщения" in t for t in _texts(bot))

    bot.sent.clear()
    await dp.feed_update(bot, _update("Скоро сессия!", ADMIN_ID))
    preview = _texts(bot)[0]
    assert "Предпросмотр рассылки" in preview
    assert "Получателей: <b>2</b>" in preview
    assert "Скоро сессия!" in preview

    bot.sent.clear()
    await dp.feed_update(bot, _callback("bc:send", ADMIN_ID))

    # Двум пользователям ушло сообщение + отчёт админу.
    texts = _texts(bot)
    assert sum("Скоро сессия!" in t for t in texts) >= 1
    report = next(t for t in texts if "Рассылка завершена" in t)
    assert "Всего получателей: <b>2</b>" in report
    assert "Отправлено: <b>2</b>" in report
    assert "Ошибок: <b>0</b>" in report


async def test_broadcast_cancel_does_not_send(dp, conn) -> None:
    """Отмена на предпросмотре: ничего не отправлено."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    await dp.feed_update(bot, _update("Текст", ADMIN_ID))

    bot.sent.clear()
    await dp.feed_update(bot, _callback("bc:cancel", ADMIN_ID))

    assert any("отменена" in t for t in _texts(bot))
    assert not any("Текст" in t for t in _texts(bot))


async def test_broadcast_cancel_by_text_button(dp, conn) -> None:
    """Отмена текстовой кнопкой на шаге ввода."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    bot.sent.clear()
    await dp.feed_update(bot, _update(reply_kb.BTN_CANCEL, ADMIN_ID))
    assert any("отменена" in t for t in _texts(bot))


async def test_broadcast_deactivates_blocked(conn) -> None:
    """TelegramForbiddenError в рассылке → is_active = 0, отчёт считает."""
    from aiogram.exceptions import TelegramForbiddenError

    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (3004, '26КАД', '2026-09-01T00:00:00+07:00')"
        )

    class PartlyBlockedBot(FakeBot):
        """Блокирует только одного пользователя."""

        async def __call__(self, method, request_timeout=None):
            if getattr(method, "chat_id", None) == 3004:
                raise TelegramForbiddenError(method=method, message="blocked")
            return await super().__call__(method, request_timeout)

    report = await adm.run_broadcast(conn, PartlyBlockedBot(), "Привет",
                                     throttle=False)

    assert report["total"] == 2
    assert report["sent"] == 1
    assert report["blocked"] == 1
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = 3004"
    ).fetchone()["is_active"]
    assert active == 0


async def test_broadcast_counts_errors(conn) -> None:
    """Прочие ошибки считаются как failed, рассылка не срывается."""
    class FailingBot(FakeBot):
        async def __call__(self, method, request_timeout=None):
            raise RuntimeError("сеть")

    report = await adm.run_broadcast(conn, FailingBot(), "Привет",
                                     throttle=False)
    assert report["failed"] == report["total"]
    assert report["sent"] == 0
    # Ошибка отправки НЕ должна деактивировать пользователя.
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 1


# --- /reparse ---

async def test_reparse_reports_counts(dp, conn, monkeypatch) -> None:
    """/reparse вызывает cache_service и сообщает числа."""
    async def fake_schedule(connection, session=None, directory=None):
        return 1456

    async def fake_subs(connection, session=None, directory=None):
        return 78

    monkeypatch.setattr(cache_service, "refresh_schedule", fake_schedule)
    monkeypatch.setattr(cache_service, "refresh_substitutions", fake_subs)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    texts = _texts(bot)
    assert any("Обновляю источники" in t for t in texts)
    report = next(t for t in texts if "Обновление завершено" in t)
    assert "1456" in report
    assert "78" in report


async def test_reparse_reports_errors(dp, conn, monkeypatch) -> None:
    """Ошибка обновления отражается в отчёте, а не падением."""
    async def failing(connection, session=None, directory=None):
        return -1

    monkeypatch.setattr(cache_service, "refresh_schedule", failing)
    monkeypatch.setattr(cache_service, "refresh_substitutions", failing)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    report = next(t for t in _texts(bot) if "Обновление завершено" in t)
    assert report.count("ошибка") == 2
    return [m["text"] for m in bot.sent if m["text"]]