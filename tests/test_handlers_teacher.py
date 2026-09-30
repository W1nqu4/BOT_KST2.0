"""Тесты команды /teacher: расписание преподавателя.

Проверяется сквозной путь через диспетчер (поиск → FSM → БД → ответ) и
чистый рендер. Bot подменяется заглушкой из ``tests.test_handlers_dispatch``.
"""

from datetime import date
from pathlib import Path

import pytest

from bot import db
from bot.db import get_connection, transaction
from bot.handlers import teacher as teacher_h
from bot.keyboards import inline as ik
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

USER_ID = 9500

FI0 = "Кудрявцева Полина Алексеевна"
IVANOV = "Иванов Иван Иванович"
IVANOVA = "Иванова Мария Петровна"
TRICKY = "Злой<b>& Ко"


def _lesson(c, teacher, *, day, para, group, subject, room,
            extra_teacher=None):
    """Вставить занятие в ``schedule_cache`` (чётность пустая)."""
    value = teacher if not extra_teacher else f"{teacher}, {extra_teacher}"
    c.execute(
        "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
        " subject, teacher, room, week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '', 'x')",
        (group, day, para, subject, value, room),
    )


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и расписанием нескольких преподавателей."""
    c = get_connection(tmp_path / "teacher.db")
    apply_migrations(c)
    with transaction(c):
        _lesson(c, FI0, day=1, para=1, group="26КАД",
                subject="ОД.07 Математика", room="307А")
        _lesson(c, FI0, day=1, para=3, group="25КАД",
                subject="ОП.11 Математическое моделирование", room="307А")
        _lesson(c, FI0, day=2, para=2, group="26Д",
                subject="ОД.07 Математика", room="307А")
        # Двое в одной ячейке (подгруппы) — второй тоже должен находиться.
        _lesson(c, IVANOV, day=3, para=1, group="26С1",
                subject="Информатика", room="401Б", extra_teacher=IVANOVA)
        _lesson(c, TRICKY, day=4, para=1, group="26<A>",
                subject="Матем & <физика>", room="307<А>")
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
    """Callback-данные последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.callback_data for row in markup.inline_keyboard
                    for b in row]
    return []


# --- поиск (bot.db.find_teachers) ---

def test_find_teachers_by_surname(conn) -> None:
    """Поиск по фамилии находит преподавателя."""
    assert db.find_teachers(conn, "Кудрявцева") == [FI0]


def test_find_teachers_case_insensitive(conn) -> None:
    """Регистр не важен: SQLite LIKE кириллицу так не умеет, поиск в Python."""
    assert db.find_teachers(conn, "кудрявцева") == [FI0]
    assert db.find_teachers(conn, "КУДРЯВЦЕВА") == [FI0]


def test_find_teachers_yo_normalized(conn) -> None:
    """ё и е взаимозаменяемы."""
    with transaction(conn):
        _lesson(conn, "Лунёва Ирина Владимировна", day=5, para=1,
                group="26КАД", subject="ЕН.01 Математика", room="221")

    assert db.find_teachers(conn, "Лунева") == ["Лунёва Ирина Владимировна"]
    assert db.find_teachers(conn, "Лунёва") == ["Лунёва Ирина Владимировна"]


def test_find_teachers_substring_of_name(conn) -> None:
    """Поиск по части ФИО: «Полина» находит по имени."""
    assert db.find_teachers(conn, "Полина") == [FI0]


def test_find_teachers_multiple(conn) -> None:
    """Несколько совпадений возвращаются списком."""
    found = db.find_teachers(conn, "Иванов")
    assert IVANOV in found
    assert IVANOVA in found


def test_find_teachers_unknown_returns_empty(conn) -> None:
    """Неизвестный запрос — пустой список, без исключений."""
    assert db.find_teachers(conn, "ХХХ") == []
    assert db.find_teachers(conn, "") == []


def test_find_teachers_split_cell(conn) -> None:
    """Второй преподаватель из ячейки подгрупп тоже находится."""
    assert IVANOVA in db.find_teachers(conn, "Иванова")


def test_find_teachers_skips_vacancy(conn) -> None:
    """«вакансия» — не человек, в списке преподавателей её нет."""
    with transaction(conn):
        _lesson(conn, "вакансия", day=1, para=2, group="26КАД",
                subject="ОД.01", room="101")

    assert db.find_teachers(conn, "вакансия") == []


# --- расписание преподавателя (bot.db.get_lessons_for_teacher) ---

def test_get_lessons_for_teacher_sorted(conn) -> None:
    """Пары идут по дню недели, внутри дня — по номеру пары."""
    lessons = db.get_lessons_for_teacher(conn, FI0)
    keys = [(lesson["day_of_week"], lesson["para_number"])
            for lesson in lessons]
    assert keys == sorted(keys)
    assert len(lessons) == 3


