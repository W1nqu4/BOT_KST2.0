"""Тесты UI преподавателя: /my_lessons, /my_groups, /attendance, исправление.

Сети нет: настоящий диспетчер aiogram + FakeBot. Проверяются обе стороны
конфликта ``/attendance``: преподаватель видит посещаемость группы, студент —
свой сценарий отметки.
"""

from datetime import date, timedelta

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.db import get_connection
from bot.handlers import teacher_ui
from bot.keyboards import reply as reply_kb
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

TEACHER_ID = 3003
OTHER_TEACHER_ID = 4004
STUDENT_ID = 2002
ADMIN_ID = 1001

FIO = "Богатырева Ирина Павловна"
OTHER_FIO = "Виссарионова Анна Сергеевна"
GROUP = "25КАД"
OTHER_GROUP = "26ИМС1"

# Понедельник, на который в кэше лежат пары (``day_of_week = 1``).
# Дата фиксированная, а не «ближайший понедельник»: тест должен одинаково
# проходить в любой день недели, иначе он зависит от дня прогона.
MONDAY = date(2026, 10, 5)


def _settings() -> Settings:
    return Settings(
        bot_token="123:TEST", public_base_url="https://bot.example", port=8080,
        db_path="data/test.db", admin_ids=(ADMIN_ID,), admin_chat_id=ADMIN_ID,
        cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user,
        text="📊 Посещаемость",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


@pytest.fixture()
def conn(tmp_path):
    """БД: два преподавателя, студент, расписание и отметки."""
    connection = get_connection(tmp_path / "teacher_ui.db")
    apply_migrations(connection)
    connection.executemany(
        "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
        " subject, teacher, room, week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'x')",
        [
            (GROUP, 1, 1, "Математика", FIO, "204", ""),
            (GROUP, 1, 3, "Физика", FIO, "316А", ""),
            (OTHER_GROUP, 1, 1, "История", OTHER_FIO, "101", ""),
        ],
    )
    connection.executemany(
        "INSERT INTO teachers (tg_id, full_name, status, applied_at)"
        " VALUES (?, ?, ?, '2026-10-01T10:00:00+07:00')",
        [(TEACHER_ID, FIO, "approved"),
         (OTHER_TEACHER_ID, OTHER_FIO, "approved")],
    )
    # Студент группы посещаемости.
    connection.execute(
        "INSERT INTO study_groups (group_name, invite_code, created_by,"
        " created_at) VALUES (?, 'CODE', ?, 'x')", (GROUP, STUDENT_ID)
    )
    connection.execute(
        "INSERT INTO students (tg_id, group_name, full_name, role, joined_at)"
        " VALUES (?, ?, 'Иванов Иван', 'student', 'x')",
        (STUDENT_ID, GROUP),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def shared_dp():
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed, settings=_settings())


@pytest.fixture()
def dp(shared_dp, conn):
    shared_dp.workflow_data["conn"] = conn
    shared_dp.workflow_data["settings"] = _settings()
    return shared_dp


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


def _messages(bot: FakeBot) -> list[dict]:
    return [m for m in bot.sent
            if m["method"] in ("SendMessage", "EditMessageText")]


def _alerts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent
            if m["method"] == "AnswerCallbackQuery" and m["text"]]


def _last_markup(bot: FakeBot):
    for message in reversed(_messages(bot)):
        if message.get("reply_markup"):
            return message["reply_markup"]
    return None
# --- клавиатура ---

def test_teacher_kb_layout() -> None:
    """Меню преподавателя: четыре кнопки в двух рядах."""
    markup = reply_kb.teacher_main_kb()

    rows = [[button.text for button in row] for row in markup.keyboard]
    assert rows[0] == [reply_kb.BTN_TEACHER_LESSONS,
                       reply_kb.BTN_TEACHER_GROUPS]
    assert rows[1] == [reply_kb.BTN_TEACHER_ATTENDANCE,
                       reply_kb.BTN_PROFILE]


def test_teacher_kb_differs_from_student() -> None:
    """Меню преподавателя отличается от студенческого."""
    teacher_rows = [[b.text for b in row]
                    for row in reply_kb.teacher_main_kb().keyboard]
    student_rows = [[b.text for b in row] for row in reply_kb.main_kb().keyboard]

    assert teacher_rows != student_rows
    assert reply_kb.BTN_MY_GROUP not in [t for row in teacher_rows for t in row]


