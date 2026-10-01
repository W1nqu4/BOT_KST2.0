"""Тесты напоминаний о паре за 5 минут (часть A).

Сеть и Telegram не используются: Bot подменяется заглушкой из
``tests.test_handlers_dispatch``, ``_sleep`` — чтобы цикл не ждал минуту.
"""

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from bot import db
from bot.attendance import attestation_service as atts
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import lesson_reminder_service as lrs
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
OTHER_GROUP = "26КАД"
USER_1 = 8801
USER_2 = 8802
USER_3 = 8803

# Понедельник 28.09.2026 — будние звонки, у 25КАД есть пары.
MONDAY = date(2026, 9, 28)
# Суббота 03.10.2026 — субботние звонки (3-я пара с 12:50, 5-й нет).
SATURDAY = date(2026, 10, 3)


class ReminderBot(FakeBot):
    """FakeBot, умеющий падать с TelegramForbiddenError для части чатов."""

    def __init__(self, forbidden: set | None = None) -> None:
        super().__init__()
        self.forbidden = forbidden or set()

    async def __call__(self, method, request_timeout=None):
        from aiogram.exceptions import TelegramForbiddenError

        name = type(method).__name__
        if name == "SendMessage":
            chat_id = getattr(method, "chat_id", None)
            if chat_id in self.forbidden:
                raise TelegramForbiddenError(method=method, message="blocked")
        return await super().__call__(method, request_timeout)


def _add_user(c, tg_id: int, group: str, *, active: int = 1) -> None:
    """Зарегистрировать пользователя с группой."""
    c.execute(
        "INSERT INTO users (tg_id, group_name, full_name, is_active, created_at)"
        " VALUES (?, ?, 'Тест Студент', ?, '2026-09-01T00:00:00+07:00')",
        (tg_id, group, active),
    )


def _add_lesson(c, group: str, day: int, para: int, subject: str,
                room: str) -> None:
    """Добавить пару в расписание группы."""
    c.execute(
        "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
        " subject, teacher, room, week_type, updated_at)"
        " VALUES (?, ?, ?, ?, 'Тест Т.Т.', ?, '', 'x')",
        (group, day, para, subject, room),
    )


@pytest.fixture()
def conn(tmp_path: Path):
    """БД: две группы с парами и три студента."""
    c = get_connection(tmp_path / "reminder.db")
    apply_migrations(c)
    with transaction(c):
        # День 1 (понедельник): 1 и 3 пары у 25КАД, 2 пара у 26КАД.
        _add_lesson(c, GROUP, 1, 1, "ОД.07 Математика", "307А")
        _add_lesson(c, GROUP, 1, 3, "ОП.11 Моделирование", "308А")
        _add_lesson(c, OTHER_GROUP, 1, 2, "ОД.11 Физика", "402А")
        # День 6 (суббота): 3 пара у 25КАД — субботние звонки.
        _add_lesson(c, GROUP, 6, 3, "ОД.07 Математика", "307А")

        _add_user(c, USER_1, GROUP)
        _add_user(c, USER_2, GROUP)
        _add_user(c, USER_3, OTHER_GROUP)
    yield c
    c.close()


def _texts(bot: FakeBot) -> list[str]:
    """Тексты отправленных сообщений."""
    return [m["text"] for m in bot.sent if m["text"]]


# --- upcoming_para ---

@pytest.mark.parametrize(("moment", "expected"), [
    (datetime(2026, 9, 28, 8, 55, 30), 1),      # 09:00 − 5 мин
    (datetime(2026, 9, 28, 9, 0, 0), None),     # пара уже идёт
    (datetime(2026, 9, 28, 10, 40, 0), 2),      # 10:45 − 5 мин
    (datetime(2026, 9, 28, 12, 20, 0), None),   # большая перемена
    (datetime(2026, 9, 28, 13, 10, 0), 3),      # 13:15 − 5 мин
    (datetime(2026, 9, 28, 9, 30, 0), None),    # середина пары
    (datetime(2026, 9, 28, 22, 0, 0), None),    # вечером напоминать не о чем
])
def test_upcoming_para_weekday(moment: datetime, expected: int | None) -> None:
    """Будни: напоминание точно в минуту «начало минус 5»."""
    assert lrs.upcoming_para(moment) == expected


