"""Тесты VK-бота (bot_vk): валидация конфигурации и регистрация хендлеров.

Бот не запускается по-настоящему: ``Bot`` подменяется заглушкой, а сообщения —
простым объектом с ``answer``. Так тесты не ходят в VK API и не требуют
реального токена.

Значения ``VK_TOKEN`` / ``VK_GROUP_ID`` читаются в :mod:`bot_vk.config` один раз
при импорте, поэтому тесты подменяют атрибуты модуля (``monkeypatch.setattr``),
а не переменные окружения.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bot_vk.config as vk_config
from bot_vk.config import validate
from bot_vk.handlers import register_handlers


@pytest.fixture()
def valid_vk_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сделать конфигурацию валидной на время теста."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "vk1.a.FAKE_TOKEN")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 123456789)


# --- config.validate ---

def test_validate_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой VK_TOKEN → RuntimeError с подсказкой про .env."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 123456789)
    with pytest.raises(RuntimeError) as exc:
        validate()
    message = str(exc.value)
    assert "VK_TOKEN" in message
    assert ".env" in message


def test_validate_raises_without_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """VK_GROUP_ID = 0 → RuntimeError с подсказкой про .env."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "vk1.a.FAKE_TOKEN")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 0)
    with pytest.raises(RuntimeError) as exc:
        validate()
    message = str(exc.value)
    assert "VK_GROUP_ID" in message
    assert ".env" in message


def test_validate_passes_with_valid_values(valid_vk_config: None) -> None:
    """Заданные токен и group_id — конфигурация валидна."""
    validate()  # не должно бросать


def test_module_import_does_not_require_env() -> None:
    """Модуль импортируется без env: проверка только в validate()."""
    assert vk_config.ENV_FILE.name == ".env"
    assert vk_config.ENV_FILE.parent == vk_config.PROJECT_ROOT
    assert isinstance(vk_config.VK_TOKEN, str)
    assert isinstance(vk_config.VK_GROUP_ID, int)


# --- register_handlers ---

class FakeBot:
    """Заглушка ``Bot``: считает вызовы ``on.message`` и запоминает правила.

    Настоящий vkbottle отдаёт из ``bot.on.message`` декоратор, поэтому
    заглушка ведёт себя так же: принимает любые аргументы и возвращает функцию
    без изменений. ``bot.on`` — обычный :class:`Mock`, так что
    ``bot.on.message`` доступен как атрибут, а его вызовы считает ``call_count``.
    """

    def __init__(self) -> None:
        self.handlers: list[tuple] = []
        self.on = Mock()
        self.on.message = Mock(side_effect=self._capture_message)

    def _capture_message(self, *args, **kwargs):
        def decorator(func):
            self.handlers.append((func, args, kwargs))
            return func

        return decorator


@pytest.fixture()
def fake_bot() -> FakeBot:
    """Заглушка Bot для проверки регистрации хендлеров."""
    return FakeBot()


def test_register_handlers_registers_three(fake_bot: FakeBot) -> None:
    """register_handlers создаёт ровно 3 правила через bot.on.message."""
    register_handlers(fake_bot)  # type: ignore[arg-type]
    assert fake_bot.on.message.call_count == 3
    assert len(fake_bot.handlers) == 3


def test_register_handlers_rules_order(fake_bot: FakeBot) -> None:
    """Порядок правил: /start → Привет → fallback без аргументов."""
    register_handlers(fake_bot)  # type: ignore[arg-type]
    names = [func.__name__ for func, _, _ in fake_bot.handlers]
    assert names == ["start_handler", "hi_handler", "fallback"]
    assert fake_bot.handlers[-1][2] == {}  # fallback ловит всё остальное


class FakeMessage:
    """Заглушка сообщения VK: запоминает текст ответа."""

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


async def test_start_handler_answers(fake_bot: FakeBot) -> None:
    """Обработчик /start отвечает знакомством с ботом КСТ."""
    register_handlers(fake_bot)  # type: ignore[arg-type]
    start_handler = fake_bot.handlers[0][0]
    message = FakeMessage()
    await start_handler(message)
    assert len(message.answers) == 1
    assert "КСТ" in message.answers[0]
    assert "VK" in message.answers[0]


async def test_hi_handler_answers(fake_bot: FakeBot) -> None:
    """Обработчик «Привет» подтверждает работу Long Poll API."""
    register_handlers(fake_bot)  # type: ignore[arg-type]
    hi_handler = fake_bot.handlers[1][0]
    message = FakeMessage()
    await hi_handler(message)
    assert len(message.answers) == 1
    assert message.answers[0].startswith("Привет!")
    assert "Long Poll" in message.answers[0]


async def test_fallback_answers(fake_bot: FakeBot) -> None:
    """Fallback подсказывает доступные команды."""
    register_handlers(fake_bot)  # type: ignore[arg-type]
    fallback = fake_bot.handlers[2][0]
    message = FakeMessage()
    await fallback(message)
    assert len(message.answers) == 1
    assert "/start" in message.answers[0]
    assert "Привет" in message.answers[0]


# --- E2E через настоящий диспетчер vkbottle (без сети) ---
#
# Здесь не подменяется ничего, кроме транспорта: берётся реальный ``Bot``,
# реальный роутер, реальные правила (``text=[...]`` в vkbottle 4 — это VBMLRule,
# а не сравнение строк) и настоящие мини-типы сообщений. Проверяется, что апдейт
# long poll приводит к отправке ожидаемого текста.

GROUP_ID = 123456789
PEER_ID = 555


