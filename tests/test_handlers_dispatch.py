"""Интеграционные тесты Шага 7: прогон апдейтов через диспетчер.

Bot подменяется заглушкой, которая записывает вызовы API вместо отправки в
Telegram. Проверяется вся цепочка: фильтры → FSM → хендлер → рендер.

Роутеры — модульные синглтоны, а aiogram запрещает привязывать один Router
к двум диспетчерам, поэтому диспетчер создаётся один раз на модуль
(фикстура ``shared_dp``), а соединение БД подставляется в каждом тесте.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import db
from bot.db import get_connection
from bot.handlers import all_routers
from bot.keyboards import reply as rk
from bot.main import BOT_COMMANDS, build_dispatcher
from bot.migrations import apply_migrations
from bot.services import cache_service

USER_ID = 555


class FakeBot(Bot):
    """Bot, который не ходит в сеть, а записывает исходящие сообщения."""

    def __init__(self) -> None:
        super().__init__(token="123456:TEST")
        self.sent: list[dict] = []
        self.edits: list = []      # разметки из editMessageReplyMarkup
        self.commands: list = []

    async def __call__(self, method, request_timeout=None):
        """Заглушка API-вызовов: сохраняем метод, текст, caption, разметку.

        ``caption`` нужен для ``SendDocument`` (текст там не в ``text``),
        ``chat_id`` — чтобы тесты рассылки проверяли получателей.
        """
        name = type(method).__name__
        markup = getattr(method, "reply_markup", None)
        self.sent.append({
            "method": name,
            "text": getattr(method, "text", None),
            "caption": getattr(method, "caption", None),
            "chat_id": getattr(method, "chat_id", None),
            "reply_markup": markup,
        })
        if name == "EditMessageReplyMarkup":
            self.edits.append(markup)
        return type("Msg", (), {"message_id": len(self.sent)})()


class _MemoryStorageStub:
    """Доступ к внутреннему словарю MemoryStorage для очистки между тестами."""

    def __init__(self, storage) -> None:
        self.storage = storage


@pytest.fixture()
def conn(tmp_path: Path):
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def conn_with_groups(conn, parsed_schedule):
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
    """Диспетчер с свежим соединением и очищенной FSM."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _update(text: str, tg_id: int = USER_ID) -> Update:
    """Апдейт с текстовым сообщением от пользователя."""
    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"),
        from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = USER_ID) -> Update:
    """Апдейт с нажатием inline-кнопки.

    ``message`` обязателен: хендлеры отвечают через ``callback.message.answer``,
    а без него отвечать некуда и тест не увидит текста.
    """
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"),
        from_user=user, text="",
    )
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data,
        message=message,
    )
    return Update(update_id=2, callback_query=query)


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


async def _register(dp: Dispatcher, bot: FakeBot) -> None:
    """Провести регистрацию: /start → ввод группы."""
    await dp.feed_update(bot, _update("/start"))
    await dp.feed_update(bot, _update("26КАД"))
    bot.sent.clear()


def test_bot_commands_declared() -> None:
    """set_my_commands получает start/help/settings."""
    assert [c.command for c in BOT_COMMANDS] == ["start", "help", "settings"]


def test_dispatcher_includes_conn(dp, conn) -> None:
    """Диспетчер отдаёт хендлерам соединение БД."""
    assert dp.workflow_data["conn"] is conn
    assert len(all_routers()) == 7


async def test_start_asks_for_group_when_unregistered(dp, conn_with_groups) -> None:
    """/start у нового пользователя просит группу."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))

    assert any("Введи номер группы" in t for t in _texts(bot))
    assert db.get_user_group(conn_with_groups, USER_ID) is None


async def test_entering_group_registers_and_shows_dashboard(dp, conn_with_groups) -> None:
    """Ввод группы 26КАД: сохранение + дашборд."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    await dp.feed_update(bot, _update("26КАД"))

    assert db.get_user_group(conn_with_groups, USER_ID) == "26КАД"
    texts = _texts(bot)
    assert any("сохранена" in t for t in texts)
    dashboard = next(t for t in texts if "Пар сегодня" in t)
    assert "26КАД" in dashboard
    assert "Число:" in dashboard


async def test_group_normalized_on_input(dp, conn_with_groups) -> None:
    """«26 кад» → «26КАД» (нормализация ввода)."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    await dp.feed_update(bot, _update("26 кад"))
    assert db.get_user_group(conn_with_groups, USER_ID) == "26КАД"


async def test_invalid_group_reprompts(dp, conn_with_groups) -> None:
    """Мусор вместо группы — просьба повторить, пользователь не создан."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    await dp.feed_update(bot, _update("!!!"))

    assert any("не похож на настоящий" in t for t in _texts(bot))
    assert db.get_user_group(conn_with_groups, USER_ID) is None


