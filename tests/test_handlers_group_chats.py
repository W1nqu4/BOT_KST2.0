"""Тесты чатов групп и каналов (шаг 2): /setup, /unsync, /schedule.

Сеть Telegram не используется: Bot подменяется заглушкой, которая отвечает
на get_chat_member/get_me и записывает исходящие сообщения. Проверяется вся
цепочка: фильтры → проверка прав → запись в БД → ответ.
"""

from datetime import date
from pathlib import Path

import pytest
import aiogram.types as t
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Chat, ChatMemberUpdated, Message, Update, User

from bot import db
from bot.db import get_connection
from bot.handlers import group_chats as gc
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from bot.services import cache_service
from tests.test_handlers_dispatch import FakeBot

GROUP = "26КАД"
ADMIN_ID = 5001
CHAT_ID = -1001234567890
CHANNEL_ID = -1009876543210


def _admin_member(user: User):
    """ChatMemberAdministrator со всеми обязательными полями aiogram 3.31."""
    return t.ChatMemberAdministrator(
        status=ChatMemberStatus.ADMINISTRATOR, user=user,
        can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=True,
        can_manage_video_chats=True, can_restrict_members=True,
        can_promote_members=False, can_change_info=True,
        can_invite_users=True,
        can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False, can_send_welcome_messages=False,
    )


class GroupBot(FakeBot):
    """FakeBot, который отвечает на get_chat_member и get_me.

    ``statuses`` — карта ``(chat_id, user_id) -> статус``; если пары нет,
    возвращается ``member`` (обычный участник).
    """

    def __init__(self, statuses: dict | None = None,
                 bot_status: str = ChatMemberStatus.ADMINISTRATOR) -> None:
        super().__init__()
        self.statuses = statuses or {}
        self.bot_status = bot_status
        self.bot_id = 424242

    def _member(self, status: str, user_id: int):
        """Собрать объект участника с нужным статусом."""
        user = User(id=user_id, is_bot=False, first_name="Тест")
        if status == ChatMemberStatus.CREATOR:
            return t.ChatMemberOwner(
                status=ChatMemberStatus.CREATOR, user=user, is_anonymous=False,
            )
        if status == ChatMemberStatus.ADMINISTRATOR:
            return _admin_member(user)
        if status == ChatMemberStatus.LEFT:
            return t.ChatMemberLeft(status=ChatMemberStatus.LEFT, user=user)
        if status == ChatMemberStatus.KICKED:
            return t.ChatMemberBanned(status=ChatMemberStatus.KICKED,
                                      user=user, until_date=0)
        return t.ChatMemberMember(status=ChatMemberStatus.MEMBER, user=user)

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name == "GetChatMember":
            chat_id = getattr(method, "chat_id", None)
            user_id = getattr(method, "user_id", None)
            if user_id == self.bot_id:
                return self._member(self.bot_status, self.bot_id)
            status = self.statuses.get((chat_id, user_id),
                                       ChatMemberStatus.MEMBER)
            return self._member(status, user_id)
        if name == "GetMe":
            return User(id=self.bot_id, is_bot=True, first_name="Bot",
                        username="kst_test_bot")
        return await super().__call__(method, request_timeout)
@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями."""
    c = get_connection(tmp_path / "chats.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def conn_with_groups(conn, parsed_schedule):
    """БД с расписанием: доступные группы берутся из кэша."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


@pytest.fixture(scope="module")
def shared_dp():
    """Единственный диспетчер на модуль (роутеры — синглтоны)."""
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed)


