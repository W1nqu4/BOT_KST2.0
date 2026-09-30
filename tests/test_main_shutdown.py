"""Тесты graceful shutdown и мониторинга (шаг 12).

Проверяется, что при сигнале остановки задачи отменяются, HTTP-сервер
гасится, а polling не перезапускается.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.main import (
    POLLING_RESTART_DELAY,
    RateLimitMiddleware,
    Settings,
    install_signal_handlers,
    run_bot,
    run_polling_with_restart,
    start_web_server,
)
from bot.migrations import apply_migrations
from bot.utils import monitoring as mon
from bot.utils.security import RateLimiter


def _settings(port: int = 0, **over) -> Settings:
    """Настройки для тестов; port=0 — свободный порт от ОС."""
    base = {
        "bot_token": "123456:TEST",
        "public_base_url": "https://bot.example",
        "port": port,
        "db_path": "data/test.db",
        "admin_ids": (),
        "admin_chat_id": None,
        "cache_dir": "data/cache",
        "log_level": "INFO",
    }
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def conn(tmp_path: Path):
    c = get_connection(tmp_path / "shutdown.db")
    apply_migrations(c)
    yield c
    c.close()


class FakeObserver:
    """Заглушка наблюдателя диспетчера: принимает middleware."""

    def __init__(self) -> None:
        self.middlewares: list = []

    def outer_middleware(self, middleware) -> None:
        self.middlewares.append(middleware)


class FakeDispatcher:
    """Заглушка диспетчера для тестов запуска и остановки."""

    def __init__(self, sleep_forever: bool = True) -> None:
        self.message = FakeObserver()
        self.callback_query = FakeObserver()
        self.sleep_forever = sleep_forever

    async def start_polling(self, bot):
        if self.sleep_forever:
            await asyncio.sleep(3600)


def _fake_dispatcher(sleep_forever: bool = True):
    """Фабрика заглушки диспетчера для monkeypatch."""
    return lambda *a, **k: FakeDispatcher(sleep_forever)


class RecordingBot:
    """Мини-бот: помнит отправленные сообщения и факт закрытия сессии."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.commands: list = []
        self.closed = False
        self.menu_button = "НЕ ВЫЗЫВАЛОСЬ"
        self.menu_calls: list = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))
        return True

    async def set_my_commands(self, commands, **kwargs):
        self.commands = list(commands)
        return True

    async def set_chat_menu_button(self, chat_id=None, menu_button=None,
                                   request_timeout=None):
        """Заглушка сброса кнопки меню: запоминаем переданный объект."""
        self.menu_button = menu_button
        self.menu_calls.append(menu_button)
        return True

    class _Session:
        def __init__(self, owner) -> None:
            self.owner = owner

        async def close(self) -> None:
            self.owner.closed = True

    @property
    def session(self):
        return self._Session(self)


async def _async_value(value):
    """Вернуть значение из корутины (для monkeypatch)."""
    return value


# --- polling с автоперезапуском ---

async def test_polling_restarts_after_error() -> None:
    """Сетевая ошибка не роняет процесс: polling пробуется снова."""
    attempts: list[int] = []

    class FlakyDispatcher:
        async def start_polling(self, bot):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("сеть упала")
            return None

    await run_polling_with_restart(
        FlakyDispatcher(), RecordingBot(), asyncio.Event()
    )
    assert len(attempts) == 2, "должен быть повторный запуск"


async def test_polling_stops_on_shutdown_during_delay() -> None:
    """Если во время паузы пришёл сигнал — перезапуска нет."""
    attempts: list[int] = []
    event = asyncio.Event()

    class CrashingDispatcher:
        async def start_polling(self, bot):
            attempts.append(1)
            event.set()          # сигнал остановки во время паузы
            raise RuntimeError("сеть")

    await run_polling_with_restart(CrashingDispatcher(), RecordingBot(), event)
    assert len(attempts) == 1, "после сигнала перезапуска быть не должно"


async def test_polling_propagates_cancellation() -> None:
    """Отмена задачи пролетает наружу, а не считается сбоем сети."""

    class CancellingDispatcher:
        async def start_polling(self, bot):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_polling_with_restart(
            CancellingDispatcher(), RecordingBot(), asyncio.Event()
        )


def test_restart_delay_is_ten_seconds() -> None:
    assert POLLING_RESTART_DELAY == 10


# --- graceful shutdown ---