@pytest.mark.parametrize(("moment", "expected"), [
    (datetime(2026, 10, 3, 8, 55, 0), 1),
    (datetime(2026, 10, 3, 12, 45, 0), 3),      # суббота: 3 пара с 12:50
    (datetime(2026, 10, 3, 12, 40, 0), None),
    (datetime(2026, 10, 3, 14, 25, 0), 4),      # 4 пара с 14:30
    (datetime(2026, 10, 3, 16, 40, 0), None),   # 5 пары в субботу нет
])
def test_upcoming_para_saturday(moment: datetime, expected: int | None) -> None:
    """Суббота: другая сетка звонков, пятой пары нет."""
    assert lrs.upcoming_para(moment) == expected


def test_upcoming_para_accepts_custom_minutes() -> None:
    """Минуты до начала — параметр, а не константа."""
    assert lrs.upcoming_para(datetime(2026, 9, 28, 8, 50, 0), 10) == 1
    assert lrs.upcoming_para(datetime(2026, 9, 28, 8, 55, 0), 10) is None


def test_time_range_for_weekday_and_saturday() -> None:
    """Время пары зависит от дня: в субботу третья пара раньше."""
    assert lrs.time_range_for(1, 0) == "09:00-10:35"
    assert lrs.time_range_for(3, 0) == "13:15-14:50"
    assert lrs.time_range_for(3, 5) == "12:50-14:20"
    assert lrs.time_range_for(5, 5) == "", "пятой пары в субботу нет"


def test_reminder_signature_is_per_user() -> None:
    """Подпись включает получателя — иначе один студент закрыл бы группу."""
    first = lrs.reminder_signature("2026-09-28", 1, USER_1)
    second = lrs.reminder_signature("2026-09-28", 1, USER_2)
    assert first != second
# --- check_reminders ---

async def test_two_students_of_group_both_receive(conn) -> None:
    """Оба студента группы получают напоминание (дедуп на получателя)."""
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    assert sent == 2
    texts = _texts(bot)
    assert len(texts) == 2
    assert all("Через 5 минут — 1 пара" in t for t in texts)


async def test_reminder_content(conn) -> None:
    """В напоминании есть предмет, кабинет и время пары."""
    bot = ReminderBot()

    await lrs.check_reminders(conn, bot, now=datetime(2026, 9, 28, 8, 55, 10))

    text = _texts(bot)[0]
    assert "ОД.07 Математика" in text
    assert "307А" in text
    assert "09:00-10:35" in text


async def test_no_reminder_between_lessons(conn) -> None:
    """В перемену (не «минус 5») не отправляем ничего."""
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 12, 20, 0)
    )

    assert sent == 0
    assert _texts(bot) == []


async def test_dedup_second_pass_sends_nothing(conn) -> None:
    """Повторный проход в ту же минуту — 0 отправок."""
    bot = ReminderBot()
    moment = datetime(2026, 9, 28, 8, 55, 10)

    first = await lrs.check_reminders(conn, bot, now=moment)
    bot.sent.clear()
    second = await lrs.check_reminders(conn, bot, now=moment)

    assert first == 2
    assert second == 0
    assert _texts(bot) == []


async def test_dedup_is_per_group_and_para(conn) -> None:
    """Напоминание о другой паре уходит, даже если о первой уже слали."""
    bot = ReminderBot()

    await lrs.check_reminders(conn, bot, now=datetime(2026, 9, 28, 8, 55, 0))
    bot.sent.clear()
    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 13, 10, 0)
    )

    assert sent == 2, "на 3 пару напоминание ещё не уходило"
    assert all("3 пара" in t for t in _texts(bot))


async def test_student_without_lesson_not_notified(conn) -> None:
    """Студент, у чьей группы нет пары в это время, ничего не получает."""
    with transaction(conn):
        _add_user(conn, 8809, "26МЭГ")      # группы нет в расписании
    bot = ReminderBot()

    await lrs.check_reminders(conn, bot, now=datetime(2026, 9, 28, 8, 55, 0))

    # Получают только двое из 25КАД: у 26КАД 1 пары нет, у 26МЭГ пар нет вовсе.
    texts = _texts(bot)
    assert len(texts) == 2


async def test_group_without_that_para_skipped(conn) -> None:
    """Напоминание на 1 пару не уходит группе, у которой её нет."""
    bot = ReminderBot()

    await lrs.check_reminders(conn, bot, now=datetime(2026, 9, 28, 8, 55, 0))

    assert not any("Физика" in t for t in _texts(bot))