# --- /start ---

async def test_start_for_teacher_shows_teacher_menu(dp, conn) -> None:
    """/start преподавателя показывает его меню, а не студенческое."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start", TEACHER_ID))

    markup = _last_markup(bot)
    assert markup is not None
    labels = [b.text for row in markup.keyboard for b in row]
    assert reply_kb.BTN_TEACHER_LESSONS in labels, labels


async def test_start_for_student_not_hijacked(dp, conn) -> None:
    """Студент на /start получает студенческое меню (перехвата нет)."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/start", STUDENT_ID))

    markup = _last_markup(bot)
    labels = [b.text for row in markup.keyboard for b in row]
    assert reply_kb.BTN_TEACHER_LESSONS not in labels, labels


# --- /my_lessons ---

def test_frozen_date_is_independent_of_run_day(monkeypatch) -> None:
    """Фиксация даты не зависит от дня прогона.

    Проверяем сам механизм: после :func:`_freeze_today` модуль видит ровно
    заданный понедельник, а арифметика ``timedelta`` продолжает работать
    (в окне рендера есть день с парами при любом реальном дне недели).
    """
    _freeze_today(monkeypatch, MONDAY)

    assert teacher_ui.date.today() == MONDAY
    assert teacher_ui.date.today().isoweekday() == 1, "фиксируем понедельник"
    assert (teacher_ui.date.today() + timedelta(days=1)) == date(2026, 10, 6)


def _freeze_today(monkeypatch: pytest.MonkeyPatch, fixed: date) -> None:
    """Заморозить ``date.today()`` в модуле ``teacher_ui`` на заданный день.

    Подменяем не сам класс ``date`` вообще, а привязку внутри модуля:
    ``teacher_ui.date`` — это класс, полученный через ``from datetime import
    date``. Поэтому подставляем подкласс с переопределённым ``today()``: так
    сохраняются и арифметика с ``timedelta`` (она возвращает обычный ``date``),
    и ``isinstance``-проверки.

    Патчим только этот модуль и только внутри одного теста: обработчики
    посещаемости тоже читают ``date.today()`` и должны видеть реальный день,
    иначе отметки, вставленные тестом, перестанут находиться.

    Args:
        monkeypatch: фикстура pytest.
        fixed: день, который должен возвращать ``date.today()``.
    """

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return fixed

    monkeypatch.setattr(teacher_ui, "date", _FrozenDate)


async def test_my_lessons_for_teacher(dp, conn, monkeypatch) -> None:
    """Преподаватель получает расписание на несколько дней.

    Дату фиксируем на понедельник: пары в кэше лежат на ``day_of_week = 1``,
    а окно рендера — ``WEEK_DAYS`` дней от «сегодня». Без фиксации тест
    проходил бы только по понедельникам: в остальные дни окно не содержало бы
    дня с парами, и проверки группы и кабинета падали бы.
    """
    _freeze_today(monkeypatch, MONDAY)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_lessons", TEACHER_ID))

    texts = "\n".join(_texts(bot))
    assert "Моё расписание" in texts
    assert FIO in texts
    # Пары показаны с группой и кабинетом (в отличие от студенческого вида).
    assert GROUP in texts
    assert "каб. 204" in texts
    assert "⏰" in texts
    # WEEK_DAYS дней: шапка с датой у каждого дня.
    assert texts.count("📅 <b>") >= teacher_ui.WEEK_DAYS


async def test_my_lessons_denied_for_student(dp, conn) -> None:
    """Студенту /my_lessons недоступен."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_lessons", STUDENT_ID))

    texts = "\n".join(_texts(bot))
    assert "только для преподавателей" in texts
    assert "каб. 204" not in texts, "расписание не показываем"


async def test_my_lessons_pending_teacher_denied(dp, conn) -> None:
    """Заявка pending прав не даёт (доступ только после одобрения)."""
    conn.execute(
        "INSERT INTO teachers (tg_id, full_name, status, applied_at)"
        " VALUES (555777, ?, 'pending', 'x')", (FIO,))
    conn.commit()

    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_lessons", 555777))

    assert "только для преподавателей" in "\n".join(_texts(bot))
# --- /my_groups ---

async def test_my_groups_for_teacher(dp, conn) -> None:
    """Преподаватель видит свои группы кнопками."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_groups", TEACHER_ID))

    texts = "\n".join(_texts(bot))
    assert "Твои группы" in texts

    markup = _last_markup(bot)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{teacher_ui.CB_GROUP_PREFIX}{GROUP}" in data, data