async def test_run_bot_stops_on_shutdown_event(conn, monkeypatch) -> None:
    """После shutdown_event.set() задачи отменяются, сервер гасится."""
    import bot.main as main_module

    cancelled: list[str] = []
    cleaned: list[bool] = []
    bot_holder: list[RecordingBot] = []

    async def fake_task(name):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    def fake_tasks(connection, bot, settings):
        return [
            asyncio.create_task(fake_task("task_a"), name="task_a"),
            asyncio.create_task(fake_task("task_b"), name="task_b"),
        ]

    class FakeRunner:
        async def cleanup(self):
            cleaned.append(True)

    def fake_build_bot(settings):
        bot = RecordingBot()
        bot_holder.append(bot)
        return bot

    monkeypatch.setattr(main_module, "build_background_tasks", fake_tasks)
    monkeypatch.setattr(main_module, "start_web_server",
                        lambda *a, **k: _async_value(FakeRunner()))
    monkeypatch.setattr(main_module, "build_dispatcher", _fake_dispatcher())
    monkeypatch.setattr(main_module, "build_bot", fake_build_bot)

    event = asyncio.Event()
    runner = asyncio.create_task(run_bot(_settings(), conn, shutdown_event=event))
    await asyncio.sleep(0.1)
    event.set()
    await asyncio.wait_for(runner, timeout=5)

    assert sorted(cancelled) == ["task_a", "task_b"], "задачи должны быть отменены"
    assert cleaned == [True], "web_runner.cleanup должен быть вызван"
    assert bot_holder[0].closed, "сессия бота должна быть закрыта"


# --- set_my_commands: команды чатов в списке ---

async def test_run_bot_sets_commands(conn, monkeypatch) -> None:
    """При старте бот регистрирует команды в меню Telegram."""
    import bot.main as main_module

    class FakeRunner:
        async def cleanup(self):
            return None

    fake_bot = RecordingBot()
    monkeypatch.setattr(main_module, "build_bot", lambda settings: fake_bot)
    monkeypatch.setattr(main_module, "build_dispatcher", _fake_dispatcher())
    monkeypatch.setattr(main_module, "build_background_tasks", lambda *a, **k: [])
    monkeypatch.setattr(main_module, "start_web_server",
                        lambda *a, **k: _async_value(FakeRunner()))

    event = asyncio.Event()
    runner = asyncio.create_task(run_bot(_settings(), conn, shutdown_event=event))
    await asyncio.sleep(0.1)
    event.set()
    await asyncio.wait_for(runner, timeout=5)

    assert [c.command for c in fake_bot.commands] == [
        "start", "help", "settings", "setup", "unsync", "schedule", "mygroup",
        "teacher",
    ]


# --- кнопка меню (Menu Button) ---

async def test_reset_menu_button_sends_default(monkeypatch) -> None:
    """Сброс кнопки меню отправляет MenuButtonDefault."""
    import bot.main as main_module
    from aiogram.types import MenuButtonDefault as Expected

    bot = RecordingBot()
    result = await main_module.reset_menu_button(bot)

    assert result is True
    assert isinstance(bot.menu_button, Expected)
    assert bot.menu_button.type == "default"
    assert bot.menu_button.web_app is None, "Web App не должен остаться"


async def test_reset_menu_button_is_not_webapp() -> None:
    """Кнопка меню не содержит WebAppInfo (именно она вела на старый URL)."""
    import bot.main as main_module

    bot = RecordingBot()
    await main_module.reset_menu_button(bot)

    assert getattr(bot.menu_button, "web_app", None) is None
    assert getattr(bot.menu_button, "text", None) is None


async def test_reset_menu_button_survives_telegram_error() -> None:
    """Ошибка Telegram не роняет старт: возвращаем False."""
    import bot.main as main_module

    class BrokenBot:
        async def set_chat_menu_button(self, **kwargs):
            raise RuntimeError("Telegram недоступен")

    assert await main_module.reset_menu_button(BrokenBot()) is False


async def test_run_bot_resets_menu_button(conn, monkeypatch) -> None:
    """При старте бот сбрасывает кнопку меню (в логе «menu button reset»)."""
    import bot.main as main_module

    class FakeRunner:
        async def cleanup(self):
            return None

    fake_bot = RecordingBot()
    monkeypatch.setattr(main_module, "build_bot", lambda settings: fake_bot)
    monkeypatch.setattr(main_module, "build_dispatcher", _fake_dispatcher())
    monkeypatch.setattr(main_module, "build_background_tasks", lambda *a, **k: [])
    monkeypatch.setattr(main_module, "start_web_server",
                        lambda *a, **k: _async_value(FakeRunner()))

    event = asyncio.Event()
    runner = asyncio.create_task(run_bot(_settings(), conn, shutdown_event=event))
    await asyncio.sleep(0.1)
    event.set()
    await asyncio.wait_for(runner, timeout=5)

    assert len(fake_bot.menu_calls) == 1, "кнопка меню должна сбрасываться один раз"
    assert getattr(fake_bot.menu_button, "web_app", None) is None


async def test_install_signal_handlers_is_safe() -> None:
    """Отсутствие add_signal_handler не ломает старт (Windows)."""
    event = asyncio.Event()
    install_signal_handlers(event)
    assert event.is_set() is False


async def test_start_web_server_busy_port_returns_none(conn, monkeypatch) -> None:
    """Занятый порт: возвращаем None и освобождаем ресурсы."""
    import bot.main as main_module

    cleaned: list[bool] = []

    class BusyRunner:
        async def setup(self):
            return None

        async def cleanup(self):
            cleaned.append(True)

    class FailingSite:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def start(self):
            raise OSError(10048, "address already in use")

    monkeypatch.setattr(main_module.web, "AppRunner", lambda app: BusyRunner())
    monkeypatch.setattr(main_module.web, "TCPSite", FailingSite)

    result = await start_web_server(conn, _settings(port=8080))

    assert result is None
    assert cleaned == [True]
