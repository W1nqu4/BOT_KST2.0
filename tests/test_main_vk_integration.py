"""Тесты совместного запуска Telegram- и VK-ботов (bot.main.run_both_bots).

Проверяется главное для Railway: VK не мешает Telegram. Если VK-креды не
заданы — бот не запускается (только warning); если VK падает — Telegram
продолжает работать.

Сеть не используется: ``vkbottle`` подменяется заглушками, а ``dp.start_polling``
— фейковой корутиной. Значения VK-конфига задаются явно, поэтому autouse-защита
из ``tests/conftest.py`` (пустые VK_TOKEN/VK_GROUP_ID) здесь переопределяется.
"""

import asyncio
import logging

import pytest

import bot.main as main_module
import bot_vk.config as vk_config


class FakeVKHttpClient:
    """Заглушка HTTP-клиента VK: фиксирует закрытие сессии."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeVKApi:
    """Мини-API VK-бота: нужен для закрытия сессии."""

    def __init__(self) -> None:
        self.http_client = FakeVKHttpClient()


class FakeVKBot:
    """Заглушка ``vkbottle.bot.Bot``: не ходит в сеть.

    ``run_polling`` «работает вечно» — как настоящий Long Poll, — поэтому
    задача не завершается сама и её отменяет :func:`run_both_bots`.
    """

    instances: list["FakeVKBot"] = []

    def __init__(self, token=None, polling=None) -> None:
        self.token = token
        self.polling = polling
        self.api = FakeVKApi()
        self.polling_started = False
        self.handlers_registered = False
        FakeVKBot.instances.append(self)

    async def run_polling(self) -> None:
        self.polling_started = True
        await asyncio.sleep(3600)


class FakeBotPolling:
    """Заглушка ``BotPolling``: запоминает переданный group_id."""

    def __init__(self, group_id=None) -> None:
        self.group_id = group_id


class FakeVKApiError(Exception):
    """Ошибка, которой «падает» VK-бот в тестах."""


@pytest.fixture()
def vk_enabled(monkeypatch: pytest.MonkeyPatch):
    """Настроенный VK-бот с подменённым vkbottle (без сети)."""
    FakeVKBot.instances.clear()
    registered: list[FakeVKBot] = []

    monkeypatch.setattr(vk_config, "VK_TOKEN", "FAKE-VK-TOKEN-FOR-TESTS")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 987654321)

    # Импорты в build_vk_bot выполняются во время вызова, поэтому достаточно
    # подменить атрибуты модулей-источников.
    monkeypatch.setattr("vkbottle.bot.Bot", FakeVKBot)
    monkeypatch.setattr("vkbottle.polling.BotPolling", FakeBotPolling)
    monkeypatch.setattr(
        "bot_vk.handlers.register_handlers",
        lambda bot: (
            setattr(bot, "handlers_registered", True),
            registered.append(bot),
        ),
    )
    return registered


@pytest.fixture()
def vk_disabled(monkeypatch: pytest.MonkeyPatch):
    """VK-креды пустые — VK-бот запускаться не должен."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 0)
# --- build_vk_bot / start_vk_bot ---

def test_build_vk_bot_returns_none_without_credentials(vk_disabled) -> None:
    """Без VK_TOKEN/VK_GROUP_ID бот не создаётся."""
    assert main_module.build_vk_bot() is None


def test_build_vk_bot_configures_token_and_group(vk_enabled) -> None:
    """С настроенными кредами создаётся VKBot с токеном и group_id."""
    bot = main_module.build_vk_bot()

    assert isinstance(bot, FakeVKBot)
    assert bot.token == "FAKE-VK-TOKEN-FOR-TESTS"
    assert bot.polling.group_id == 987654321, "group_id должен уходить в polling"
    assert bot.handlers_registered is True, "хендлеры VK должны быть навешены"


def test_start_vk_bot_logs_warning_when_disabled(vk_disabled, caplog) -> None:
    """Не заданы креды → warning и никакого VK-бота."""
    with caplog.at_level(logging.WARNING):
        result = main_module.start_vk_bot()

    assert result is None
    assert any(
        "VK_TOKEN или VK_GROUP_ID не заданы" in record.message
        for record in caplog.records
    )


async def test_start_vk_bot_creates_polling_task(vk_enabled, caplog) -> None:
    """Настроенный VK-бот запускается задачей vk_bot и пишет об этом в лог."""
    with caplog.at_level(logging.INFO):
        vk_bot, task = main_module.start_vk_bot()

    try:
        assert task.get_name() == "vk_bot"
        assert any(
            "VK-бот запущен в основном процессе" in record.message
            for record in caplog.records
        )
        await asyncio.sleep(0.05)
        assert vk_bot.polling_started is True, "run_polling должен быть вызван"
    finally:
        # Отменяем «вечный» polling, чтобы не оставлять висящую задачу.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await main_module.close_vk_bot(vk_bot)