class FakeMessagesAPI:
    """Мини-``api.messages``: ``Message.answer`` собирает параметры через
    ``get_set_params`` и отправляет их в ``send``, ожидая список в ответе."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def get_set_params(self, params: dict) -> dict:
        return {
            key: value
            for key, value in params.items()
            if value is not None and key not in ("self", "message", "ctx_api")
        }

    async def send(self, peer_ids=None, **kwargs) -> list:
        self.sent.append({**kwargs, "peer_ids": peer_ids})
        return [1]


class FakeAPI:
    """Заглушка API для Bot: вместо сети — запись отправленных сообщений."""

    def __init__(self) -> None:
        self.messages = FakeMessagesAPI()

    async def request(self, method: str, params: dict):
        raise AssertionError(f"неожиданный вызов VK API: {method}")


def make_message_event(text: str) -> dict:
    """Сырой long-poll апдейт ``message_new`` с заданным текстом.

    Поля подобраны так, чтобы проходила валидация моделей vkbottle
    (``version`` и ``fwd_messages`` обязательны).
    """
    return {
        "type": "message_new",
        "group_id": GROUP_ID,
        "event_id": "e1",
        "v": "5.199",
        "object": {
            "message": {
                "id": 1,
                "date": 1700000000,
                "from_id": PEER_ID,
                "peer_id": PEER_ID,
                "conversation_message_id": 1,
                "text": text,
                "out": 0,
                "version": 0,
                "fwd_messages": [],
            },
            "client_info": {
                "button_actions": [],
                "keyboard": False,
                "inline_keyboard": False,
                "carousel": False,
                "lang_id": 0,
            },
        },
    }


@pytest.fixture()
def real_bot():
    """Настоящий ``Bot`` vkbottle с заглушкой транспорта и нашими хендлерами."""
    from vkbottle.bot import Bot

    api = FakeAPI()
    bot = Bot(api=api)
    register_handlers(bot)
    return bot, api


async def _send_text(real_bot, text: str) -> str:
    """Прогнать текст через реальный роутер и вернуть отправленный ответ."""
    bot, api = real_bot
    api.messages.sent.clear()
    await bot.process_event(make_message_event(text))
    assert api.messages.sent, f"бот не ответил на {text!r}"
    return api.messages.sent[0]["message"]


async def test_e2e_hi(real_bot) -> None:
    """«Привет» → ответ про работу Long Poll API."""
    assert await _send_text(real_bot, "Привет") == "Привет! Я работаю. Это Long Poll API VK."


async def test_e2e_start(real_bot) -> None:
    """/start → знакомство с ботом КСТ."""
    answer = await _send_text(real_bot, "/start")
    assert "бот расписания КСТ (VK)" in answer
    assert "Привет" in answer


async def test_e2e_unknown_goes_to_fallback(real_bot) -> None:
    """Неизвестный текст обрабатывает fallback."""
    answer = await _send_text(real_bot, "абракадабра")
    assert "Не понимаю эту команду" in answer


async def test_e2e_reply_goes_to_peer(real_bot) -> None:
    """Ответ уходит в тот же peer_id, откуда пришло сообщение."""
    bot, api = real_bot
    api.messages.sent.clear()
    await bot.process_event(make_message_event("Привет"))
    assert api.messages.sent[0]["peer_ids"] == [PEER_ID]


@pytest.mark.parametrize("text", ["/START", "Привет!", " привет "])
async def test_e2e_case_and_spacing_go_to_fallback(real_bot, text: str) -> None:
    """Правила точные: другой регистр и лишние пробелы — это fallback.

    Подтверждает семантику ``text=[...]`` в vkbottle 4 (VBMLRule, совпадение
    целиком, регистрозависимо): такие сообщения не должны попадать в /start.
    """
    answer = await _send_text(real_bot, text)
    assert "Не понимаю эту команду" in answer


# --- main_vk: сборка бота ---

async def test_main_vk_passes_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() передаёт VK_GROUP_ID в polling и регистрирует хендлеры.

    Без этого vkbottle выясняет group_id сам запросом groups.getById, а
    значение из .env остаётся неиспользованным.
    """
    import bot_vk.main_vk as main_vk

    created: dict = {}
    polling_kwargs: dict = {}

    class RecordingBot:
        def __init__(self, token=None, polling=None) -> None:
            created["token"] = token
            created["polling"] = polling
            # Как у настоящего Bot: labeler.message_view.handlers — список
            # зарегистрированных правил (main() логирует их количество).
            self.labeler = SimpleNamespace(
                message_view=SimpleNamespace(handlers=[])
            )

        async def run_polling(self) -> None:
            created["polling_started"] = True

    class RecordingPolling:
        def __init__(self, group_id=None) -> None:
            polling_kwargs["group_id"] = group_id

    monkeypatch.setattr(main_vk, "Bot", RecordingBot)
    monkeypatch.setattr(main_vk, "BotPolling", RecordingPolling)
    monkeypatch.setattr(main_vk.config, "validate", lambda: None)
    monkeypatch.setattr(main_vk.config, "VK_TOKEN", "vk1.a.FAKE")
    monkeypatch.setattr(main_vk.config, "VK_GROUP_ID", 987654321)
    registered: list = []

    def fake_register_handlers(bot) -> None:
        """Как настоящий register_handlers: вешает правила на bot."""
        registered.append(bot)
        bot.labeler.message_view.handlers.extend(["start", "hi", "fallback"])

    monkeypatch.setattr(main_vk, "register_handlers", fake_register_handlers)

    await main_vk.main()

    assert created["token"] == "vk1.a.FAKE"
    assert polling_kwargs["group_id"] == 987654321
    assert created["polling_started"] is True
    assert len(registered) == 1