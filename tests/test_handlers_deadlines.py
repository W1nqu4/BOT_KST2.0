"""Тесты хендлеров дедлайнов (шаг 8): диалог добавления, удаление, рендер.

Bot подменяется заглушкой (см. tests/test_handlers_dispatch.py): реальная
сеть не используется. Диспетчер создаётся один раз на модуль — роутеры
являются синглтонами, и aiogram запрещает привязку к двум диспетчерам.
"""

from datetime import date
from pathlib import Path

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import db
from bot.db import get_connection, transaction
from bot.handlers import deadlines as dhl
from bot.keyboards import inline as ikb
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from bot.services import cache_service, deadline_service as dl
from tests.test_handlers_dispatch import FakeBot

USER_ID = 777
TODAY = date(2026, 9, 27)


def _update(text: str, tg_id: int = USER_ID) -> Update:
    """Апдейт с текстовым сообщением."""
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"),
        from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = USER_ID) -> Update:
    """Апдейт с нажатием inline-кнопки (message обязателен — хендлеры отвечают)."""
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"),
        from_user=user, text="",
    )
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data, message=message,
    )
    return Update(update_id=2, callback_query=query)


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и зарегистрированным пользователем."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, '26КАД', '2026-09-01T00:00:00+07:00')",
            (USER_ID,),
        )
    yield c
    c.close()


@pytest.fixture()
def conn_with_schedule(conn, parsed_schedule):
    """БД с расписанием (для списка предметов и преподавателей)."""
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
    return shared_dp


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


def _last_markup(bot: FakeBot):
    """Разметка последнего сообщения с клавиатурой."""
    for message in reversed(bot.sent):
        if message["reply_markup"] is not None:
            return message["reply_markup"]
    return None


def _buttons(bot: FakeBot) -> list[str]:
    """Callback-данные из последней inline-клавиатуры."""
    markup = _last_markup(bot)
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- список дедлайнов ---

async def test_deadlines_button_empty_list(dp, conn) -> None:
    """Кнопка «📝 Дедлайны» на пустом списке: приглашение добавить."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("📝 Дедлайны"))

    texts = _texts(bot)
    assert any("Дедлайны" in t for t in texts)
    assert any("Пока пусто" in t for t in texts)
    assert "dl:add" in _buttons(bot)


async def test_deadline_list_shows_item(dp, conn) -> None:
    """Созданный дедлайн виден в списке."""
    dl.add(conn, USER_ID, "Химия", "", "Сдать лабу", "2026-09-28")
    bot = FakeBot()
    await dp.feed_update(bot, _update("📝 Дедлайны"))

    body = " ".join(_texts(bot))
    assert "Сдать лабу" in body
    assert "Химия" in body
    assert any(b.startswith("dl:del:") for b in _buttons(bot))


# --- полный диалог добавления: custom → текст → дата из календаря ---

async def test_add_custom_flow_through_calendar(dp, conn) -> None:
    """«Произвольный» → название → задача → календарь → дата → дедлайн создан."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback("dl:add"))
    assert any("Как привязать" in t for t in _texts(bot))
    assert "dl:type:custom" in _buttons(bot)

    await dp.feed_update(bot, _callback("dl:type:custom"))
    assert any("Напиши название" in t for t in _texts(bot))

    await dp.feed_update(bot, _update("Курсовая по МДК"))
    assert any("Что нужно сделать" in t for t in _texts(bot))

    await dp.feed_update(bot, _update("сдать до пятницы"))
    assert any("Как укажешь дату" in t for t in _texts(bot))

    await dp.feed_update(bot, _callback("dl:date:calendar"))
    cal_buttons = _buttons(bot)
    assert "dl:cal:manual" in cal_buttons
    pick = next(b for b in cal_buttons if b.startswith("dl:cal:pick:"))

    await dp.feed_update(bot, _callback(pick))

    items = dl.list_active(conn, USER_ID)
    assert len(items) == 1
    item = items[0]
    assert item["subject"] == "Курсовая по МДК"
    assert item["task"] == "сдать до пятницы"
    assert item["deadline_date"] == pick.removeprefix("dl:cal:pick:")
    assert any("добавлен" in t.lower() for t in _texts(bot))


