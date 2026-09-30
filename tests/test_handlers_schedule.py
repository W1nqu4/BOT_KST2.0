"""Тесты расписания без группы: экран «укажи группу» и связка с посещаемостью.

Проверяется сквозной путь через диспетчер (фильтры → FSM → БД → ответ):
раньше без ``users.group_name`` кнопка «📆 Расписание» молчала, теперь
приходит экран с двумя способами указать группу.

Bot подменяется заглушкой из ``tests.test_handlers_dispatch``.
"""

from datetime import date
from pathlib import Path

import pytest

from bot import db
from bot.attendance import db as att_db
from bot.attendance import keyboards as att_kb
from bot.attendance import service
from bot.attendance import texts as att_texts
from bot.db import get_connection, transaction
from bot.handlers import schedule as sched
from bot.keyboards import inline as ik
from bot.keyboards import reply as rk
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

USER_ID = 8101
GROUP = "25КАД"
OTHER_GROUP = "26КАД"
TYPO = "25КД"
STAROSTA_ID = 8100


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и группами в расписании (для fuzzy-подсказок)."""
    c = get_connection(tmp_path / "schedule_nogroup.db")
    apply_migrations(c)
    with transaction(c):
        for name in (GROUP, OTHER_GROUP, "026КАД"):
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, week_type, updated_at)"
                " VALUES (?, 1, 1, 'ОД.01', '', 'x')", (name,)
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
    """Апдейт с нажатием inline-кнопки (``message`` нужен для ответа)."""
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
    """Callback-данные последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.callback_data for row in markup.inline_keyboard
                    for b in row]
    return []


def _register(conn, tg_id: int = USER_ID, group: str = GROUP) -> None:
    """Выдать пользователю группу для расписания."""
    db.update_user_group_only(conn, tg_id, group)


# --- экран «группа не указана» ---

async def test_schedule_button_without_group_shows_prompt(dp, conn) -> None:
    """«📆 Расписание» без группы: экран «укажи группу» + кнопки выбора."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))

    texts = _texts(bot)
    assert any("Чтобы показывать расписание, укажи свою группу" in t
               for t in texts), "просьба указать группу"
    assert any("код от старосты" in t for t in texts)
    assert not any("Группа:" in t for t in texts), "расписания ещё нет"

    data = _buttons(bot)
    assert sched.CB_SET_GROUP in data
    assert att_kb.CB_ENTER_CODE in data
    assert ik.CB_MENU in data


async def test_today_alias_without_group_shows_prompt(dp, conn) -> None:
    """Алиас «📅 Сегодня» ведёт себя так же — экран вместо молчания."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_TODAY))

    assert any("укажи свою группу" in t for t in _texts(bot))


@pytest.mark.parametrize("data", [
    "sched:today", "sched:nav:+1", "sched:pickday", "subj:list",
])
async def test_callbacks_without_group_show_prompt(dp, conn, data) -> None:
    """Навигация и предметы без группы не молчат: приходит тот же экран."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(data))

    assert any("укажи свою группу" in t for t in _texts(bot)), data


# --- «🔢 Указать группу» ---

async def test_set_group_callback_asks_for_number(dp, conn) -> None:
    """«🔢 Указать группу» просит номер группы."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(sched.CB_SET_GROUP))

    assert any("Введи номер группы" in t for t in _texts(bot))


async def test_set_group_then_valid_group_shows_schedule(dp, conn) -> None:
    """Ввод существующей группы: сохранение + расписание на сегодня."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(sched.CB_SET_GROUP))
    await dp.feed_update(bot, _update(GROUP))

    assert db.get_user_group(conn, USER_ID) == GROUP
    texts = _texts(bot)
    assert any(f"Группа <b>{GROUP}</b> сохранена" in t for t in texts)
    assert any(f"Группа: <b>{GROUP}</b>" in t for t in texts), \
        "расписание показано сразу после ввода"
    today = date.today()
    assert any(f"Число: {today.day} →" in t for t in texts)
    assert "sched:nav:+1" in _buttons(bot)


async def test_set_group_then_typo_offers_suggestions(dp, conn) -> None:
    """Опечатка в номере: показываем ближайшие группы кнопками."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(sched.CB_SET_GROUP))
    await dp.feed_update(bot, _update(TYPO))

    assert db.get_user_group(conn, USER_ID) is None, "группа не сохранена"
    assert any("нет в расписании" in t for t in _texts(bot))

    picks = [d for d in _buttons(bot) if d.startswith("group:pick:")]
    assert picks, "должны быть кнопки с похожими группами"
    assert len(picks) <= 3


