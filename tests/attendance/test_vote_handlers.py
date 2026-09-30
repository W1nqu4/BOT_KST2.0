"""Тесты обработчиков голосования (Часть 8).

Сквозные сценарии через диспетчер: запуск старостой, голосование студентов,
автозачёт по порогу, закрытие.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from bot.attendance import attendance_service as att_svc
from bot.attendance import service
from bot.attendance import vote_handlers as vh
from bot.attendance import vote_keyboards as vote_kb
from bot.attendance import vote_service as vs
from bot.db import get_connection, transaction
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
STAROSTA = 7001
S1, S2, S3, S4 = 7002, 7003, 7004, 7005
CHAT_ID = -100700
PARA = 3
SUBJECT = "Физика"


@pytest.fixture()
def conn(tmp_path: Path):
    """БД: группа из 5 человек, чат привязан, пары по среде."""
    c = get_connection(tmp_path / "vote_handlers.db")
    apply_migrations(c)
    with transaction(c):
        for para, subject in [(1, "История"), (2, "Литература"),
                              (PARA, SUBJECT)]:
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 3, ?, ?, 'Т.Т.', '', '', 'x')",
                (GROUP, para, subject),
            )
        c.execute(
            "INSERT INTO group_chats (chat_id, chat_title, chat_type,"
            " group_name, added_by, added_at, notifications_enabled)"
            " VALUES (?, 'Группа', 'group', ?, ?, 'x', 1)",
            (CHAT_ID, GROUP, STAROSTA),
        )
        code = service.create_group(c, GROUP, STAROSTA, "Абрамчик С.Г.")
        for index, tg_id in enumerate([S1, S2, S3, S4], start=1):
            service.join_group(c, tg_id, code, f"Студент{index} И.И.")
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


def _update(text: str, tg_id: int = STAROSTA):
    """Апдейт с текстовым сообщением."""
    from aiogram.types import Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = STAROSTA):
    """Апдейт с нажатием inline-кнопки."""
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


def _texts(bot: FakeBot) -> list[str]:
    """Тексты всех сообщений (send + edit)."""
    return [m["text"] for m in bot.sent if m["text"]]


def _buttons(bot: FakeBot) -> list[str]:
    """Подписи кнопок последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.text for row in markup.inline_keyboard for b in row]
    return []


def _chat_buttons(bot: FakeBot) -> list[str]:
    """Подписи кнопок сообщения, ушедшего в чат группы."""
    for message in bot.sent:
        markup = message["reply_markup"]
        if (message.get("chat_id") == CHAT_ID and markup is not None
                and getattr(markup, "inline_keyboard", None)):
            return [b.text for row in markup.inline_keyboard for b in row]
    return []


def _redirect_today(monkeypatch, day: date) -> None:
    """Подменить «сегодня» для парсера дат и кнопки запуска.

    Нужно там, где тест идёт через ``/vote сегодня``: иначе результат
    зависел бы от дня прогона. Остальные тесты используют явную дату.
    """
    real_datetime = datetime

    class FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(day.year, day.month, day.day, 14, 0,
                                 tzinfo=tz)

    monkeypatch.setattr(vh, "datetime", FakeDateTime)


def _start_vote(conn) -> int:
    """Создать голосование напрямую (короче, чем гонять /vote)."""
    return vs.create_vote_poll(conn, GROUP, DATE_ISO, PARA, chat_id=CHAT_ID,
                               started_by=STAROSTA, message_id=55)


# Среда 30.09.2026: 3 пара будни (13:15). Дата задаётся явно, чтобы тесты
# не зависели от дня прогона.
WEDNESDAY = date(2026, 9, 30)
DATE_ISO = WEDNESDAY.isoformat()
# --- parse_vote_args ---

@pytest.mark.parametrize(("raw", "expected_date", "expected_para"), [
    ("сегодня 3", "2026-09-30", 3),
    ("29.09 2", "2026-09-29", 2),
    ("30.09.2026 1", "2026-09-30", 1),
    ("2026-09-30 4", "2026-09-30", 4),
])
def test_parse_vote_args_ok(raw: str, expected_date: str,
                            expected_para: int) -> None:
    """Разбор даты и пары: «сегодня», dd.mm, dd.mm.yyyy, ISO."""
    assert vh.parse_vote_args(raw, today=WEDNESDAY) == (expected_date,
                                                        expected_para)


