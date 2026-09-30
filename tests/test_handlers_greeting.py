"""Тесты приветствия, свободного ввода группы и профиля без группы.

Главное, что проверяется: студент БЕЗ кода старосты может указать группу и
пользоваться ботом. Раньше это был замкнутый круг: /start требовал код,
а «Профиль» отправлял в /start.

Bot подменяется заглушкой из ``tests.test_handlers_dispatch``.
"""

from datetime import date
from pathlib import Path

import pytest

from bot import db
from bot.attendance import texts as att_texts
from bot.db import get_connection, transaction
from bot.keyboards import inline as ik
from bot.keyboards import reply as rk
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

USER_ID = 7700
GROUPS = ("25КАД", "26КАД", "026КАД", "25КАД1", "26МЭГ")


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с несколькими группами в расписании (для fuzzy-подсказок)."""
    c = get_connection(tmp_path / "greeting.db")
    apply_migrations(c)
    with transaction(c):
        for name in GROUPS:
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 3, 1, 'История', 'Т.Т.', '101', '', 'x')",
                (name,),
            )
    yield c
    c.close()


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
    return shared_dp


def _update(text: str, tg_id: int = USER_ID):
    """Апдейт с текстовым сообщением."""
    from aiogram.types import Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = USER_ID):
    """Апдейт с нажатием inline-кнопки."""
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


def _texts(bot: FakeBot) -> list[str]:
    """Тексты отправленных сообщений."""
    return [m["text"] for m in bot.sent if m["text"]]


def _buttons(bot: FakeBot) -> list[str]:
    """Подписи кнопок последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.text for row in markup.inline_keyboard for b in row]
    return []


def _button_data(bot: FakeBot) -> list[str]:
    """Callback-данные последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.callback_data for row in markup.inline_keyboard
                    for b in row]
    return []


def _has_reply_kb(bot: FakeBot) -> bool:
    """Пришла ли reply-клавиатура (главное меню)."""
    return any(m["reply_markup"] is not None
               and getattr(m["reply_markup"], "keyboard", None)
               for m in bot.sent)
# --- приветствие ---

async def test_start_greeting_has_three_buttons(dp, conn) -> None:
    """/start у новичка: приветствие и три inline-кнопки выбора."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/start"))

    body = " ".join(_texts(bot))
    assert "Что я умею" in body
    assert "Начни с одного из шагов" in body

    labels = _buttons(bot)
    assert labels == ["📆 Указать мою группу", "📊 Ввести код старосты",
                      "ℹ️ Что выбрать?"]
    assert _button_data(bot) == [ik.CB_SET_GROUP, ik.CB_ENTER_CODE,
                                ik.CB_HELP_CHOOSE]


async def test_start_greeting_no_code_required(dp, conn) -> None:
    """В приветствии не написано, что код обязателен.

    Раньше текст заканчивался «введи код от старосты» — это и был замкнутый
    круг для тех, кому нужен только расписание.
    """
    bot = FakeBot()

    await dp.feed_update(bot, _update("/start"))

    body = " ".join(_texts(bot))
    assert "введи код от старосты" not in body.lower()
    assert any(lbl.startswith("📆 Указать мою группу")
               for lbl in _buttons(bot))


async def test_choose_explains_difference(dp, conn) -> None:
    """«ℹ️ Что выбрать?» объясняет разницу двух способов."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(ik.CB_HELP_CHOOSE))

    body = " ".join(_texts(bot))
    assert "В чём разница?" in body
    assert "расписание" in body
    assert "посещаемости" in body
    assert "Никто тебя не видит" in body
    assert _button_data(bot) == [ik.CB_SET_GROUP]


def test_choose_text_mentions_both_ways() -> None:
    """Текст пояснения содержит оба варианта и слово «позже»."""
    assert "Указать группу" in att_texts.CHOOSE_TEXT
    assert "Код старосты" in att_texts.CHOOSE_TEXT
    assert "позже" in att_texts.CHOOSE_TEXT


async def test_returning_user_sees_menu_not_choice(dp, conn) -> None:
    """Вернувшийся с группой получает короткое приветствие и меню."""
    db.update_user_group_only(conn, USER_ID, "25КАД")
    bot = FakeBot()

    await dp.feed_update(bot, _update("/start"))

    body = " ".join(_texts(bot))
    assert "С возвращением" in body
    assert "Начни с одного из шагов" not in body
    assert _has_reply_kb(bot), "главное меню из reply-кнопок"


# --- «Указать группу» через кнопку (FSM) ---

async def test_setgroup_button_asks_number(dp, conn) -> None:
    """Кнопка «📆 Указать мою группу» запускает FSM ввода номера."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(ik.CB_SET_GROUP))

    assert any("Введи номер группы" in t for t in _texts(bot))


async def test_setgroup_fsm_saves_and_shows_schedule(dp, conn) -> None:
    """Ввод «25КАД» в FSM: сохранение и расписание на сегодня."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback(ik.CB_SET_GROUP))
    bot.sent.clear()

    await dp.feed_update(bot, _update("25КАД"))

    assert db.get_user_group(conn, USER_ID) == "25КАД"
    body = " ".join(_texts(bot))
    assert "сохранена" in body
    assert "Группа: <b>25КАД</b>" in body


async def test_setgroup_fsm_normalizes_case(dp, conn) -> None:
    """«25кад» сохраняется как «25КАД»."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback(ik.CB_SET_GROUP))

    await dp.feed_update(bot, _update("25кад"))

    assert db.get_user_group(conn, USER_ID) == "25КАД"