async def test_my_groups_denied_for_student(dp, conn) -> None:
    """Студенту /my_groups недоступен."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_groups", STUDENT_ID))

    assert "только для преподавателей" in "\n".join(_texts(bot))


async def test_teacher_without_groups(dp, conn) -> None:
    """Преподаватель без пар видит понятное сообщение."""
    conn.execute(
        "INSERT INTO teachers (tg_id, full_name, status, applied_at)"
        " VALUES (666888, 'Бежулькина Анастасия Николаевна', 'approved', 'x')")
    conn.commit()

    bot = FakeBot()
    await dp.feed_update(bot, _update("/my_groups", 666888))

    texts = "\n".join(_texts(bot))
    assert "Твои группы" in texts
    assert _last_markup(bot) is None, "кнопок нет"


# --- /attendance: конфликт со студенческой командой ---

async def test_attendance_teacher_sees_group(dp, conn) -> None:
    """Преподаватель: /attendance <своя группа> показывает посещаемость."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(f"/attendance {GROUP}", TEACHER_ID))

    texts = "\n".join(_texts(bot))
    assert f"Посещаемость {GROUP}" in texts


async def test_attendance_student_not_hijacked(dp, conn) -> None:
    """Студент получает СВОЙ сценарий отметки, а не экран преподавателя.

    Команда объявлена в двух роутерах; фильтр IsTeacher должен пропустить
    студента мимо, иначе он потерял бы возможность отметиться.
    """
    bot = FakeBot()
    await dp.feed_update(bot, _update("/attendance", STUDENT_ID))

    texts = "\n".join(_texts(bot))
    assert f"Посещаемость {GROUP}" not in texts, \
        "экран преподавателя студенту не показываем"


async def test_attendance_other_group_refused(dp, conn) -> None:
    """Чужую группу смотреть нельзя."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(f"/attendance {OTHER_GROUP}", TEACHER_ID))

    texts = "\n".join(_texts(bot))
    assert "не ведёшь" in texts
    assert OTHER_GROUP in texts, "показываем, какие группы свои"


async def test_attendance_without_group_lists_groups(dp, conn) -> None:
    """/attendance без аргумента показывает список своих групп."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/attendance", TEACHER_ID))

    assert "Твои группы" in "\n".join(_texts(bot))


async def test_cb_teacher_group_shows_attendance(dp, conn) -> None:
    """Кнопка группы открывает посещаемость."""
    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{teacher_ui.CB_GROUP_PREFIX}{GROUP}", TEACHER_ID))

    assert f"Посещаемость {GROUP}" in "\n".join(_texts(bot))


async def test_cb_teacher_group_other_group_refused(dp, conn) -> None:
    """Кнопка чужой группы отклоняется."""
    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{teacher_ui.CB_GROUP_PREFIX}{OTHER_GROUP}", TEACHER_ID))

    assert "Не твоя группа" in " ".join(_alerts(bot))


# --- teacher_fix_absent: только absent → present ---

TODAY_ISO = date.today().isoformat()


def _make_mark(conn, group: str, para: int, status: str,
               tg_id: int = STUDENT_ID) -> None:
    """Создать отметку студента на сегодня."""
    conn.execute(
        "INSERT INTO attendance (group_name, date_iso, para, tg_id, full_name,"
        " status, marked_by, marked_at, method)"
        " VALUES (?, ?, ?, ?, 'Иванов Иван', ?, 999, 'x', 'starosta')",
        (group, TODAY_ISO, para, tg_id, status),
    )
    conn.commit()


def _fix_cb(group: str, para: int, tg_id: int = STUDENT_ID) -> str:
    """Собрать callback_data исправления прогула."""
    return teacher_ui.CB_SEP.join([
        "teacher_fix_absent", group, TODAY_ISO, str(para), str(tg_id),
    ])