@pytest.mark.parametrize("raw", [
    "", "сегодня", "3", "сегодня abc", "неверно 3", "сегодня 0",
    "сегодня 9", "сегодня 3 лишнее",
])
def test_parse_vote_args_bad(raw: str) -> None:
    """Мусор не разбирается (None), а не падает."""
    assert vh.parse_vote_args(raw, today=WEDNESDAY) is None


@pytest.mark.parametrize("raw", ["30.02 1", "31.04 2", "00.09 1", "45.01 1"])
def test_parse_vote_args_impossible_date(raw: str) -> None:
    """Несуществующая дата (30 февраля и т.п.) — не ошибка, а None."""
    assert vh.parse_vote_args(raw, today=WEDNESDAY) is None


def test_parse_vote_args_past_year() -> None:
    """«29.12» в январе — это декабрь прошлого года, а не будущего."""
    assert vh.parse_vote_args("29.12 2", today=date(2027, 1, 10)) == (
        "2026-12-29", 2
    )


def test_parse_vote_args_future_date_in_year() -> None:
    """«15.11» в сентябре — ноябрь текущего года (в пределах полугода)."""
    assert vh.parse_vote_args("15.11 1", today=date(2026, 9, 30)) == (
        "2026-11-15", 1
    )


# --- is_vote_admin ---

def test_is_vote_admin_starosta(conn) -> None:
    """Староста может запускать голосование."""
    assert vh.is_vote_admin(conn, STAROSTA) is True


def test_is_vote_admin_student_denied(conn) -> None:
    """Обычный студент — нет."""
    assert vh.is_vote_admin(conn, S1) is False


def test_is_vote_admin_outsider_denied(conn) -> None:
    """Посторонний (не в группе) — нет."""
    assert vh.is_vote_admin(conn, 999999) is False


# --- /vote ---

async def test_vote_command_denied_for_student(dp, conn) -> None:
    """/vote от студента — отказ."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}", S1))

    body = " ".join(_texts(bot))
    assert "Только староста" in body or "староста или зам" in body.lower()


async def test_vote_command_without_args_shows_format(dp, conn) -> None:
    """/vote без аргументов — подсказка формата."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/vote"))

    body = " ".join(_texts(bot))
    assert "Формат" in body
    assert "/vote сегодня 3" in body


async def test_vote_command_bad_args_shows_format(dp, conn) -> None:
    """Кривые аргументы — та же подсказка."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/vote когда-то там"))

    body = " ".join(_texts(bot))
    assert "Формат" in body


async def test_vote_command_no_lesson(dp, conn) -> None:
    """/vote на пару, которой нет в расписании."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} 6"))

    body = " ".join(_texts(bot))
    assert "нет такой пары" in body
async def test_vote_command_starts_poll_in_chat(dp, conn) -> None:
    """/vote старосты отправляет голосование в чат группы."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))

    chat_texts = [m["text"] for m in bot.sent
                  if m.get("chat_id") == CHAT_ID and m["text"]]
    assert chat_texts, "сообщение должно уйти в чат группы"
    assert "Голосование за посещаемость" in chat_texts[0]

    labels = " | ".join(_chat_buttons(bot))
    assert "Студент1 И.И." in labels
    assert "Закрыть досрочно" in labels

    poll = vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA)
    assert poll is not None
    assert poll["is_closed"] == 0
    assert int(poll["chat_id"]) == CHAT_ID


async def test_vote_command_reports_start_to_starosta(dp, conn) -> None:
    """Староста получает подтверждение с порогом."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))

    private = [m["text"] for m in bot.sent
               if m.get("chat_id") == STAROSTA and m["text"]]
    assert any("Голосование запущено" in text for text in private)
    assert any("порог зачёта" in text for text in private)


async def test_vote_command_empty_missing(dp, conn) -> None:
    """Все отметились — голосовать не за кого."""
    with transaction(conn):
        for tg_id, name in [(STAROSTA, "Абрамчик С.Г."),
                            (S1, "Студент1 И.И."), (S2, "Студент2 И.И."),
                            (S3, "Студент3 И.И."), (S4, "Студент4 И.И.")]:
            conn.execute(
                "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
                " full_name, status, marked_by, marked_at, method, subject)"
                " VALUES (?, ?, ?, ?, ?, 'present', ?, 'x', 'self', ?)",
                (GROUP, DATE_ISO, PARA, tg_id, name, tg_id, SUBJECT),
            )
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))

    assert any("Все отметились" in text for text in _texts(bot))


async def test_vote_command_twice_refused(dp, conn) -> None:
    """Повторный запуск по той же паре отклоняется."""
    bot = FakeBot()
    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))
    bot.sent.clear()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))

    assert any("уже есть голосование" in text for text in _texts(bot))