async def test_set_group_then_pick_suggestion_shows_schedule(dp, conn) -> None:
    """Выбор подсказанной группы из расписания сохраняет её и показывает день."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(sched.CB_SET_GROUP))
    await dp.feed_update(bot, _update(TYPO))
    bot.sent.clear()
    await dp.feed_update(bot, _callback(f"group:pick:{GROUP}"))

    assert db.get_user_group(conn, USER_ID) == GROUP
    body = " ".join(_texts(bot))
    assert "сохранена" in body
    assert f"Группа: <b>{GROUP}</b>" in body, "расписание на сегодня"


async def test_set_group_then_junk_input_prompts_again(dp, conn) -> None:
    """Мусор вместо номера: просим повторить, группа не сохранена."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(sched.CB_SET_GROUP))
    await dp.feed_update(bot, _update("!!!"))

    assert any("не похож на настоящий" in t for t in _texts(bot))
    assert db.get_user_group(conn, USER_ID) is None


# --- «📊 Ввести код от старосты» ---

async def test_enter_code_callback_goes_to_attendance(dp, conn) -> None:
    """Кнопка кода ведёт в существующую регистрацию посещаемости."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(att_kb.CB_ENTER_CODE))

    assert any(att_texts.ASK_CODE in t for t in _texts(bot))


async def test_join_by_code_enables_schedule(dp, conn) -> None:
    """Вступление по коду включает расписание автоматически."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = FakeBot()

    await dp.feed_update(bot, _callback(att_kb.CB_ENTER_CODE))
    await dp.feed_update(bot, _update(code))
    await dp.feed_update(bot, _update("Иванов И.И."))
    bot.sent.clear()

    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))

    assert db.get_user_group(conn, USER_ID) == GROUP
    assert any(f"Группа: <b>{GROUP}</b>" in t for t in _texts(bot))
# --- группа уже задана ---

async def test_schedule_button_with_group_shows_day(dp, conn) -> None:
    """С заданной группой «📆 Расписание» показывает день как раньше."""
    _register(conn)
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))

    texts = _texts(bot)
    assert any(f"Группа: <b>{GROUP}</b>" in t for t in texts)
    assert not any("укажи свою группу" in t for t in texts)
    assert "sched:nav:-1" in _buttons(bot) and "sched:nav:+1" in _buttons(bot)


async def test_student_without_users_group_gets_it_restored(dp, conn) -> None:
    """Студент есть в students, но не в users — связка восстанавливается.

    Так работают записи, сделанные до появления связки: группа берётся из
    ``students.group_name`` при первом обращении к расписанию.
    """
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, USER_ID, code, "Иванов И.И.")
    with transaction(conn):
        conn.execute("DELETE FROM users WHERE tg_id = ?", (USER_ID,))
    assert db.get_user_group(conn, USER_ID) is None

    bot = FakeBot()
    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))

    assert db.get_user_group(conn, USER_ID) == GROUP
    assert any(f"Группа: <b>{GROUP}</b>" in t for t in _texts(bot))


# --- Часть 3: существующее не ломаем ---

async def test_setup_schedule_does_not_touch_students(dp, conn) -> None:
    """``/setup_schedule`` задаёт группу расписания и не пишет в students."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/setup_schedule"))
    await dp.feed_update(bot, _update(GROUP))

    assert db.get_user_group(conn, USER_ID) == GROUP
    assert att_db.get_student(conn, USER_ID) is None, \
        "регистрация расписания не должна создавать студента"


async def test_mygroup_works_without_schedule_group(dp, conn) -> None:
    """«📊 Моя группа» не требует users.group_name."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(att_kb.BTN_MY_GROUP))

    assert any(att_texts.NOT_REGISTERED[:20] in t for t in _texts(bot))
    assert db.get_user_group(conn, USER_ID) is None


async def test_menu_button_from_prompt_returns_keyboard(dp, conn) -> None:
    """«🏠 Меню» из экрана просьбы возвращает главную reply-клавиатуру."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_SCHEDULE))
    await dp.feed_update(bot, _callback(ik.CB_MENU))

    markup = next(m["reply_markup"] for m in bot.sent
                  if m["reply_markup"] is not None
                  and getattr(m["reply_markup"], "keyboard", None))
    assert [b.text for b in markup.keyboard[0]] == [
        rk.BTN_SCHEDULE, rk.BTN_DEADLINES,
    ]


def test_no_group_kb_layout() -> None:
    """Порядок кнопок экрана: группа, код, меню."""
    kb = sched.no_group_kb()
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == [sched.CB_SET_GROUP, att_kb.CB_ENTER_CODE, ik.CB_MENU]


def test_no_group_text_mentions_my_group() -> None:
    """В тексте есть подсказка про «📊 Моя группа»."""
    assert "Моя группа" in sched.NO_GROUP_TEXT
    assert "Расписание" in sched.NO_GROUP_TEXT