async def test_setgroup_fsm_typo_suggests(dp, conn) -> None:
    """Опечатка в FSM: подсказки похожих групп кнопками."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback(ik.CB_SET_GROUP))
    bot.sent.clear()

    await dp.feed_update(bot, _update("26КД"))

    assert any("нет в расписании" in t for t in _texts(bot))
    picks = [d for d in _button_data(bot) if d.startswith("group:pick:")]
    assert picks and len(picks) <= 3
    assert db.get_user_group(conn, USER_ID) is None
# --- свободный ввод группы без кнопок ---

async def test_free_input_group_understood(dp, conn) -> None:
    """«25кад» сразу после /start понимается как группа."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start"))
    bot.sent.clear()

    await dp.feed_update(bot, _update("25кад"))

    assert db.get_user_group(conn, USER_ID) == "25КАД"
    body = " ".join(_texts(bot))
    assert "сохранена" in body
    assert "Группа: <b>25КАД</b>" in body, "расписание показано"
    assert _has_reply_kb(bot)


async def test_free_input_normalizes_spaces(dp, conn) -> None:
    """«25 кад» с пробелом тоже распознаётся."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("25 кад"))

    assert db.get_user_group(conn, USER_ID) == "25КАД"


async def test_free_input_typo_shows_three_suggestions(dp, conn) -> None:
    """«ХХХ» похоже на номер, но группы нет — три ближайших варианта."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("25КД"))

    assert any("нет в расписании" in t for t in _texts(bot))
    picks = [d for d in _button_data(bot) if d.startswith("group:pick:")]
    assert picks and len(picks) <= 3
    assert db.get_user_group(conn, USER_ID) is None


async def test_free_input_plain_text_ignored(dp, conn) -> None:
    """Обычный текст не считается группой и не перехватывается."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("привет, как дела"))

    assert _texts(bot) == []
    assert db.get_user_group(conn, USER_ID) is None


async def test_free_input_ignored_when_group_set(dp, conn) -> None:
    """Если группа уже есть, текст не перехватывается (не мешаем другим)."""
    db.update_user_group_only(conn, USER_ID, "26КАД")
    bot = FakeBot()

    await dp.feed_update(bot, _update("25КАД"))

    assert db.get_user_group(conn, USER_ID) == "26КАД", "группа не перезаписана"
    assert _texts(bot) == []


async def test_free_input_ignored_during_fsm(dp, conn) -> None:
    """Во время FSM текст получает FSM-шаг, а не свободный перехват.

    Проверяется на вводе кода старосты: «25КАД» — не код, поэтому должен
    прийти ответ именно FSM-обработчика кода.
    """
    bot = FakeBot()
    await dp.feed_update(bot, _callback(ik.CB_ENTER_CODE))
    bot.sent.clear()

    await dp.feed_update(bot, _update("25КАД"))

    assert any("6 цифр" in t for t in _texts(bot)), "ответ FSM про код"
    assert db.get_user_group(conn, USER_ID) is None


async def test_free_input_typo_then_pick_saves(dp, conn) -> None:
    """Выбор подсказанной группы сохраняет её и показывает расписание."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("25КД"))
    bot.sent.clear()

    await dp.feed_update(bot, _callback("group:pick:25КАД"))

    assert db.get_user_group(conn, USER_ID) == "25КАД"
    body = " ".join(_texts(bot))
    assert "сохранена" in body
    # Вход был из расписания, поэтому вместо дашборда показываем день.
    assert "Группа: <b>25КАД</b>" in body or "Группа:</b> 25КАД" in body


# --- профиль без группы ---

async def test_profile_without_group_shows_choice(dp, conn) -> None:
    """«👤 Профиль» без группы: экран выбора, а не отсылка в /start."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_PROFILE))

    body = " ".join(_texts(bot))
    assert "Профиль" in body
    assert "укажи свою группу" in body
    assert "сначала выбери группу" not in body.lower()

    labels = _buttons(bot)
    assert "📆 Указать группу" in labels
    assert "📊 Ввести код старосты" in labels
    assert "🏠 Меню" in labels
    assert _button_data(bot) == [ik.CB_SET_GROUP, ik.CB_ENTER_CODE,
                                ik.CB_MENU]


async def test_profile_with_group_shows_usual_screen(dp, conn) -> None:
    """С группой профиль обычный."""
    db.update_user_group_only(conn, USER_ID, "25КАД")
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_PROFILE))

    body = " ".join(_texts(bot))
    assert "🎓 Группа: <b>25КАД</b>" in body
    assert "укажи свою группу" not in body
    assert any("Моя посещаемость" in lbl for lbl in _buttons(bot))


async def test_profile_choice_leads_to_group_input(dp, conn) -> None:
    """Из профиля без группы можно сразу указать группу (FSM)."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(rk.BTN_PROFILE))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(ik.CB_SET_GROUP))
    assert any("Введи номер группы" in t for t in _texts(bot))

    await dp.feed_update(bot, _update("26КАД"))

    assert db.get_user_group(conn, USER_ID) == "26КАД"


async def test_profile_choice_leads_to_code_input(dp, conn) -> None:
    """И ввести код старосты — это отдельный (посещаемость) путь."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(rk.BTN_PROFILE))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(ik.CB_ENTER_CODE))

    assert any("код приглашения" in t.lower() for t in _texts(bot))


async def test_mygroup_works_without_code(dp, conn) -> None:
    """«📊 Моя группа» без группы по-прежнему предлагает код или создание."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_MY_GROUP))

    labels = " | ".join(_buttons(bot))
    assert "Ввести код" in labels
    assert "Создать группу" in labels