async def test_add_flow_with_empty_task_reprompts(dp, conn) -> None:
    """Пустая задача не проходит: бот просит описать."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:custom"))
    await dp.feed_update(bot, _update("Название"))
    await dp.feed_update(bot, _update("   "))

    assert any("Опиши задачу" in t for t in _texts(bot))
    assert dl.list_active(conn, USER_ID) == []


async def test_add_flow_manual_date(dp, conn) -> None:
    """Дата вручную: «15.10» разбирается и сохраняется."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:custom"))
    await dp.feed_update(bot, _update("Реферат"))
    await dp.feed_update(bot, _update("написать"))
    await dp.feed_update(bot, _callback("dl:date:manual"))
    await dp.feed_update(bot, _update("15.10.2026"))

    items = dl.list_active(conn, USER_ID)
    assert len(items) == 1
    assert items[0]["deadline_date"] == "2026-10-15"


async def test_add_flow_bad_manual_date_reprompts(dp, conn) -> None:
    """Непонятная дата: просьба повторить, дедлайн не создан."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:custom"))
    await dp.feed_update(bot, _update("Реферат"))
    await dp.feed_update(bot, _update("написать"))
    await dp.feed_update(bot, _callback("dl:date:manual"))
    await dp.feed_update(bot, _update("завтра"))

    assert any("Не понял дату" in t for t in _texts(bot))
    assert dl.list_active(conn, USER_ID) == []


async def test_add_flow_without_date(dp, conn) -> None:
    """«Без даты»: дедлайн создаётся с пустой датой."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:custom"))
    await dp.feed_update(bot, _update("Что-то"))
    await dp.feed_update(bot, _update("сделать"))
    await dp.feed_update(bot, _callback("dl:date:none"))

    items = dl.list_active(conn, USER_ID)
    assert len(items) == 1
    assert items[0]["deadline_date"] is None
# --- удаление с подтверждением ---

async def test_delete_flow_removes_from_active(dp, conn) -> None:
    """Удаление: сначала подтверждение, после — исчез из активных."""
    deadline_id = dl.add(conn, USER_ID, "Химия", "", "Сдать лабу", "2026-09-28")
    bot = FakeBot()

    await dp.feed_update(bot, _callback(f"dl:del:{deadline_id}"))
    assert any("Удалить дедлайн" in t for t in _texts(bot))
    assert f"dl:del_confirmed:{deadline_id}" in _buttons(bot)

    await dp.feed_update(bot, _callback(f"dl:del_confirmed:{deadline_id}"))

    assert dl.list_active(conn, USER_ID) == []
    assert any("Пока пусто" in t for t in _texts(bot))
    row = conn.execute(
        "SELECT deleted_at FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row["deleted_at"], "запись должна остаться с deleted_at"


async def test_delete_cancel_keeps_deadline(dp, conn) -> None:
    """«↩️ Отмена» оставляет дедлайн активным."""
    deadline_id = dl.add(conn, USER_ID, "Химия", "", "Лаба", "2026-09-28")
    bot = FakeBot()
    await dp.feed_update(bot, _callback(f"dl:del:{deadline_id}"))
    await dp.feed_update(bot, _callback("dl:list"))
    assert len(dl.list_active(conn, USER_ID)) == 1


async def test_delete_foreign_deadline_denied(dp, conn) -> None:
    """Чужой дедлайн удалить нельзя."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (999, '26Р', 'x')"
        )
    foreign_id = dl.add(conn, 999, "Чужой", "", "чужой", "2026-09-28")
    bot = FakeBot()

    await dp.feed_update(bot, _callback(f"dl:del:{foreign_id}"))
    assert any("не найден" in t for t in _texts(bot))
    assert dl.get(conn, foreign_id, 999) is not None


# --- выбор предмета и преподавателя из расписания ---

async def test_subject_pick_creates_deadline(dp, conn_with_schedule) -> None:
    """Выбор предмета из списка группы → дедлайн с этим предметом."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:subject"))
    assert any("Выбери предмет" in t for t in _texts(bot))
    assert "dl:subj:0" in _buttons(bot)

    await dp.feed_update(bot, _callback("dl:subj:0"))
    await dp.feed_update(bot, _update("Сделать конспект"))
    await dp.feed_update(bot, _callback("dl:date:none"))

    items = dl.list_active(conn_with_schedule, USER_ID)
    assert len(items) == 1
    assert items[0]["subject"], "предмет должен быть заполнен"
    assert items[0]["task"] == "Сделать конспект"


