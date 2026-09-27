"""Тесты обратной связи и трекинга экрана (шаг 11)."""

from datetime import date, datetime
from pathlib import Path

import pytest
from aiogram.types import Chat, Message, Update, User

from bot.db import get_connection, transaction
from bot.handlers import feedback as fb
from bot.keyboards import reply as reply_kb
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from bot.state import (
    SCREEN_SCHEDULE_TODAY,
    SCREEN_UNKNOWN,
    get_last_screen,
    screen_label,
    set_last_screen,
)
from tests.test_handlers_dispatch import FakeBot

ADMIN_ID = 1001
USER_ID = 2002
GROUP = "26КАД"


def _settings(admin_chat_id=ADMIN_ID) -> Settings:
    return Settings(
        bot_token="123:TEST", public_base_url="https://bot.example",
        port=8080, db_path="data/test.db", admin_ids=(ADMIN_ID,),
        admin_chat_id=admin_chat_id, cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int = USER_ID) -> Update:
    user = User(id=tg_id, is_bot=False, first_name="Тест Студент",
                username="test_student")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


@pytest.fixture()
def conn(tmp_path: Path):
    c = get_connection(tmp_path / "feedback.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, full_name, created_at)"
            " VALUES (?, ?, 'Тест Студент', '2026-09-01T00:00:00+07:00')",
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


def _to_admin(bot: FakeBot) -> list[str]:
    """Сообщения, ушедшие в админ-чат."""
    return [m["text"] for m in bot.sent
            if m.get("chat_id") == ADMIN_ID and m["text"]]


# --- трекинг экрана ---

async def test_screen_roundtrip() -> None:
    """Экран сохраняется и читается."""

    class FakeState:
        """Мини-FSM: только data."""

        def __init__(self) -> None:
            self.data: dict = {}

        async def update_data(self, **kwargs):
            self.data.update(kwargs)

        async def get_data(self):
            return dict(self.data)

    state = FakeState()
    assert await get_last_screen(state) == SCREEN_UNKNOWN
    await set_last_screen(state, SCREEN_SCHEDULE_TODAY)
    assert await get_last_screen(state) == SCREEN_SCHEDULE_TODAY


async def test_screen_none_state_is_safe() -> None:
    """None-состояние не ломает функции трекинга."""
    await set_last_screen(None, SCREEN_SCHEDULE_TODAY)
    assert await get_last_screen(None) == SCREEN_UNKNOWN


def test_screen_label() -> None:
    assert screen_label(SCREEN_SCHEDULE_TODAY) == "расписание: сегодня"
    assert screen_label("нечто") == "нечто"


async def test_schedule_button_records_screen(dp, conn) -> None:
    """Кнопка «Сегодня» записывает экран (виден через FSM хранилища)."""
    from aiogram.fsm.storage.base import StorageKey

    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (3005, '26КАД', 'x')"
        )
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_TODAY, 3005))

    key = StorageKey(bot_id=bot.id, chat_id=3005, user_id=3005)
    data = await dp.fsm.storage.get_data(key)
    assert data.get("last_screen") == SCREEN_SCHEDULE_TODAY


# --- обратная связь ---

async def test_feedback_button_asks_for_text(dp, conn) -> None:
    """Кнопка «🐛 Сообщить о проблеме» просит описание."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))

    body = _texts(bot)[0]
    assert "Сообщить о проблеме" in body
    assert "Опиши проблему одним сообщением" in body


async def test_feedback_delivers_to_admin_chat(dp, conn) -> None:
    """Текст пользователя уходит в админ-чат с контекстом."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))
    bot.sent.clear()

    await dp.feed_update(bot, _update("Расписание показывает не тот день"))

    admin_messages = _to_admin(bot)
    assert len(admin_messages) == 1
    report = admin_messages[0]
    assert "🐛 <b>Обратная связь</b>" in report
    assert "Тест Студент" in report
    assert "@test_student" in report
    assert str(USER_ID) in report
    assert GROUP in report
    assert "Расписание показывает не тот день" in report
    assert fb.BOT_VERSION in report

    assert any("Спасибо" in t for t in _texts(bot))