@pytest.fixture()
def dp(shared_dp, conn):
    """Диспетчер со свежим соединением и очищенной FSM."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.fsm.storage.storage.clear()
    gc.reset_setup_rate_limit()
    return shared_dp


def _chat(chat_id: int = CHAT_ID,
          chat_type: str = ChatType.SUPERGROUP,
          title: str = "КСТ 26КАД") -> Chat:
    return Chat(id=chat_id, type=chat_type, title=title)


def _update(text: str, tg_id: int = ADMIN_ID, chat_id: int = CHAT_ID,
            chat_type: str = ChatType.SUPERGROUP) -> Update:
    """Апдейт с текстовым сообщением из группового чата."""
    user = User(id=tg_id, is_bot=False, first_name="Админ Чат")
    message = Message(
        message_id=1, date=date.today(), chat=_chat(chat_id, chat_type),
        from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _bot_member(status: str, bot_user: User):
    """Объект участника-бота с нужным статусом."""
    if status == ChatMemberStatus.ADMINISTRATOR:
        return _admin_member(bot_user)
    if status == ChatMemberStatus.LEFT:
        return t.ChatMemberLeft(status=ChatMemberStatus.LEFT, user=bot_user)
    if status == ChatMemberStatus.KICKED:
        return t.ChatMemberBanned(status=ChatMemberStatus.KICKED,
                                  user=bot_user, until_date=0)
    return t.ChatMemberMember(status=ChatMemberStatus.MEMBER, user=bot_user)


def _member_update(new_status: str, old_status: str = ChatMemberStatus.LEFT,
                   chat_id: int = CHAT_ID,
                   chat_type: str = ChatType.SUPERGROUP) -> Update:
    """Апдейт my_chat_member: статус бота в чате изменился."""
    bot_user = User(id=424242, is_bot=True, first_name="Bot")
    from_user = User(id=ADMIN_ID, is_bot=False, first_name="Админ Чат")
    event = ChatMemberUpdated(
        chat=_chat(chat_id, chat_type), from_user=from_user,
        date=date.today(), old_chat_member=_bot_member(old_status, bot_user),
        new_chat_member=_bot_member(new_status, bot_user),
    )
    return Update(update_id=2, my_chat_member=event)


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


def _all_text(bot: FakeBot) -> str:
    return " ".join(_texts(bot))


def _admin_bot(**kwargs) -> GroupBot:
    """Бот, у которого автор — админ, а сам бот — админ чата."""
    statuses = {(CHAT_ID, ADMIN_ID): ChatMemberStatus.CREATOR,
                (CHANNEL_ID, ADMIN_ID): ChatMemberStatus.CREATOR}
    statuses.update(kwargs.pop("statuses", {}))
    return GroupBot(statuses=statuses, **kwargs)
# --- добавление бота в чат ---

async def test_bot_added_to_group_sends_greeting(dp, conn) -> None:
    """Бота добавили в группу → приветствие с инструкцией про /setup."""
    bot = _admin_bot()
    await dp.feed_update(bot, _member_update(ChatMemberStatus.MEMBER))

    body = _all_text(bot)
    assert "Привет" in body
    assert "/setup" in body
    assert "25КАД" in body, "должен быть пример группы"


async def test_bot_added_to_channel_sends_greeting(dp, conn) -> None:
    """Канал: приветствие тоже отправляется (когда бот уже админ)."""
    bot = GroupBot()
    await dp.feed_update(bot, _member_update(
        ChatMemberStatus.ADMINISTRATOR, chat_id=CHANNEL_ID,
        chat_type=ChatType.CHANNEL,
    ))
    assert "/setup" in _all_text(bot)


async def test_bot_removed_deletes_binding(dp, conn) -> None:
    """Бота удалили из чата → привязка стирается."""
    db.add_group_chat(conn, CHAT_ID, "КСТ", "supergroup", GROUP, ADMIN_ID)
    assert db.get_group_chat(conn, CHAT_ID) is not None

    bot = GroupBot()
    await dp.feed_update(bot, _member_update(
        ChatMemberStatus.LEFT, old_status=ChatMemberStatus.MEMBER,
    ))

    assert db.get_group_chat(conn, CHAT_ID) is None


async def test_bot_removed_from_unlinked_chat_is_safe(dp, conn) -> None:
    """Удаление из чата без привязки не падает."""
    bot = GroupBot()
    await dp.feed_update(bot, _member_update(
        ChatMemberStatus.KICKED, old_status=ChatMemberStatus.ADMINISTRATOR,
    ))
    assert db.get_group_chat(conn, CHAT_ID) is None


# --- /setup ---

async def test_setup_from_admin_links_chat(dp, conn_with_groups) -> None:
    """/setup 26КАД от админа → чат привязан, ответ с подтверждением."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26КАД"))

    link = db.get_group_chat(conn_with_groups, CHAT_ID)
    assert link is not None
    assert link["group_name"] == GROUP
    assert link["chat_type"] == ChatType.SUPERGROUP.value
    assert link["added_by"] == ADMIN_ID
    assert link["notifications_enabled"] == 1
    assert link["chat_title"] == "КСТ 26КАД"

    body = _all_text(bot)
    assert "Чат привязан к группе" in body
    assert GROUP in body