def test_get_lessons_for_teacher_unknown(conn) -> None:
    """У несуществующего преподавателя пар нет."""
    assert db.get_lessons_for_teacher(conn, "Некто Нектович") == []


def test_get_lessons_for_teacher_from_shared_cell(conn) -> None:
    """Из ячейки с двумя ФИО пары достаются обоим."""
    assert len(db.get_lessons_for_teacher(conn, IVANOVA)) == 1
    assert len(db.get_lessons_for_teacher(conn, IVANOV)) == 1


# --- рендер ---

def test_render_groups_by_day(conn) -> None:
    """Дни идут по возрастанию, пары — внутри дня."""
    text = teacher_h.render_teacher_schedule(
        FI0, db.get_lessons_for_teacher(conn, FI0)
    )
    assert f"👤 <b>{FI0}</b>" in text
    assert "📅 <b>Понедельник</b>" in text
    assert "📅 <b>Вторник</b>" in text
    assert text.index("Понедельник") < text.index("Вторник")
    assert "  1 пара · ОД.07 Математика · 26КАД · каб. 307А" in text
    assert ("  3 пара · ОП.11 Математическое моделирование · 25КАД"
            " · каб. 307А") in text


def test_render_no_lessons() -> None:
    """Без пар — дружелюбная строка вместо пустоты."""
    text = teacher_h.render_teacher_schedule(FI0, [])
    assert text == f"👤 <b>{FI0}</b>\n\n{teacher_h.NO_LESSONS_TEXT}"
    assert "🎉" in text


def test_render_no_lessons_exact_wording() -> None:
    """Формулировка «нет пар» — ровно как в ТЗ."""
    assert teacher_h.NO_LESSONS_TEXT == "🎉 У преподавателя нет пар в этом семестре"


def test_render_escapes_html() -> None:
    """HTML-спецсимволы в ФИО, предмете, группе и кабинете экранируются."""
    lessons = [{
        "group_name": "26<A>", "day_of_week": 1, "para_number": 1,
        "subject": "Матем & <физика>", "teacher": TRICKY,
        "room": "307<А>", "week_type": "",
    }]
    text = teacher_h.render_teacher_schedule(TRICKY, lessons)

    assert text.startswith("👤 <b>"), "наша разметка остаётся"
    assert "Злой<b>& Ко" not in text, "сырой тег из ФИО не проходит"
    assert "&lt;b&gt;" in text, "тег из ФИО экранирован"
    assert "&amp;" in text, "амперсанд экранирован"
    assert "&lt;физика&gt;" in text
    assert "26&lt;A&gt;" in text
    assert "307&lt;А&gt;" in text


def test_render_marks_week_type() -> None:
    """Чётность добавляется пометкой: иначе строки выглядели бы дублями."""
    lessons = [
        {"group_name": "24М", "day_of_week": 6, "para_number": 3,
         "subject": "МДК 02.02", "teacher": "Абрамов В.Н.", "room": "П-1",
         "week_type": "Чет"},
        {"group_name": "24М", "day_of_week": 6, "para_number": 3,
         "subject": "МДК.02.03", "teacher": "Абрамов В.Н.", "room": "П-1",
         "week_type": "нечет"},
    ]
    text = teacher_h.render_teacher_schedule("Абрамов В.Н.", lessons)
    assert "(Чет)" in text
    assert "(нечет)" in text


def test_render_without_week_type_has_no_marks() -> None:
    """Без чётности лишних скобок в строке нет."""
    lessons = [{
        "group_name": "26КАД", "day_of_week": 1, "para_number": 1,
        "subject": "ОД.07 Математика", "teacher": FI0, "room": "307А",
        "week_type": "",
    }]
    text = teacher_h.render_teacher_schedule(FI0, lessons)
    assert "(Чет)" not in text and "(нечет)" not in text


def test_render_skips_empty_room_and_subject() -> None:
    """Пустые предмет и кабинет не дают пустых « · » в строке."""
    lessons = [{
        "group_name": "26КАД", "day_of_week": 1, "para_number": 2,
        "subject": "", "teacher": FI0, "room": "", "week_type": "",
    }]
    line = teacher_h.render_teacher_line(lessons[0])
    assert line == "  2 пара · 26КАД"


# --- /teacher через диспетчер ---

async def test_teacher_command_with_arg_shows_schedule(dp, conn) -> None:
    """/teacher Кудрявцева — сразу расписание с ФИО."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Кудрявцева"))

    texts = _texts(bot)
    assert any(FI0 in t for t in texts), "ФИО преподавателя"
    assert any("Понедельник" in t for t in texts)
    data = _buttons(bot)
    assert ik.CB_TEACHER_AGAIN in data
    assert ik.CB_MENU in data


async def test_teacher_command_multiple_shows_buttons(dp, conn) -> None:
    """/teacher Иван — несколько совпадений: кнопки выбора."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Иван"))

    assert any("Выбери нужного" in t for t in _texts(bot))
    picks = [d for d in _buttons(bot)
             if d.startswith(ik.CB_TEACHER_PICK_PREFIX)]
    assert len(picks) >= 2