async def test_unknown_group_suggests_similar(dp, conn_with_groups) -> None:
    """Несуществующая группа → предложение похожих кнопками."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    await dp.feed_update(bot, _update("26КД"))

    assert any("нет в расписании" in t for t in _texts(bot))
    pick = [
        b for m in bot.sent if m["reply_markup"] is not None
        for row in m["reply_markup"].inline_keyboard for b in row
        if b.callback_data.startswith("group:pick:")
    ]
    assert pick, "должны быть кнопки с похожими группами"


async def test_start_twice_returns_to_dashboard(dp, conn_with_groups) -> None:
    """Повторный /start зарегистрированного — дашборд, без просьбы о группе."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _update("/start"))
    texts = _texts(bot)
    assert any("С возвращением" in t for t in texts)
    assert not any("Введи номер группы" in t for t in texts)
async def test_today_button_shows_schedule_with_parity(dp, conn_with_groups) -> None:
    """Кнопка «📅 Сегодня»: шапка «Число: DD → Чет/нечет» и карточки пар."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _update(rk.BTN_TODAY))

    texts = _texts(bot)
    assert texts, "должно прийти сообщение"
    body = texts[0]
    assert "Группа: <b>26КАД</b>" in body
    today = date.today()
    assert f"Число: {today.day} →" in body
    assert ("<b>Чет</b>" in body) or ("<b>нечет</b>" in body)
    markup = next(m["reply_markup"] for m in bot.sent if m["reply_markup"])
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "sched:today" in data


async def test_schedule_button_shows_navigation(dp, conn_with_groups) -> None:
    """Кнопка «📆 Расписание»: тот же день + inline-навигация."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))

    markup = next(m["reply_markup"] for m in bot.sent if m["reply_markup"])
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "sched:nav:-1" in data and "sched:nav:+1" in data


async def test_callback_nav_moves_to_next_day(dp, conn_with_groups) -> None:
    """Callback «▶️» показывает следующий день."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _callback("sched:nav:+1"))

    texts = _texts(bot)
    assert texts
    tomorrow = date.today() + timedelta(days=1)
    assert tomorrow.strftime("%d.%m.%Y") in texts[0]


async def test_callback_nav_prev_day(dp, conn_with_groups) -> None:
    """Callback «◀️» показывает предыдущий день."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _callback("sched:nav:-1"))

    texts = _texts(bot)
    yesterday = date.today() - timedelta(days=1)
    assert yesterday.strftime("%d.%m.%Y") in texts[0]


async def test_callback_today(dp, conn_with_groups) -> None:
    """Callback «🔄 Сегодня» возвращает на сегодня."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _callback("sched:today"))
    assert date.today().strftime("%d.%m.%Y") in _texts(bot)[0]


async def test_callback_pick_day_lists_days(dp, conn_with_groups) -> None:
    """«📆 Выбрать день» присылает кнопки дней недели."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _callback("sched:pickday"))

    assert any("Выбери день недели" in t for t in _texts(bot))


async def test_callback_weekday_selects_day(dp, conn_with_groups) -> None:
    """Выбор «Пятница» (sched:nav:5) показывает пятницу текущей недели."""
    bot = FakeBot()
    await _register(dp, bot)
    await dp.feed_update(bot, _callback("sched:nav:5"))

    today = date.today()
    expected = today + timedelta(days=5 - today.isoweekday())
    assert expected.strftime("%d.%m.%Y") in _texts(bot)[0]


async def test_schedule_without_group_asks_to_register(dp, conn) -> None:
    """Без регистрации кнопка «Сегодня» не показывает расписание."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(rk.BTN_TODAY))
    assert not any("Группа:" in t for t in _texts(bot))


async def test_help_and_stub_buttons(dp, conn) -> None:
    """/help работает, /settings показывает настройки уведомлений.

    «📝 Дедлайны» (шаг 8), «👤 Профиль» (шаг 9), «🐛 Сообщить о проблеме»
    (шаг 11) — рабочие разделы, они проверяются в своих тестах.
    """
    bot = FakeBot()

    await dp.feed_update(bot, _update("/help"))
    assert any("Что умеет бот" in t for t in _texts(bot))

    bot.sent.clear()
    await dp.feed_update(bot, _update("/settings"))
    assert any("Настройки" in t for t in _texts(bot))
    assert any("Уведомления" in t for t in _texts(bot))
    bot.sent.clear()