async def test_setup_normalizes_group_input(dp, conn_with_groups) -> None:
    """«26 кад» нормализуется в «26КАД» и привязка проходит."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26 кад"))

    link = db.get_group_chat(conn_with_groups, CHAT_ID)
    assert link is not None and link["group_name"] == GROUP


async def test_setup_from_regular_member_denied(dp, conn_with_groups) -> None:
    """Обычный участник получает отказ, в БД ничего не пишется."""
    bot = GroupBot(statuses={(CHAT_ID, ADMIN_ID): ChatMemberStatus.MEMBER})
    await dp.feed_update(bot, _update("/setup 26КАД"))

    assert db.get_group_chat(conn_with_groups, CHAT_ID) is None
    assert "только администратор" in _all_text(bot)


async def test_setup_unknown_group_suggests(dp, conn_with_groups) -> None:
    """Опечатка в группе → предложения похожих, привязки нет."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26КД"))

    assert db.get_group_chat(conn_with_groups, CHAT_ID) is None
    body = _all_text(bot)
    assert "нет в расписании" in body
    assert GROUP in body, "должна быть подсказка с настоящей группой"


async def test_setup_without_argument_shows_hint(dp, conn_with_groups) -> None:
    """/setup без аргумента → инструкция, привязки нет."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup"))

    assert db.get_group_chat(conn_with_groups, CHAT_ID) is None
    assert "/setup" in _all_text(bot)


async def test_setup_in_private_chat_explains(dp, conn) -> None:
    """В личке /setup объясняет, что команда для чатов."""
    bot = _admin_bot()
    user = User(id=ADMIN_ID, is_bot=False, first_name="Студент")
    private = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=ADMIN_ID, type=ChatType.PRIVATE), from_user=user,
        text="/setup 26КАД",
    )
    await dp.feed_update(bot, Update(update_id=3, message=private))

    assert db.get_group_chat(conn, ADMIN_ID) is None
    assert "групповом чате или канале" in _all_text(bot)


async def test_setup_twice_overwrites_group(dp, conn_with_groups) -> None:
    """Повторный /setup меняет группу, а не создаёт вторую запись."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26КАД"))
    gc.reset_setup_rate_limit()
    await dp.feed_update(bot, _update("/setup 26МЭГ"))

    link = db.get_group_chat(conn_with_groups, CHAT_ID)
    assert link is not None
    assert link["group_name"] == "26МЭГ"
    assert len(db.get_all_group_chats(conn_with_groups)) == 1


async def test_setup_rate_limited(dp, conn_with_groups, monkeypatch) -> None:
    """Второй /setup в ту же минуту отсекается anti-flood."""
    monkeypatch.setattr(gc, "SETUP_COOLDOWN_SECONDS", 60)
    gc.reset_setup_rate_limit()

    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26КАД"))
    bot.sent.clear()
    await dp.feed_update(bot, _update("/setup 26МЭГ"))

    assert "Слишком часто" in _all_text(bot)
    # Первая привязка сохранилась.
    assert db.get_group_chat(conn_with_groups, CHAT_ID)["group_name"] == GROUP


def test_setup_rate_limit_allows_after_cooldown() -> None:
    """После окна ожидания /setup снова разрешён."""
    gc.reset_setup_rate_limit()
    assert gc.check_setup_rate_limit(777, now=1000.0) is True
    assert gc.check_setup_rate_limit(777, now=1010.0) is False
    assert gc.check_setup_rate_limit(777, now=1100.0) is True
    gc.reset_setup_rate_limit()
# --- канал: бот должен быть админом ---

async def test_channel_setup_without_admin_rights_explains(
        dp, conn_with_groups) -> None:
    """В канале без прав бота — понятная инструкция, привязки нет."""
    bot = GroupBot(statuses={(CHANNEL_ID, ADMIN_ID): ChatMemberStatus.CREATOR},
                   bot_status=ChatMemberStatus.MEMBER)
    await dp.feed_update(bot, _update(
        "/setup 26КАД", chat_id=CHANNEL_ID, chat_type=ChatType.CHANNEL,
    ))

    assert db.get_group_chat(conn_with_groups, CHANNEL_ID) is None
    assert "администратором канала" in _all_text(bot)


async def test_channel_setup_with_admin_rights_links(dp,
                                                     conn_with_groups) -> None:
    """Канал, где бот админ, привязывается нормально."""
    bot = GroupBot(statuses={(CHANNEL_ID, ADMIN_ID): ChatMemberStatus.CREATOR},
                   bot_status=ChatMemberStatus.ADMINISTRATOR)
    await dp.feed_update(bot, _update(
        "/setup 26КАД", chat_id=CHANNEL_ID, chat_type=ChatType.CHANNEL,
    ))

    link = db.get_group_chat(conn_with_groups, CHANNEL_ID)
    assert link is not None
    assert link["group_name"] == GROUP
    assert link["chat_type"] == ChatType.CHANNEL.value