async def test_inactive_user_not_notified(conn) -> None:
    """Неактивный пользователь (заблокировал бота) напоминаний не получает."""
    with transaction(conn):
        _add_user(conn, 8810, GROUP, active=0)
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 0)
    )

    assert sent == 2, "только двое активных из 25КАД"


async def test_forbidden_error_deactivates_user(conn) -> None:
    """TelegramForbiddenError → is_active = 0, остальные получают."""
    bot = ReminderBot(forbidden={USER_1})

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 0)
    )

    assert sent == 1, "дошло до второго студента"
    row = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_1,)
    ).fetchone()
    assert row["is_active"] == 0


async def test_failed_send_not_marked_as_sent(conn) -> None:
    """Сбой отправки не ставит метку: следующая попытка дошлёт."""
    bot = ReminderBot(forbidden={USER_1})
    moment = datetime(2026, 9, 28, 8, 55, 0)

    await lrs.check_reminders(conn, bot, now=moment)
    # Пользователь разблокировал бота — возвращаем активность.
    with transaction(conn):
        conn.execute("UPDATE users SET is_active = 1 WHERE tg_id = ?",
                     (USER_1,))
    bot.sent.clear()
    bot.forbidden = set()

    sent = await lrs.check_reminders(conn, bot, now=moment)

    assert sent == 1, "неудачная отправка не должна считаться доставленной"


async def test_saturday_reminder_content(conn) -> None:
    """В субботу напоминание о 3 паре с субботним временем."""
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 10, 3, 12, 45, 0)
    )

    assert sent == 2
    assert "12:50-14:20" in _texts(bot)[0]


async def test_missing_room_shows_dash(conn) -> None:
    """Без кабинета в тексте прочерк, а не пустая строка."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
            " subject, teacher, room, week_type, updated_at)"
            " VALUES (?, 1, 4, 'Семинар', 'Тест Т.Т.', '', '', 'x')", (GROUP,)
        )
    bot = ReminderBot()

    await lrs.check_reminders(conn, bot, now=datetime(2026, 9, 28, 14, 55, 0))

    assert "🚪 —" in _texts(bot)[0]
# --- цикл ---

async def _no_sleep(seconds: float) -> None:
    """Пауза-заглушка: цикл крутится без ожидания."""
    return None


async def test_loop_uses_minute_interval(conn, monkeypatch) -> None:
    """Пауза между проходами — одна минута."""
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", recording_sleep)

    with pytest.raises(asyncio.CancelledError):
        await lrs.lesson_reminder_loop(conn, ReminderBot())

    assert slept == [lrs.LESSON_TICK_SECONDS] == [60]


async def test_loop_survives_errors(conn, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: следующий проход выполняется."""
    calls: list[int] = []

    async def failing_check(connection, bot, now=None, minutes_before=5):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("БД занята")
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "check_reminders", failing_check)
    monkeypatch.setattr(lrs, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await lrs.lesson_reminder_loop(conn, ReminderBot())

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_loop_sends_reminder(conn, monkeypatch) -> None:
    """Цикл доходит до отправки на нужной минуте."""
    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(lrs, "_sleep", stopping_sleep)
    bot = ReminderBot()

    with pytest.raises(asyncio.CancelledError):
        await lrs.lesson_reminder_loop(
            conn, bot, now_provider=lambda: datetime(2026, 9, 28, 8, 55, 5),
        )

    assert len(_texts(bot)) == 2


def test_lesson_loop_registered_in_main() -> None:
    """Задача подключена в main как фоновая."""
    import inspect

    import bot.main as main_module

    source = inspect.getsource(main_module.build_background_tasks)
    assert "lesson_reminder_loop" in source


def test_truant_loop_registered_in_main() -> None:
    """Задача прогульщиков тоже подключена в main."""
    import inspect

    import bot.main as main_module

    source = inspect.getsource(main_module.build_background_tasks)
    assert "weekly_truant_loop" in source


def test_background_tasks_count_is_twelve() -> None:
    """Всего фоновых задач — 12 (ТЗ: 10 прежних + 2 новых)."""
    import ast
    import inspect
    import textwrap

    import bot.main as main_module

    source = textwrap.dedent(
        inspect.getsource(main_module.build_background_tasks)
    )
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value)

    assert len(names) == 12, names
    assert "lesson_reminder_loop" in names
    assert "weekly_truant_loop" in names
    assert "notify_substitutions_loop" in names, "существующая задача цела"