async def test_vote_command_without_chat(dp, conn) -> None:
    """Без привязанного чата запуск невозможен с понятным текстом."""
    with transaction(conn):
        conn.execute("DELETE FROM group_chats")
    bot = FakeBot()

    await dp.feed_update(bot, _update(f"/vote {DATE_ISO} {PARA}"))

    assert any("нет привязанного чата" in text for text in _texts(bot))


async def test_vote_start_button_lists_paras(dp, conn, monkeypatch) -> None:
    """Кнопка «📣 Запустить голосование» показывает пары дня."""
    _redirect_today(monkeypatch, WEDNESDAY)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(vote_kb.CB_START))

    body = " ".join(_texts(bot))
    assert "Выбери пару" in body
    labels = " | ".join(_buttons(bot))
    assert "3 пара" in labels, "пара среды в списке"


async def test_vote_start_button_denied_for_student(dp, conn) -> None:
    """Студенту кнопка запуска недоступна."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(vote_kb.CB_START, S1))

    alerts = [m for m in bot.sent if m["method"] == "AnswerCallbackQuery"]
    assert alerts, "должен быть ответ на callback"


async def test_vote_pick_starts_poll(dp, conn, monkeypatch) -> None:
    """Выбор пары из списка запускает голосование."""
    _redirect_today(monkeypatch, WEDNESDAY)
    bot = FakeBot()
    await dp.feed_update(bot, _callback(vote_kb.CB_START))
    bot.sent.clear()

    await dp.feed_update(
        bot, _callback(f"{vote_kb.CB_PICK_PREFIX}{DATE_ISO}:{PARA}"))

    assert vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA) is not None
    assert any("Голосование за посещаемость" in m["text"]
               for m in bot.sent
               if m.get("chat_id") == CHAT_ID and m["text"])
# --- голосование студентов ---

def _confirm(target: int) -> str:
    """Callback голоса за студента."""
    return f"{vote_kb.CB_CONFIRM_PREFIX}{DATE_ISO}:{PARA}:{target}"


async def test_vote_button_increments_counter(dp, conn) -> None:
    """Нажатие кнопки увеличивает счётчик и обновляет сообщение."""
    _start_vote(conn)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(_confirm(S1), S2))

    counts = vs.vote_counts(conn, GROUP, DATE_ISO, PARA)
    assert counts == {S1: 1}
    edits = [m for m in bot.sent if m["method"] == "EditMessageText"]
    assert edits, "сообщение перерисовано"
    assert "1 голос" in edits[-1]["text"]


async def test_vote_button_twice_refused(dp, conn) -> None:
    """Повторное нажатие тем же студентом — «уже подтвердил»."""
    _start_vote(conn)
    bot = FakeBot()
    await dp.feed_update(bot, _callback(_confirm(S1), S2))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(_confirm(S1), S2))

    alerts = [m for m in bot.sent if m["method"] == "AnswerCallbackQuery"]
    assert alerts
    assert vs.vote_counts(conn, GROUP, DATE_ISO, PARA) == {S1: 1}


async def test_vote_for_self_allowed(dp, conn) -> None:
    """Голос за себя проходит."""
    _start_vote(conn)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(_confirm(S1), S1))

    assert vs.vote_counts(conn, GROUP, DATE_ISO, PARA) == {S1: 1}


async def test_vote_reaching_threshold_marks(dp, conn) -> None:
    """Достижение порога ставит отметку method='vote' и объявляет в чат."""
    _start_vote(conn)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(_confirm(S1), S1))
    await dp.feed_update(bot, _callback(_confirm(S1), S2))
    bot.sent.clear()
    await dp.feed_update(bot, _callback(_confirm(S1), S3))

    row = conn.execute(
        "SELECT status, method FROM attendance"
        " WHERE tg_id = ? AND date_iso = ? AND para = ?",
        (S1, DATE_ISO, PARA),
    ).fetchone()
    assert row is not None
    assert row["status"] == "present"
    assert row["method"] == "vote"

    chat_texts = [m["text"] for m in bot.sent
                  if m.get("chat_id") == CHAT_ID and m["text"]]
    assert any("зачтён по голосованию" in text for text in chat_texts)


async def test_vote_from_other_group_denied(dp, conn) -> None:
    """Голосовать можно только в своей группе."""
    _start_vote(conn)
    bot = FakeBot()

    # Посторонний не состоит в students — группа не находится.
    await dp.feed_update(bot, _callback(_confirm(S1), 999999))

    assert vs.vote_counts(conn, GROUP, DATE_ISO, PARA) == {}


async def test_vote_unknown_poll_answered(dp, conn) -> None:
    """Голос по несуществующему голосованию — вежливый отказ."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback(_confirm(S1), S2))

    assert vs.vote_counts(conn, GROUP, DATE_ISO, PARA) == {}