def _mark_status(conn, group: str, para: int,
                 tg_id: int = STUDENT_ID) -> str | None:
    row = conn.execute(
        "SELECT status FROM attendance WHERE group_name = ? AND date_iso = ?"
        " AND para = ? AND tg_id = ?", (group, TODAY_ISO, para, tg_id)
    ).fetchone()
    return str(row["status"]) if row else None


async def test_fix_absent_to_present_allowed(dp, conn) -> None:
    """Прогул разрешено исправить на «присутствовал»."""
    _make_mark(conn, GROUP, 1, "absent")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 1), TEACHER_ID))

    assert _mark_status(conn, GROUP, 1) == "present"
    assert "Исправлено" in " ".join(_alerts(bot))
    edited = [m["text"] for m in bot.sent if m["method"] == "EditMessageText"]
    assert any("прогул → присутствовал" in text for text in edited), edited


async def test_fix_present_is_refused(dp, conn) -> None:
    """«Присутствовал» менять нельзя — правило безопасности.

    Иначе преподаватель мог бы задним числом проставить прогул студенту.
    """
    _make_mark(conn, GROUP, 2, "present")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 2), TEACHER_ID))

    assert _mark_status(conn, GROUP, 2) == "present", "статус не изменился"
    assert "только «прогул»" in " ".join(_alerts(bot))


async def test_fix_late_is_refused(dp, conn) -> None:
    """«Опоздал» тоже не меняется (только absent → present)."""
    _make_mark(conn, GROUP, 2, "late")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 2), TEACHER_ID))

    assert _mark_status(conn, GROUP, 2) == "late"
    assert "только «прогул»" in " ".join(_alerts(bot))


async def test_fix_other_group_refused(dp, conn) -> None:
    """Чужую группу править нельзя."""
    _make_mark(conn, OTHER_GROUP, 1, "absent")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(OTHER_GROUP, 1), TEACHER_ID))

    assert _mark_status(conn, OTHER_GROUP, 1) == "absent", "статус не тронут"
    assert "Не твоя группа" in " ".join(_alerts(bot))


async def test_fix_by_student_refused(dp, conn) -> None:
    """Студент исправлять отметки не может."""
    _make_mark(conn, GROUP, 1, "absent")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 1), STUDENT_ID))

    assert _mark_status(conn, GROUP, 1) == "absent"
    assert "Только для преподавателей" in " ".join(_alerts(bot))


async def test_fix_missing_mark(dp, conn) -> None:
    """Если отметки нет — понятная ошибка, а не падение."""
    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 5), TEACHER_ID))

    assert "Отметки нет" in " ".join(_alerts(bot))


async def test_fix_malformed_callback(dp, conn) -> None:
    """Битые callback-данные не роняют бота."""
    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{teacher_ui.CB_FIX_PREFIX}мусор", TEACHER_ID))

    assert "Некорректные данные" in " ".join(_alerts(bot))


async def test_absent_kb_lists_only_absent(dp, conn) -> None:
    """Кнопки исправления появляются только для прогулов."""
    _make_mark(conn, GROUP, 1, "absent")
    _make_mark(conn, GROUP, 2, "present")

    markup = teacher_ui.absent_kb(conn, GROUP)

    assert markup is not None
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert len(data) == 1, data
    assert f"{teacher_ui.CB_SEP}1{teacher_ui.CB_SEP}" in data[0]


async def test_absent_kb_none_without_absents(dp, conn) -> None:
    """Нет прогулов — нет и кнопок."""
    _make_mark(conn, GROUP, 1, "present")

    assert teacher_ui.absent_kb(conn, GROUP) is None


async def test_mark_records_teacher_as_author(dp, conn) -> None:
    """В отметке сохраняется, кто исправил (method='teacher')."""
    _make_mark(conn, GROUP, 1, "absent")

    bot = FakeBot()
    await dp.feed_update(bot, _callback(_fix_cb(GROUP, 1), TEACHER_ID))

    row = conn.execute(
        "SELECT marked_by, method FROM attendance WHERE group_name = ?"
        " AND para = 1", (GROUP,)).fetchone()
    assert int(row["marked_by"]) == TEACHER_ID
    assert str(row["method"]) == "teacher"
    """Кнопка чужой группы отклоняется."""
    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{teacher_ui.CB_GROUP_PREFIX}{OTHER_GROUP}", TEACHER_ID))

    assert "Не твоя группа" in " ".join(_alerts(bot))