def test_help_mentions_reminders() -> None:
    """/help рассказывает про напоминание за 5 минут."""
    from bot.handlers.help import HELP_TEXT

    assert "За 5 минут до пары" in HELP_TEXT
def test_help_mentions_reminders() -> None:
    """/help рассказывает про напоминание за 5 минут."""
    from bot.handlers.help import HELP_TEXT

    assert "За 5 минут до пары" in HELP_TEXT


# --- воскресный блок «под угрозой неаттестации» ---

def _add_group_with_student(conn, tg_id: int) -> None:
    """Завести группу со студентом и тремя предметами (для аттестации)."""
    from bot.attendance import service

    with transaction(conn):
        for para, subject in [(1, "История"), (2, "Литература"),
                              (3, "Физика")]:
            conn.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 1, ?, ?, 'Т.Т.', '', '', 'x')",
                (GROUP, para, subject),
            )
    code = service.create_group(conn, GROUP, 8899, "Абрамчик С.Г.")
    service.join_group(conn, tg_id, code, "Иванов И.И.")


def _add_truancy(conn, tg_id: int, days: list[str]) -> None:
    """Добавить прогулы (absent)."""
    with transaction(conn):
        for day in days:
            conn.execute(
                "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
                " full_name, status, marked_by, marked_at, method, subject)"
                " VALUES (?, ?, 1, ?, 'Иванов И.И.', 'absent', ?, 'x',"
                " 'starosta', 'История')",
                (GROUP, day, tg_id, 8899),
            )


async def test_weekly_warning_contains_attestation_block(conn) -> None:
    """Воскресное предупреждение содержит блок «Под угрозой неаттестации»."""
    _add_group_with_student(conn, USER_1)
    _add_truancy(conn, USER_1,
                 ["2026-09-28", "2026-09-29", "2026-09-30"])
    bot = ReminderBot()

    await lrs.send_truant_reminders(conn, bot, today=date(2026, 9, 30))

    text = _texts(bot)[0]
    assert "прогула за неделю" in text
    assert "Под угрозой неаттестации:" in text
    assert "История — 0/3" in text


async def test_weekly_warning_lists_need_more(conn) -> None:
    """В блоке видно, сколько пар не хватает по предмету."""
    _add_group_with_student(conn, USER_1)
    _add_truancy(conn, USER_1,
                 ["2026-09-28", "2026-09-29", "2026-09-30"])
    with transaction(conn):
        # Литература: 2 зачтённые пары (понедельники сентября).
        for day in ("2026-09-07", "2026-09-14"):
            conn.execute(
                "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
                " full_name, status, marked_by, marked_at, method, subject)"
                " VALUES (?, ?, 2, ?, 'Иванов И.И.', 'present', ?, 'x',"
                " 'self', 'Литература')",
                (GROUP, day, USER_1, USER_1),
            )
    bot = ReminderBot()

    await lrs.send_truant_reminders(conn, bot, today=date(2026, 9, 30))

    text = _texts(bot)[0]
    assert "Литература — 2/3 (нужно ещё 1)" in text


async def test_weekly_warning_without_group_has_no_block(conn) -> None:
    """Студент не в группе посещаемости — блока аттестации нет."""
    _add_truancy(conn, USER_1,
                 ["2026-09-28", "2026-09-29", "2026-09-30"])
    bot = ReminderBot()

    await lrs.send_truant_reminders(conn, bot, today=date(2026, 9, 30))

    text = _texts(bot)[0]
    assert "прогула за неделю" in text
    assert "Под угрозой неаттестации" not in text