async def test_vote_closed_poll_denied(dp, conn) -> None:
    """После закрытия кнопки не работают."""
    poll_id = _start_vote(conn)
    vs.close_vote_poll_row(conn, poll_id)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(_confirm(S1), S2))

    assert vs.vote_counts(conn, GROUP, DATE_ISO, PARA) == {}
# --- кнопка закрытия ---

async def test_close_button_closes_poll(dp, conn) -> None:
    """«🔒 Закрыть досрочно» закрывает голосование и показывает итог."""
    _start_vote(conn)
    for voter in (S1, S2, S3):
        vs.add_vote(conn, GROUP, DATE_ISO, PARA, S1, "Студент1 И.И.", voter)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(
        f"{vote_kb.CB_CLOSE_PREFIX}{DATE_ISO}:{PARA}"))

    assert vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA)["is_closed"] == 1
    edits = [m for m in bot.sent if m["method"] == "EditMessageText"]
    assert edits
    assert "Голосование закрыто" in edits[-1]["text"]


async def test_close_button_denied_for_student(dp, conn) -> None:
    """Студент закрыть голосование не может."""
    _start_vote(conn)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(
        f"{vote_kb.CB_CLOSE_PREFIX}{DATE_ISO}:{PARA}", S1))

    assert vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA)["is_closed"] == 0


async def test_close_button_second_time_alert(dp, conn) -> None:
    """Повторное нажатие «Закрыть» — alert «уже закрыто»."""
    poll_id = _start_vote(conn)
    vs.close_vote_poll_row(conn, poll_id)
    bot = FakeBot()

    await dp.feed_update(bot, _callback(
        f"{vote_kb.CB_CLOSE_PREFIX}{DATE_ISO}:{PARA}"))

    alerts = [m for m in bot.sent if m["method"] == "AnswerCallbackQuery"]
    assert alerts


# --- интеграция с фоновым проходом ---

async def test_tick_closes_due_votes(conn) -> None:
    """Общий tick посещаемости закрывает истёкшие голосования."""
    _start_vote(conn)
    bot = FakeBot()
    # 15:00 — позже closes_at (14:00 для 3 пары будни).
    moment = datetime(2026, 9, 30, 15, 0, tzinfo=att_svc.KRASNOYARSK)

    result = await att_svc.tick(conn, bot, moment)

    assert result["votes_closed"] == 1
    assert vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA)["is_closed"] == 1


async def test_tick_keeps_future_votes_open(conn) -> None:
    """До срока tick голосование не закрывает."""
    _start_vote(conn)
    bot = FakeBot()
    moment = datetime(2026, 9, 30, 13, 30, tzinfo=att_svc.KRASNOYARSK)

    result = await att_svc.tick(conn, bot, moment)

    assert result["votes_closed"] == 0
    assert vs.get_vote_poll(conn, GROUP, DATE_ISO, PARA)["is_closed"] == 0


async def test_tick_result_has_votes_key(conn) -> None:
    """Ключ votes_closed появился в результате tick (совместимость)."""
    bot = FakeBot()
    moment = datetime(2026, 9, 30, 8, 0, tzinfo=att_svc.KRASNOYARSK)

    result = await att_svc.tick(conn, bot, moment)

    assert "closed" in result and "started" in result
    assert "votes_closed" in result


def test_vote_in_help() -> None:
    """/help упоминает /vote."""
    from bot.handlers.help import HELP_TEXT

    assert "/vote — запустить голосование" in HELP_TEXT


def test_starosta_kb_has_vote_button() -> None:
    """У старосты в «Моей группе» есть кнопка запуска голосования."""
    from bot.attendance import keyboards as att_kb

    buttons = [b for row in att_kb.my_group_starosta_kb().inline_keyboard
               for b in row]
    labels = [b.text for b in buttons]
    assert "📣 Запустить голосование" in labels
    assert vote_kb.CB_START in [b.callback_data for b in buttons]


def test_student_kb_has_no_vote_button() -> None:
    """У студента кнопки голосования нет."""
    from bot.attendance import keyboards as att_kb

    labels = [b.text for row in att_kb.my_group_student_kb().inline_keyboard
              for b in row]
    assert "📣 Запустить голосование" not in labels