# --- run_both_bots ---

class FakeTelegramDispatcher:
    """Диспетчер, чей polling работает вечно (как настоящий)."""

    def __init__(self) -> None:
        self.started = False

    async def start_polling(self, bot):
        self.started = True
        await asyncio.sleep(3600)


async def test_run_both_bots_starts_telegram_and_vk(vk_enabled, caplog) -> None:
    """Оба бота стартуют параллельно в одном event loop."""
    dp = FakeTelegramDispatcher()

    event = asyncio.Event()
    with caplog.at_level(logging.INFO):
        runner = asyncio.create_task(
            main_module.run_both_bots(object(), dp, event)
        )
        await asyncio.sleep(0.1)
        event.set()
        await asyncio.wait_for(runner, timeout=5)

    assert dp.started, "polling Telegram должен быть запущен"
    assert FakeVKBot.instances, "VK-бот должен быть создан"
    assert FakeVKBot.instances[0].polling_started, "VK run_polling должен быть вызван"
    assert any(
        "VK-бот запущен в основном процессе" in record.message
        for record in caplog.records
    )


async def test_run_both_bots_telegram_survives_vk_crash(
    vk_enabled, monkeypatch, caplog
) -> None:
    """Падение VK-бота не роняет Telegram-бота."""
    dp = FakeTelegramDispatcher()

    class CrashingVKBot(FakeVKBot):
        async def run_polling(self) -> None:
            raise FakeVKApiError("VK недоступен: invalid access_token")

    def crashing_build():
        bot = CrashingVKBot(token="x", polling=FakeBotPolling(group_id=1))
        bot.handlers_registered = True
        return bot

    monkeypatch.setattr(main_module, "build_vk_bot", crashing_build)

    event = asyncio.Event()
    with caplog.at_level(logging.INFO):
        runner = asyncio.create_task(
            main_module.run_both_bots(object(), dp, event)
        )
        await asyncio.sleep(0.1)

        # VK упал, но Telegram всё ещё обслуживает студентов.
        assert dp.started, "Telegram должен продолжать работу после падения VK"
        assert not runner.done(), "run_both_bots не должен завершаться из-за VK"

        event.set()
        await asyncio.wait_for(runner, timeout=5)

    assert any(
        "VK-бот остановился с ошибкой" in record.message
        for record in caplog.records
    ), "падение VK должно попасть в лог"
async def test_run_both_bots_without_vk_still_runs_telegram(
    vk_disabled, monkeypatch, caplog
) -> None:
    """Без VK-кредов Telegram работает, в логе — warning."""
    dp = FakeTelegramDispatcher()
    monkeypatch.setattr(main_module, "build_vk_bot", lambda: None)

    event = asyncio.Event()
    with caplog.at_level(logging.WARNING):
        runner = asyncio.create_task(
            main_module.run_both_bots(object(), dp, event)
        )
        await asyncio.sleep(0.1)
        event.set()
        await asyncio.wait_for(runner, timeout=5)

    assert dp.started
    assert any(
        "VK_TOKEN или VK_GROUP_ID не заданы" in record.message
        for record in caplog.records
    )


async def test_run_both_bots_cancels_vk_and_closes_session(vk_enabled) -> None:
    """При остановке VK-задача отменяется, HTTP-сессия закрывается."""
    dp = FakeTelegramDispatcher()

    event = asyncio.Event()
    runner = asyncio.create_task(
        main_module.run_both_bots(object(), dp, event)
    )
    await asyncio.sleep(0.1)

    vk_bot = FakeVKBot.instances[-1]
    assert not vk_bot.api.http_client.closed

    event.set()
    await asyncio.wait_for(runner, timeout=5)

    assert vk_bot.api.http_client.closed, (
        "сессия VK должна закрываться — иначе «Unclosed client session»"
    )


async def test_run_both_bots_closes_vk_session_on_telegram_exit(vk_enabled) -> None:
    """Даже если первым завершился Telegram, VK-сессия закрывается."""

    class QuickDispatcher:
        async def start_polling(self, bot):
            return None  # polling завершился сразу

    event = asyncio.Event()
    await asyncio.wait_for(
        main_module.run_both_bots(object(), QuickDispatcher(), event), timeout=5
    )

    vk_bot = FakeVKBot.instances[-1]
    assert vk_bot.api.http_client.closed