async def test_weekly_warning_all_attested_has_no_block(conn) -> None:
    """Все предметы закрыты — блока аттестации нет."""
    _add_group_with_student(conn, USER_1)
    _add_truancy(conn, USER_1,
                 ["2026-09-28", "2026-09-29", "2026-09-30"])
    with transaction(conn):
        # Закрываем ВСЕ предметы группы: и заведённые фикстурой, и три
        # добавленных _add_group_with_student — иначе блок останется.
        for para, subject in [(1, "История"), (2, "Литература"),
                              (3, "Физика")]:
            for day in ("2026-09-07", "2026-09-14", "2026-09-21"):
                conn.execute(
                    "INSERT INTO attendance (group_name, date_iso, para,"
                    " tg_id, full_name, status, marked_by, marked_at, method,"
                    " subject) VALUES (?, ?, ?, ?, 'Иванов И.И.', 'present',"
                    " ?, 'x', 'self', ?)",
                    (GROUP, day, para, USER_1, USER_1, subject),
                )
    subjects = atts.get_group_subjects(conn, GROUP)
    with transaction(conn):
        for para, subject in enumerate(subjects, start=4):
            for day in ("2026-09-07", "2026-09-14", "2026-09-21"):
                conn.execute(
                    "INSERT INTO attendance (group_name, date_iso, para,"
                    " tg_id, full_name, status, marked_by, marked_at, method,"
                    " subject) VALUES (?, ?, ?, ?, 'Иванов И.И.', 'present',"
                    " ?, 'x', 'self', ?)",
                    (GROUP, day, para, USER_1, USER_1, subject),
                )
    bot = ReminderBot()

    await lrs.send_truant_reminders(conn, bot, today=date(2026, 9, 30))

    text = _texts(bot)[0]
    assert "Под угрозой неаттестации" not in text, \
        f"остались долги: {text}"
# --- учёт замен в напоминании ---
#
# Проблема, которую закрывают тесты: напоминание приходило и на отменённую
# пару, а предмет/кабинет не отражали замену.

def _add_substitution(conn, para: int, *, old: str = "ОД.07 Математика",
                      new: str = "", teacher: str = "", room: str = "",
                      cancelled: int = 0, self_study: int = 0) -> None:
    """Вставить замену на 1 пару в понедельник (дату фикстуры)."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO substitutions_cache (group_name, date_iso, para,"
            " old_subject, new_subject, teacher, room, is_cancelled,"
            " is_self_study, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'x')",
            (GROUP, MONDAY.isoformat(), para, old, new, teacher, room,
             cancelled, self_study),
        )


async def test_cancelled_lesson_not_reminded(conn) -> None:
    """Отменённая пара напоминания не вызывает."""
    _add_substitution(conn, 1, cancelled=1, new="")
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    assert sent == 0
    assert _texts(bot) == []


async def test_cancelled_only_for_one_group(conn) -> None:
    """Отмена у 25КАД не мешает напоминанию другому студенту той же пары.

    В фикстуре 2 пара есть только у 26КАД, поэтому проверяем парную ситуацию:
    отмена 1 пары у 25КАД, а напоминание о 1 паре нужно только ему — и оно
    не уходит. Второй студент (26КАД) в это время пары не имеет.
    """
    _add_substitution(conn, 1, cancelled=1, new="")
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    # USER_3 в 26КАД: 1 пары у его группы нет вовсе — напоминаний ноль.
    assert sent == 0
    assert _texts(bot) == []


async def test_substituted_lesson_uses_new_subject_and_room(conn) -> None:
    """Замена без отмены: в напоминании новый предмет и кабинет."""
    _add_substitution(conn, 1, new="ОД.11 Астрономия",
                      teacher="Кудрявцева П.А.", room="307А")
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    assert sent == 2
    text = _texts(bot)[0]
    assert "ОД.11 Астрономия" in text
    assert "307А" in text
    assert "ОД.07 Математика" not in text, "старый предмет не показываем"


async def test_unchanged_lesson_still_reminded(conn) -> None:
    """Без замен напоминание приходит как раньше."""
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    assert sent == 2
    assert "ОД.07 Математика" in _texts(bot)[0]


async def test_self_study_lesson_not_reminded(conn) -> None:
    """Самостоятельная работа — не пара с преподавателем, не напоминаем.

    Решение зафиксировано в :func:`check_reminders` комментарием: чтобы вернуть
    напоминания для самостоятельной работы, достаточно убрать ту проверку.
    """
    _add_substitution(conn, 1, new="Самостоятельная работа", self_study=1)
    bot = ReminderBot()

    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 8, 55, 10)
    )

    assert sent == 0
    assert _texts(bot) == []


async def test_other_para_reminder_unaffected_by_cancellation(conn) -> None:
    """Отмена 1 пары не мешает напоминанию о 3 паре."""
    _add_substitution(conn, 1, cancelled=1, new="")
    bot = ReminderBot()

    # 13:10 — «минус 5» для 3 пары (13:15).
    sent = await lrs.check_reminders(
        conn, bot, now=datetime(2026, 9, 28, 13, 10, 10)
    )

    assert sent == 2
    assert "3 пара" in _texts(bot)[0]