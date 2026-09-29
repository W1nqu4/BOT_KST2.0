"""Тесты ежедневной рассылки расписания в чаты (шаг 5).

Проверяется, что расписание на завтра уходит во все привязанные чаты
независимо от замен, ровно один раз за дату, и что пустой день не
закрепляется. Сеть и Telegram не используются: Bot подменяется заглушкой,
``_sleep`` — чтобы цикл не ждал 15 минут.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot import db
from bot.db import get_connection
from bot.migrations import apply_migrations
from bot.services import cache_service
from bot.services import daily_schedule_service as dss
from tests.test_handlers_dispatch import FakeBot

GROUP = "26КАД"
CHAT_ID = -100500
CHAT_ID_2 = -100600
USER_ID = 555

# Понедельник — у 26КАД есть пары.
MONDAY = date(2026, 9, 28)
# Воскресенье — пар нет.
SUNDAY = date(2026, 9, 27)


class DailyBot(FakeBot):
    """FakeBot, который помнит pin/unpin и умеет отвечать ошибкой."""

    def __init__(self, forbidden_chats: set | None = None) -> None:
        super().__init__()
        self.pinned: list[dict] = []
        self.unpinned: list[dict] = []
        self.forbidden_chats = forbidden_chats or set()

    async def __call__(self, method, request_timeout=None):
        from aiogram.exceptions import TelegramForbiddenError

        name = type(method).__name__
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.forbidden_chats and name in (
            "SendMessage", "PinChatMessage"
        ):
            raise TelegramForbiddenError(method=method,
                                         message="bot is not a member")
        if name == "PinChatMessage":
            self.pinned.append({
                "chat_id": chat_id,
                "message_id": getattr(method, "message_id", None),
                "disable_notification": getattr(
                    method, "disable_notification", None
                ),
            })
        if name == "UnpinChatMessage":
            self.unpinned.append({
                "chat_id": chat_id,
                "message_id": getattr(method, "message_id", None),
            })
        return await super().__call__(method, request_timeout)


async def _no_sleep(seconds: float) -> None:
    """Заглушка паузы."""
    return None


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями."""
    c = get_connection(tmp_path / "daily.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def conn_with_schedule(conn, parsed_schedule):
    """БД с реальным расписанием (нужно для карточек дня)."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


def _link(conn, chat_id: int = CHAT_ID, group: str = GROUP,
          enabled: int = 1) -> None:
    """Привязать чат к группе."""
    db.add_group_chat(conn, chat_id, "КСТ", "supergroup", group, USER_ID)
    if not enabled:
        with db.transaction(conn):
            conn.execute(
                "UPDATE group_chats SET notifications_enabled = 0"
                " WHERE chat_id = ?", (chat_id,)
            )


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent
            if m["method"] == "SendMessage" and m["text"]]
# --- send_daily_to_all_chats ---

async def test_linked_chat_gets_schedule(conn_with_schedule) -> None:
    """Чат с привязкой получает расписание на завтра."""
    _link(conn_with_schedule)
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 1
    body = _texts(bot)[0]
    assert GROUP in body
    assert MONDAY.strftime("%d.%m.%Y") in body
    assert "пара</b>" in body, "должны быть карточки пар"
    assert bot.sent[0]["chat_id"] == CHAT_ID


async def test_chat_without_link_gets_nothing(conn_with_schedule) -> None:
    """Без привязок рассылать некому."""
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 0
    assert _texts(bot) == []


async def test_notifications_disabled_chat_skipped(conn_with_schedule) -> None:
    """Чат с notifications_enabled=0 пропускается."""
    _link(conn_with_schedule, enabled=0)
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 0
    assert _texts(bot) == []


async def test_empty_day_sent_but_not_pinned(conn_with_schedule) -> None:
    """Пустой день: уходит «🎉 На завтра пар нет», закрепления нет."""
    _link(conn_with_schedule)
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, SUNDAY)

    assert sent == 1
    body = _texts(bot)[0]
    assert "🎉 <b>На завтра пар нет</b>" in body
    assert bot.pinned == [], "пустой день закреплять нельзя"
    assert db.get_pinned_message(conn_with_schedule, CHAT_ID) is None


async def test_schedule_is_pinned(conn_with_schedule) -> None:
    """Обычный день: расписание закрепляется тихо, отметка в БД."""
    _link(conn_with_schedule)
    bot = DailyBot()

    await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert len(bot.pinned) == 1
    assert bot.pinned[0]["disable_notification"] is True, "без уведомления"
    assert bot.pinned[0]["chat_id"] == CHAT_ID
    pinned = db.get_pinned_message(conn_with_schedule, CHAT_ID)
    assert pinned["pinned_date_iso"] == MONDAY.isoformat()


async def test_already_sent_today_not_repeated(conn_with_schedule) -> None:
    """Уже отправляли на эту дату → повторно не шлём (дедуп по date)."""
    _link(conn_with_schedule)
    db.mark_group_chat_full_sent(conn_with_schedule, CHAT_ID,
                                 MONDAY.isoformat())
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 0
    assert _texts(bot) == []


async def test_marked_after_send(conn_with_schedule) -> None:
    """После отправки проставляется дата — защита от повтора."""
    _link(conn_with_schedule)

    await dss.send_daily_to_all_chats(conn_with_schedule, DailyBot(), MONDAY)

    chat = db.get_group_chat(conn_with_schedule, CHAT_ID)
    assert chat["last_full_schedule_sent_date"] == MONDAY.isoformat()
async def test_two_runs_send_once(conn_with_schedule) -> None:
    """Два прохода внутри часа: второй ничего не отправляет."""
    _link(conn_with_schedule)

    first_bot = DailyBot()
    first = await dss.send_daily_to_all_chats(conn_with_schedule, first_bot,
                                              MONDAY)
    second_bot = DailyBot()
    second = await dss.send_daily_to_all_chats(conn_with_schedule, second_bot,
                                               MONDAY)

    assert first == 1
    assert second == 0, "второй проход не должен дублировать"
    assert _texts(second_bot) == []


async def test_forbidden_removes_chat(conn_with_schedule) -> None:
    """TelegramForbiddenError → чат удалён из БД."""
    _link(conn_with_schedule)
    bot = DailyBot(forbidden_chats={CHAT_ID})

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 0
    assert db.get_group_chat(conn_with_schedule, CHAT_ID) is None, \
        "мёртвая привязка должна быть удалена"


async def test_multiple_chats_all_get_schedule(conn_with_schedule) -> None:
    """Несколько чатов — расписание уходит в каждый."""
    _link(conn_with_schedule, CHAT_ID)
    _link(conn_with_schedule, CHAT_ID_2)
    bot = DailyBot()

    sent = await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    assert sent == 2
    assert {m["chat_id"] for m in bot.sent
            if m["method"] == "SendMessage"} == {CHAT_ID, CHAT_ID_2}


async def test_chats_of_different_groups_get_own_schedule(
        conn_with_schedule) -> None:
    """Каждый чат получает расписание СВОЕЙ группы (26КАД и 026КАД раздельно)."""
    _link(conn_with_schedule, CHAT_ID, "26КАД")
    _link(conn_with_schedule, CHAT_ID_2, "026КАД")
    bot = DailyBot()

    await dss.send_daily_to_all_chats(conn_with_schedule, bot, MONDAY)

    by_chat = {m["chat_id"]: m["text"] for m in bot.sent
               if m["method"] == "SendMessage"}
    assert "26КАД" in by_chat[CHAT_ID]
    assert "026КАД" in by_chat[CHAT_ID_2]
    # Наборы пар разные — это разные группы, расписание не одно и то же.
    assert by_chat[CHAT_ID] != by_chat[CHAT_ID_2]
# --- цикл daily_schedule_loop ---

async def test_loop_sends_in_configured_hour(conn_with_schedule,
                                             monkeypatch) -> None:
    """В 18:00 цикл отправляет расписание на завтра."""
    _link(conn_with_schedule)
    bot = DailyBot()

    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(dss, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(
            conn_with_schedule, bot,
            now_provider=lambda: datetime(2026, 9, 28, 18, 0),
        )

    assert _texts(bot), "расписание должно уйти в 18:00"
    assert date(2026, 9, 29).strftime("%d.%m.%Y") in _texts(bot)[0]


async def test_loop_does_nothing_outside_hour(conn_with_schedule,
                                              monkeypatch) -> None:
    """Вне 18:00 цикл молчит."""
    _link(conn_with_schedule)
    bot = DailyBot()
    calls: list[int] = []

    async def stopping_sleep(seconds: float) -> None:
        calls.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(dss, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(
            conn_with_schedule, bot,
            now_provider=lambda: datetime(2026, 9, 28, 12, 0),
        )

    assert calls, "цикл должен дойти до паузы"
    assert _texts(bot) == []


async def test_loop_sends_after_late_start(conn_with_schedule,
                                           monkeypatch) -> None:
    """Бот стартовал в 18:15 → расписание всё равно уйдёт (мы внутри часа)."""
    _link(conn_with_schedule)
    bot = DailyBot()

    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(dss, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(
            conn_with_schedule, bot,
            now_provider=lambda: datetime(2026, 9, 28, 18, 15),
        )

    assert len(_texts(bot)) == 1, "запуск в 18:15 должен отправить расписание"
    chat = db.get_group_chat(conn_with_schedule, CHAT_ID)
    assert chat["last_full_schedule_sent_date"] == "2026-09-29"


async def test_loop_late_start_sends_only_once(conn_with_schedule,
                                               monkeypatch) -> None:
    """Дедуп не даёт отправить дважды внутри часа."""
    _link(conn_with_schedule)
    bot = DailyBot()
    calls: list[int] = []

    async def counting_sleep(seconds: float) -> None:
        calls.append(1)
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(dss, "_sleep", counting_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(
            conn_with_schedule, bot,
            now_provider=lambda: datetime(2026, 9, 28, 18, 20),
        )

    assert len(_texts(bot)) == 1, "внутри часа расписание уходит один раз"


async def test_loop_survives_errors(conn_with_schedule, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: следующий проход выполняется."""
    calls: list[int] = []

    class FakeDb:
        """Заглушка db: первый проход падает, второй отменяется."""

        @staticmethod
        def get_all_group_chats(connection):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("БД занята")
            raise asyncio.CancelledError

    monkeypatch.setattr(dss, "db", FakeDb)
    monkeypatch.setattr(dss, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(
            conn_with_schedule, DailyBot(),
            now_provider=lambda: datetime(2026, 9, 28, 18, 0),
        )

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_loop_uses_interval(conn_with_schedule, monkeypatch) -> None:
    """Пауза между проходами — 15 минут."""
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(dss, "_sleep", recording_sleep)

    with pytest.raises(asyncio.CancelledError):
        await dss.daily_schedule_loop(conn_with_schedule, DailyBot())

    assert slept == [dss.DAILY_CHECK_INTERVAL_SECONDS] == [15 * 60]


def test_daily_task_registered_in_main() -> None:
    """Задача подключена в main как фоновая."""
    import inspect

    import bot.main as main_module

    source = inspect.getsource(main_module.build_background_tasks)
    assert "daily_schedule_loop" in source


def test_daily_hour_configured() -> None:
    """Час отправки вынесен в конфиг и равен 18."""
    from bot.config import DAILY_SCHEDULE_HOUR

    assert DAILY_SCHEDULE_HOUR == 18