# --- rate limit middleware ---

async def test_rate_limit_middleware_blocks_after_limit() -> None:
    """21-е сообщение не доходит до хендлера."""
    middleware = RateLimitMiddleware(RateLimiter(max_per_minute=20))
    calls: list[int] = []

    class User:
        id = 42

    async def handler(event, data):
        calls.append(1)
        return "ok"

    for _ in range(21):
        await middleware(handler, object(), {"event_from_user": User()})

    assert len(calls) == 20, "21-е сообщение должно быть отброшено"


async def test_rate_limit_middleware_passes_without_user() -> None:
    """События без from_user пропускаются (служебные апдейты)."""
    middleware = RateLimitMiddleware(RateLimiter(max_per_minute=1))
    calls: list[int] = []

    async def handler(event, data):
        calls.append(1)
        return "ok"

    for _ in range(3):
        await middleware(handler, object(), {})
    assert len(calls) == 3


# --- мониторинг ---

async def test_alert_not_sent_without_admin_chat() -> None:
    """Без admin_chat_id алерт логируется, но не отправляется."""
    bot = RecordingBot()
    assert await mon.alert_admin(bot, _settings(), "проблема") is False
    assert bot.messages == []


async def test_alert_sent_and_escaped() -> None:
    """Алерт уходит в админ-чат, HTML экранируется."""
    bot = RecordingBot()
    sent = await mon.alert_admin(
        bot, _settings(admin_chat_id=555), "<b>жирный</b> & текст"
    )
    assert sent is True
    chat_id, text = bot.messages[0]
    assert chat_id == 555
    assert "&lt;b&gt;жирный&lt;/b&gt;" in text
    assert "&amp;" in text


async def test_check_health_alerts_on_parser_fails(conn) -> None:
    """Два падения подряд → алерт админу."""
    bot = RecordingBot()
    settings = _settings(admin_chat_id=555)

    mon.note_parser_result(conn, False)
    mon.note_parser_result(conn, False)

    sent = await mon.check_health(conn, bot, settings)
    assert "parser_fails" in sent
    assert any("неудачных попыток" in text for _cid, text in bot.messages)


async def test_check_health_alerts_once_per_day(conn) -> None:
    """Повторная проверка в тот же день не спамит."""
    bot = RecordingBot()
    settings = _settings(admin_chat_id=555)
    mon.note_parser_result(conn, False)
    mon.note_parser_result(conn, False)

    first = await mon.check_health(conn, bot, settings)
    second = await mon.check_health(conn, bot, settings)

    assert "parser_fails" in first
    assert second == [], "повторный алерт не нужен"
    assert len(bot.messages) == 1


async def test_check_health_alerts_on_stale_cache(conn) -> None:
    """Старый кэш расписания → алерт."""
    from bot.services import cache_service

    old = datetime(2026, 9, 20, 12, 0).isoformat()
    with transaction(conn):
        cache_service._set_meta(conn, cache_service.META_LAST_SCHEDULE, old)

    bot = RecordingBot()
    sent = await mon.check_health(
        conn, bot, _settings(admin_chat_id=555),
        now=datetime.fromisoformat(old) + timedelta(hours=30),
    )
    assert "stale_cache" in sent


async def test_check_health_quiet_when_all_ok(conn) -> None:
    """Когда всё в порядке — алертов нет."""
    bot = RecordingBot()
    sent = await mon.check_health(conn, bot, _settings(admin_chat_id=555))
    assert sent == []
    assert bot.messages == []


def test_db_growth_detection(tmp_path: Path) -> None:
    """Рост размера БД больше чем в 2 раза за сутки — подозрительно."""
    from bot.services import cache_service

    db_file = tmp_path / "bot.db"
    db_file.write_bytes(b"x" * 100)

    conn = get_connection(tmp_path / "meta.db")
    apply_migrations(conn)
    try:
        with transaction(conn):
            cache_service._set_meta(conn, mon.META_DB_SIZE, "100")
            cache_service._set_meta(conn, mon.META_DB_SIZE_DATE, "2026-09-26")

        db_file.write_bytes(b"x" * 500)
        grew, current, previous = mon.check_db_growth(
            conn, str(db_file), today="2026-09-27"
        )

        assert grew is True
        assert previous == 100
        assert current >= 500
    finally:
        conn.close()


def test_db_size_includes_wal(tmp_path: Path) -> None:
    """Размер БД учитывает WAL-журнал (иначе алерт роста не сработает)."""
    db_file = tmp_path / "bot.db"
    db_file.write_bytes(b"x" * 100)
    (tmp_path / "bot.db-wal").write_bytes(b"y" * 300)
    (tmp_path / "bot.db-shm").write_bytes(b"z" * 50)

    assert mon.db_size_bytes(str(db_file)) == 450
    assert POLLING_RESTART_DELAY == 10