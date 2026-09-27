"""Тесты хендлера интеграции с календарём (шаг 9).

Проверяются инструкции, ссылки и отправка проверочного .ics. Bot подменяется
заглушкой, реальная сеть не используется.
"""

from datetime import date
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.db import get_connection, transaction
from bot.handlers.calendar import BTN_CALENDAR
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from bot.services import ics_service
from tests.test_handlers_dispatch import FakeBot

USER_ID = 888
BASE_URL = "https://bot-kst.amvera.io"


def _settings() -> Settings:
    """Настройки с публичным URL (как в бою)."""
    return Settings(
        bot_token="123:TEST", public_base_url=BASE_URL, port=8080,
        db_path=":memory:", admin_ids=(), admin_chat_id=None,
        cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int = USER_ID) -> Update:
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = USER_ID) -> Update:
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и пользователем с группой."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, '26КАД', '2026-09-01T00:00:00+07:00')", (USER_ID,),
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
    """Диспетчер со свежим соединением и настройками."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.workflow_data["settings"] = _settings()
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


def _buttons(bot: FakeBot) -> list[tuple[str, str]]:
    """Пары (текст, callback/url) из последней клавиатуры."""
    markup = None
    for message in reversed(bot.sent):
        if message["reply_markup"] is not None:
            markup = message["reply_markup"]
            break
    if markup is None:
        return []
    result: list[tuple[str, str]] = []
    for row in markup.inline_keyboard:
        for button in row:
            result.append((button.text, button.callback_data or button.url or ""))
    return result


# --- ссылки подписки ---

async def test_calendar_button_shows_links(dp, conn) -> None:
    """Кнопка «Интеграция с календарём»: обе ссылки и кнопки-ссылки."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(BTN_CALENDAR))

    body = " ".join(_texts(bot))
    token = ics_service.get_or_create_token(conn, USER_ID)
    assert f"{BASE_URL}/calendar/{token}.ics" in body
    assert f"webcal://bot-kst.amvera.io/calendar/{token}.ics" in body
    assert "26КАД" in body

    buttons = _buttons(bot)
    labels = [label for label, _ in buttons]
    assert "📱 iPhone / iPad" in labels
    assert "🤖 Android" in labels
    assert "✅ Проверить" in labels
    assert any(url.startswith("https://") and url.endswith(".ics")
               for _, url in buttons), "должна быть кнопка-ссылка на .ics"


async def test_calendar_button_without_group_asks_group(dp, conn) -> None:
    """Без группы бот просит сначала указать группу."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET group_name = '' WHERE tg_id = ?", (USER_ID,)
        )
    bot = FakeBot()
    await dp.feed_update(bot, _update(BTN_CALENDAR))
    assert any("Сначала выбери группу" in t for t in _texts(bot))


async def test_calendar_token_persists(dp, conn) -> None:
    """Повторное открытие не создаёт новый токен."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(BTN_CALENDAR))
    first = ics_service.get_or_create_token(conn, USER_ID)

    bot.sent.clear()
    await dp.feed_update(bot, _update(BTN_CALENDAR))
    second = ics_service.get_or_create_token(conn, USER_ID)

    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM calendar_tokens").fetchone()[0]
    assert count == 1


# --- инструкции ---

async def test_ios_instruction(dp, conn) -> None:
    """cal:ios показывает шаги для iPhone с webcal-ссылкой."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("cal:ios"))

    body = " ".join(_texts(bot))
    assert "iPhone" in body
    assert "Добавить подписной календарь" in body
    assert "webcal://" in body


async def test_android_instruction(dp, conn) -> None:
    """cal:android показывает шаги для Google Calendar с https-ссылкой."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("cal:android"))

    body = " ".join(_texts(bot))
    assert "Android" in body
    assert "Google Calendar" in body
    assert "https://" in body


# --- проверочный файл ---

async def test_test_ics_document_sent(dp, conn) -> None:
    """cal:test присылает .ics с одним событием (caption, а не text)."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("cal:test"))

    documents = [m for m in bot.sent if m["method"] == "SendDocument"]
    assert documents, "должен быть отправлен документ"
    captions = [m.get("caption") or "" for m in bot.sent]
    assert any("Проверка календаря" in c for c in captions)


def test_test_ics_file_content() -> None:
    """Содержимое проверочного файла: одно событие, валидный VCALENDAR."""
    text = ics_service.build_test_ics("26КАД", minutes_ahead=2)
    assert text.count("BEGIN:VEVENT") == 1
    assert text.startswith("BEGIN:VCALENDAR")
    assert text.rstrip().endswith("END:VCALENDAR")


# --- профиль ---

async def test_profile_shows_calendar_entry(dp, conn) -> None:
    """«👤 Профиль» показывает группу и вход в календарь."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("👤 Профиль"))

    body = " ".join(_texts(bot))
    assert "Профиль" in body
    assert "26КАД" in body
    assert any(cb == "profile:calendar" for _, cb in _buttons(bot))


async def test_profile_to_calendar_transition(dp, conn) -> None:
    """Из профиля можно перейти к ссылкам подписки."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("profile:calendar"))

    body = " ".join(_texts(bot))
    assert "Интеграция с календарём" in body


async def test_back_returns_main_menu(dp) -> None:
    """cal:back возвращает главное меню."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("cal:back"))
    assert any("Главное меню" in t for t in _texts(bot))