async def test_teacher_pick_creates_deadline(dp, conn_with_schedule) -> None:
    """Выбор преподавателя → дедлайн с преподавателем."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:type:teacher"))
    assert any("Выбери преподавателя" in t for t in _texts(bot))
    assert "dl:teach:0" in _buttons(bot)

    await dp.feed_update(bot, _callback("dl:teach:0"))
    await dp.feed_update(bot, _update("Принести отчёт"))
    await dp.feed_update(bot, _callback("dl:date:none"))

    items = dl.list_active(conn_with_schedule, USER_ID)
    assert len(items) == 1
    assert items[0]["teacher"], "преподаватель должен быть заполнен"
# --- календарь ---

def test_calendar_grid_and_navigation() -> None:
    """Календарь: шапка Пн..Вс, дни месяца, навигация по месяцам."""
    kb = ikb.build_calendar_kb(2026, 9)
    header = kb.inline_keyboard[1]
    assert [b.text for b in header] == list(ikb.WEEKDAY_HEADERS)
    assert ikb.month_title(2026, 9) == "Сентябрь 2026"
    data = [b.callback_data for b in kb.inline_keyboard[-1]]
    assert "dl:cal:nav:2026-08" in data
    assert "dl:cal:nav:2026-10" in data
    assert "dl:cal:manual" in data


def test_calendar_pick_callbacks_are_iso() -> None:
    """Дни месяца дают ISO-даты в callback."""
    kb = ikb.build_calendar_kb(2026, 9)
    picks = [b.callback_data for row in kb.inline_keyboard for b in row
             if b.callback_data.startswith("dl:cal:pick:")]
    assert "dl:cal:pick:2026-09-15" in picks
    assert len(picks) == 30


def test_calendar_year_boundary() -> None:
    """Навигация через границу года: декабрь → январь следующего года."""
    kb = ikb.build_calendar_kb(2026, 12)
    data = [b.callback_data for b in kb.inline_keyboard[-1]]
    assert "dl:cal:nav:2027-01" in data


async def test_calendar_navigation_edits_markup(dp) -> None:
    """◀️/▶️ меняют месяц в существующем сообщении."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:cal:nav:2026-12"))
    assert bot.edits, "edit_reply_markup должен быть вызван"
    title = bot.edits[-1].inline_keyboard[0][0].text
    assert title == "Декабрь 2026"


async def test_calendar_ignore_answers_silently(dp) -> None:
    """Заглушка календаря не отправляет сообщений."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback("dl:cal:ignore"))
    assert _texts(bot) == []


# --- экранирование ---

def test_render_escapes_html_in_task() -> None:
    """Опасные символы в задаче экранируются."""
    items = [{"id": 1, "task": "<script>alert(1)</script>",
              "subject": "Химия", "teacher": "Иванов & К",
              "deadline_date": "2026-09-28"}]
    text = dhl.render_deadlines(items, TODAY)
    assert "<script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "Иванов &amp; К" in text


def test_render_line_urgency_suffixes() -> None:
    """Пометки срочности: N дн. назад / сегодня / завтра."""
    overdue = dhl.render_deadline_line(
        {"task": "A", "subject": "S", "teacher": "",
         "deadline_date": "2026-09-20", "days_left": -7}, TODAY)
    today_line = dhl.render_deadline_line(
        {"task": "B", "subject": "S", "teacher": "",
         "deadline_date": "2026-09-27", "days_left": 0}, TODAY)
    tomorrow = dhl.render_deadline_line(
        {"task": "C", "subject": "S", "teacher": "",
         "deadline_date": "2026-09-28", "days_left": 1}, TODAY)
    assert "7 дн. назад" in overdue
    assert "сегодня" in today_line
    assert "завтра" in tomorrow


def test_render_line_without_date() -> None:
    line = dhl.render_deadline_line(
        {"task": "D", "subject": "", "teacher": "",
         "deadline_date": None, "days_left": None}, TODAY)
    assert "без даты" in line
    assert "D" in line