# --- /unsync ---

async def test_unsync_removes_binding(dp, conn_with_groups) -> None:
    """/unsync от админа отвязывает чат."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/setup 26КАД"))
    bot.sent.clear()

    await dp.feed_update(bot, _update("/unsync"))

    assert db.get_group_chat(conn_with_groups, CHAT_ID) is None
    assert "отвязан" in _all_text(bot)


async def test_unsync_from_member_denied(dp, conn_with_groups) -> None:
    """Обычный участник не может отвязать чат."""
    db.add_group_chat(conn_with_groups, CHAT_ID, "КСТ", "supergroup",
                      GROUP, ADMIN_ID)
    bot = GroupBot(statuses={(CHAT_ID, ADMIN_ID): ChatMemberStatus.MEMBER})

    await dp.feed_update(bot, _update("/unsync"))

    assert db.get_group_chat(conn_with_groups, CHAT_ID) is not None
    assert "только администратор" in _all_text(bot)


async def test_unsync_unlinked_chat_explains(dp, conn) -> None:
    """/unsync в непривязанном чате → подсказка про /setup."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/unsync"))

    assert "не привязан" in _all_text(bot)


# --- /schedule ---

async def test_schedule_in_linked_chat_shows_today(dp,
                                                   conn_with_groups) -> None:
    """/schedule в привязанном чате присылает расписание на сегодня."""
    db.add_group_chat(conn_with_groups, CHAT_ID, "КСТ", "supergroup",
                      GROUP, ADMIN_ID)
    bot = _admin_bot()

    await dp.feed_update(bot, _update("/schedule"))

    body = _all_text(bot)
    today = date.today()
    assert GROUP in body
    assert today.strftime("%d.%m.%Y") in body
    assert "Число:" in body, "шапка с чётностью должна быть"


async def test_schedule_in_unlinked_chat_explains(dp, conn) -> None:
    """/schedule без привязки → подсказка про /setup."""
    bot = _admin_bot()
    await dp.feed_update(bot, _update("/schedule"))

    assert "не привязан" in _all_text(bot)


# --- /chat_status ---

async def test_chat_status_shows_binding(dp, conn_with_groups) -> None:
    """Диагностика: /chat_status показывает группу и уведомления."""
    db.add_group_chat(conn_with_groups, CHAT_ID, "КСТ", "supergroup",
                      GROUP, ADMIN_ID)
    bot = _admin_bot()

    await dp.feed_update(bot, _update("/chat_status"))

    body = _all_text(bot)
    assert GROUP in body
    assert "включены" in body


# --- вспомогательные функции ---

def test_parse_group_argument() -> None:
    """Аргумент /setup разбирается и нормализуется."""
    assert gc._parse_group_argument("26КАД") == "26КАД"
    assert gc._parse_group_argument("26 кад") == "26КАД"
    assert gc._parse_group_argument("") is None
    assert gc._parse_group_argument(None) is None
    assert gc._parse_group_argument("!!!") is None


def test_resolve_group_argument_with_available() -> None:
    """С учётом списка групп распознаётся и «лишнее слово» в аргументе."""
    available = ["26КАД", "26МЭГ"]

    assert gc.resolve_group_argument("26КАД", available) == "26КАД"
    assert gc.resolve_group_argument("26 кад", available) == "26КАД"
    assert gc.resolve_group_argument("26КАД лишние слова", available) == "26КАД"
    # Нет в списке — возвращается нормализованное значение (для подсказок).
    assert gc.resolve_group_argument("26КД", available) == "26КД"


def test_resolve_group_argument_without_cache() -> None:
    """Пустой кэш: доверяем формату, не отбрасываем ввод."""
    assert gc.resolve_group_argument("26КАД", []) == "26КАД"


def test_suggest_groups_finds_typo() -> None:
    """Опечатка находит близкую группу."""
    available = ["26КАД", "26МЭГ", "25КАД"]
    assert "26КАД" in gc.suggest_groups("26КД", available)


def test_build_full_schedule_for_chat_without_new_subs() -> None:
    """Нет новых замен → пустой список (расписание каждый цикл не спамим)."""
    from bot.services.notify_service import build_full_schedule_for_chat

    assert build_full_schedule_for_chat(None, GROUP, date(2026, 9, 28),
                                        False) == []


def test_help_mentions_schedule_command() -> None:
    """/help упоминает /schedule (и команды чатов)."""
    from bot.handlers.help import HELP_TEXT

    assert "/schedule" in HELP_TEXT
    assert "/setup" in HELP_TEXT
    assert "/unsync" in HELP_TEXT