@pytest.mark.parametrize("query", ["ХХХ", "Некто"])
async def test_teacher_command_not_found(dp, conn, query) -> None:
    """/teacher с непонятным запросом — «не нашёл»."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/teacher {query}"))

    assert any("Не нашёл преподавателя" in t for t in _texts(bot))
    assert any("только фамилию" in t for t in _texts(bot))


async def test_teacher_command_without_arg_asks_query(dp, conn) -> None:
    """/teacher без аргумента — просим фамилию (FSM)."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher"))

    assert any("Введи фамилию" in t for t in _texts(bot))
    assert not any("👤 <b>" in t for t in _texts(bot)), "расписания ещё нет"


async def test_teacher_fsm_input_shows_schedule(dp, conn) -> None:
    """/teacher → ввод фамилии → расписание."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher"))
    await dp.feed_update(bot, _update("Кудрявцева"))

    texts = _texts(bot)
    assert any(FI0 in t for t in texts)
    assert any("Понедельник" in t for t in texts)


async def test_teacher_fsm_input_not_found(dp, conn) -> None:
    """Ввод неизвестной фамилии в FSM — «не нашёл»."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher"))
    await dp.feed_update(bot, _update("ХХХ"))

    assert any("Не нашёл преподавателя" in t for t in _texts(bot))


async def test_teacher_fsm_ignores_command_as_query(dp, conn) -> None:
    """Команда вместо фамилии не глотается, а переспрашивается."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher"))
    await dp.feed_update(bot, _update("/help"))

    assert any("Напиши фамилию текстом" in t for t in _texts(bot))


async def test_teacher_pick_shows_schedule(dp, conn) -> None:
    """Кнопка выбора показывает расписание выбранного преподавателя."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Иван"))
    # Индекс берём из того же списка, что строит хендлер: по «Иван» находятся
    # и реальные преподаватели с отчеством «Ивановна», у которых в этой тестовой
    # БД пар нет.
    index = db.find_teachers(conn, "Иван").index(IVANOV)
    bot.sent.clear()

    await dp.feed_update(
        bot, _callback(f"{ik.CB_TEACHER_PICK_PREFIX}{index}")
    )

    texts = _texts(bot)
    assert any(f"👤 <b>{IVANOV}</b>" in t for t in texts)
    assert any("📅 <b>" in t for t in texts)


async def test_teacher_pick_stale_index(dp, conn) -> None:
    """Устаревший индекс не падает: просим ввести фамилию заново."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Иван"))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(f"{ik.CB_TEACHER_PICK_PREFIX}999"))

    assert not any("👤 <b>" in t for t in _texts(bot)), "расписания нет"


async def test_teacher_again_button_restarts_fsm(dp, conn) -> None:
    """«📆 Другой преподаватель» снова просит фамилию, потом ищет."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Кудрявцева"))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(ik.CB_TEACHER_AGAIN))
    assert any("Введи фамилию" in t for t in _texts(bot))

    await dp.feed_update(bot, _update("Полина"))
    assert any("👤 <b>" in t for t in _texts(bot))


async def test_teacher_no_lessons_text(dp, conn) -> None:
    """Преподаватель без пар: «🎉 нет пар в этом семестре»."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Кислова"))

    texts = _texts(bot)
    assert any("Кислова" in t for t in texts), "фамилию узнали"
    assert any(teacher_h.NO_LESSONS_TEXT in t for t in texts)


async def test_teacher_with_lessons_is_not_no_lessons(dp, conn) -> None:
    """У преподавателя с парами «нет пар» не показываем."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/teacher Кудрявцева"))

    assert not any(teacher_h.NO_LESSONS_TEXT in t for t in _texts(bot))


def test_teacher_in_help() -> None:
    """/help упоминает /teacher."""
    from bot.handlers.help import HELP_TEXT

    assert "/teacher — расписание преподавателя" in HELP_TEXT


def test_teacher_kb_layouts() -> None:
    """Клавиатуры: выбор, возврат к поиску и меню."""
    pick = ik.teacher_pick_kb(["А Б В", "Г Д Е"])
    data = [b.callback_data for row in pick.inline_keyboard for b in row]
    assert data == ["teacher:pick:0", "teacher:pick:1", ik.CB_MENU]

    detail = ik.teacher_detail_kb()
    data = [b.callback_data for row in detail.inline_keyboard for b in row]
    assert data == [ik.CB_TEACHER_AGAIN, ik.CB_MENU]