async def test_feedback_reports_last_screen(dp, conn) -> None:
    """В отчёт попадает последний экран пользователя."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_TODAY))
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))
    bot.sent.clear()

    await dp.feed_update(bot, _update("не открывается"))
    report = _to_admin(bot)[0]
    assert "расписание: сегодня" in report


async def test_feedback_escapes_html(dp, conn) -> None:
    """Текст с HTML-спецсимволами экранируется."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))
    bot.sent.clear()

    await dp.feed_update(bot, _update("<script>alert('x')</script> & <b>тег</b>"))
    report = _to_admin(bot)[0]

    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&amp;" in report
    assert "&lt;b&gt;тег&lt;/b&gt;" in report
async def test_feedback_cancel_sends_nothing(dp, conn) -> None:
    """«↩️ Отмена» на этапе FSM: ничего не отправлено."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))
    bot.sent.clear()

    await dp.feed_update(bot, _update(reply_kb.BTN_CANCEL))

    assert _to_admin(bot) == []
    assert any("Отмена" in t for t in _texts(bot))


async def test_feedback_without_admin_chat_warns(dp, conn) -> None:
    """Без ADMIN_CHAT_ID бот честно сообщает, что чат не настроен."""
    dp.workflow_data["settings"] = _settings(admin_chat_id=None)
    bot = FakeBot()

    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK))
    bot.sent.clear()
    await dp.feed_update(bot, _update("проблема"))

    assert _to_admin(bot) == []
    assert any("ADMIN_CHAT_ID" in t for t in _texts(bot))
    dp.workflow_data["settings"] = _settings()


async def test_feedback_without_group(dp, conn) -> None:
    """Пользователь без группы: в отчёте «не выбрана», отправка работает."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(reply_kb.BTN_FEEDBACK, tg_id=77777))
    bot.sent.clear()
    await dp.feed_update(bot, _update("нет группы", tg_id=77777))

    report = _to_admin(bot)[0]
    assert "не выбрана" in report


def test_build_feedback_report_format() -> None:
    """Прямая проверка формата отчёта."""

    class FakeUser:
        id = 555
        full_name = "Пётр Иванов"
        username = "petr"

    report = fb.build_feedback_report(
        FakeUser(), "текст", "26КАД", SCREEN_SCHEDULE_TODAY,
        moment=datetime(2026, 9, 27, 18, 40),
    )
    assert "🐛 <b>Обратная связь</b>" in report
    assert "От: Пётр Иванов" in report
    assert "@petr" in report
    assert "tg://user?id=555" in report
    assert "Группа: <b>26КАД</b>" in report
    assert "Экран: расписание: сегодня" in report
    assert "27.09.2026 18:40" in report
    assert "текст" in report


def test_build_feedback_report_without_username() -> None:
    """Отсутствие username не ломает отчёт."""

    class AnonUser:
        id = 556
        full_name = ""
        username = None

    report = fb.build_feedback_report(AnonUser(), "x", None, SCREEN_UNKNOWN)
    assert "без имени" in report
    assert "без username" in report
    assert "не выбрана" in report


# --- настройки уведомлений ---

async def test_settings_shows_state(dp, conn) -> None:
    """/settings показывает состояние уведомлений и кнопку переключения."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/settings"))

    body = _texts(bot)[0]
    assert "Настройки" in body
    assert "включены" in body


async def test_settings_toggle_disables_notifications(dp, conn) -> None:
    """Переключение выключает уведомления и сохраняет в БД."""
    from aiogram.types import CallbackQuery

    bot = FakeBot()
    query = CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="Тест"),
        chat_instance="ci", data="settings:toggle",
    )
    await dp.feed_update(bot, Update(update_id=5, callback_query=query))

    enabled = conn.execute(
        "SELECT notifications_enabled FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["notifications_enabled"]
    assert enabled == 0


def test_notifications_disabled_skips_user_in_broadcast_notify(conn) -> None:
    """Отключённый пользователь не получает рассылку замен (флаг в БД)."""
    from bot.services import cache_service, notify_service as ns

    with transaction(conn):
        conn.execute(
            "UPDATE users SET notifications_enabled = 0 WHERE tg_id = ?",
            (USER_ID,),
        )
    cache_service.save_substitutions(conn, [{
        "group": GROUP, "date_iso": "2026-09-28", "para": 2,
        "old_subject": "A", "new_subject": "B", "teacher": "", "room": "",
        "is_cancelled": False, "is_self_study": False,
    }])

    import asyncio

    bot = FakeBot()
    sent = asyncio.run(ns._process_substitutions(
        conn, bot, date(2026, 9, 28), throttle=False
    ))
    assert sent == 0