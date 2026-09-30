"""Тесты логики голосования за посещаемость (Часть 8).

Проверяется чистая логика :mod:`bot.attendance.vote_service` на in-memory БД:
порог, запись голосов, автозачёт, закрытие.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from bot.attendance import service
from bot.attendance import vote_service as vs
from bot.attendance import vote_texts as vt
from bot.config import KRASNOYARSK
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
STAROSTA = 5001
S1, S2, S3, S4 = 5002, 5003, 5004, 5005
CHAT_ID = -100500

# Среда 30.09.2026: 3 пара начинается в 13:15 (будни).
DAY = date(2026, 9, 30)
PARA = 3
SUBJECT = "Физика"


def _add_lesson(c, group: str, day_of_week: int, para: int,
                subject: str) -> None:
    """Добавить пару в расписание группы."""
    c.execute(
        "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
        " subject, teacher, room, week_type, updated_at)"
        " VALUES (?, ?, ?, ?, 'Т.Т.', '', '', 'x')",
        (group, day_of_week, para, subject),
    )


@pytest.fixture()
def conn(tmp_path: Path):
    """БД: группа из 5 человек (староста + 4 студента), пары по среде."""
    c = get_connection(tmp_path / "vote.db")
    apply_migrations(c)
    with transaction(c):
        for para, subject in [(1, "История"), (2, "Литература"),
                              (PARA, SUBJECT)]:
            _add_lesson(c, GROUP, 3, para, subject)
        code = service.create_group(c, GROUP, STAROSTA, "Абрамчик С.Г.")
        for index, tg_id in enumerate([S1, S2, S3, S4], start=1):
            service.join_group(c, tg_id, code, f"Студент{index} И.И.")
    yield c
    c.close()


def _start_poll(conn, *, chat_id: int = CHAT_ID,
                message_id: int | None = 10) -> int:
    """Запустить голосование по тестовой паре."""
    return vs.create_vote_poll(conn, GROUP, DAY.isoformat(), PARA,
                              chat_id=chat_id, started_by=STAROSTA,
                              message_id=message_id)


def _vote_for(conn, target: int, voter: int, name: str = "Студент1 И.И."):
    """Проголосовать за студента."""
    return vs.add_vote(conn, GROUP, DAY.isoformat(), PARA, target, name,
                       voter)


# --- порог ---

def test_threshold_half_up() -> None:
    """Порог — половина состава с округлением вверх."""
    assert vs.vote_threshold(5) == 3
    assert vs.vote_threshold(4) == 2
    assert vs.vote_threshold(20) == 10
    assert vs.vote_threshold(1) == 1
    assert vs.vote_threshold(2) == 1


def test_threshold_zero_group_is_one() -> None:
    """Пустая группа не делит на ноль."""
    assert vs.vote_threshold(0) == 1
    assert vs.vote_threshold(-3) == 1


def test_threshold_for_group_uses_real_size(conn) -> None:
    """Порог считается по фактическому составу группы (5 человек)."""
    assert vs.threshold_for_group(conn, GROUP) == 3


# --- closes_at ---

def test_closes_at_is_para_start_plus_45(conn) -> None:
    """Закрытие = начало пары + 45 минут (3 пара будни: 13:15 → 14:00)."""
    closes = vs.vote_closes_at(DAY, PARA)
    assert closes == datetime(2026, 9, 30, 14, 0, tzinfo=KRASNOYARSK)
    assert closes - datetime(2026, 9, 30, 13, 15,
                             tzinfo=KRASNOYARSK) == timedelta(minutes=45)


def test_closes_at_saturday_uses_saturday_bells() -> None:
    """В субботу звонки другие: 3 пара в 12:50 → закрытие 13:35."""
    saturday = date(2026, 10, 3)
    closes = vs.vote_closes_at(saturday, 3)
    assert closes == datetime(2026, 10, 3, 13, 35, tzinfo=KRASNOYARSK)


def test_closes_at_without_bells_returns_now() -> None:
    """Пары с таким номером нет — закрытие не повисает в прошлом."""
    closes = vs.vote_closes_at(DAY, 7)
    assert closes.tzinfo is not None


# --- create_poll ---

def test_create_poll_creates_row(conn) -> None:
    """create_vote_poll создаёт запись голосования."""
    poll_id = _start_poll(conn)

    poll = vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA)
    assert poll is not None
    assert int(poll["id"]) == poll_id
    assert poll["is_closed"] == 0
    assert int(poll["started_by"]) == STAROSTA


def test_create_poll_lists_missing_students(conn) -> None:
    """Список отсутствующих — все, кто не отметился."""
    missing = vs.missing_students(conn, GROUP, DAY.isoformat(), PARA)

    assert len(missing) == 5, "староста и четыре студента"
    assert [person["full_name"] for person in missing] == sorted(
        person["full_name"] for person in missing
    )


def test_missing_excludes_marked(conn) -> None:
    """Отметившийся через «Я на паре» в голосование не попадает."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
            " full_name, status, marked_by, marked_at, method, subject)"
            " VALUES (?, ?, ?, ?, 'Студент1 И.И.', 'present', ?, 'x',"
            " 'self', ?)",
            (GROUP, DAY.isoformat(), PARA, S1, S1, SUBJECT),
        )

    missing = vs.missing_students(conn, GROUP, DAY.isoformat(), PARA)
    assert S1 not in [person["tg_id"] for person in missing]
    assert len(missing) == 4


def test_create_poll_twice_keeps_one_row(conn) -> None:
    """Повторный запуск не создаёт второе голосование по той же паре."""
    first = _start_poll(conn)
    second = _start_poll(conn, message_id=99)

    assert first == second
    count = conn.execute(
        "SELECT COUNT(*) FROM attendance_vote_polls"
    ).fetchone()[0]
    assert count == 1


def test_vote_poll_exists(conn) -> None:
    """Признак «голосование уже есть»."""
    assert vs.vote_poll_exists(conn, GROUP, DAY.isoformat(), PARA) is False
    _start_poll(conn)
    assert vs.vote_poll_exists(conn, GROUP, DAY.isoformat(), PARA) is True
# --- голосование ---

def test_add_vote_success(conn) -> None:
    """Первый голос за студента записывается."""
    _start_poll(conn)

    result = _vote_for(conn, S1, S2)

    assert result["ok"] is True
    assert result["error"] is None
    assert result["count"] == 1
    assert result["attested"] is False


def test_add_vote_twice_same_voter_refused(conn) -> None:
    """Повторный голос того же человека за того же студента не проходит."""
    _start_poll(conn)
    _vote_for(conn, S1, S2)

    again = _vote_for(conn, S1, S2)

    assert again["ok"] is False
    assert again["error"] == "already_voted"
    assert again["count"] == 1, "счётчик не вырос"


def test_add_vote_self_allowed(conn) -> None:
    """Голос за себя разрешён (решение владельца проекта)."""
    _start_poll(conn)

    result = _vote_for(conn, S1, S1, "Студент1 И.И.")

    assert result["ok"] is True
    assert result["count"] == 1


def test_different_voters_counted(conn) -> None:
    """За одного студента могут голосовать разные люди."""
    _start_poll(conn)

    _vote_for(conn, S1, S1)
    _vote_for(conn, S1, S2)
    result = _vote_for(conn, S1, S3)

    assert result["count"] == 3


def test_vote_counts_by_target(conn) -> None:
    """Счётчики разделяются по студентам."""
    _start_poll(conn)
    _vote_for(conn, S1, S2)
    _vote_for(conn, S1, S3)
    _vote_for(conn, S4, S2)

    counts = vs.vote_counts(conn, GROUP, DAY.isoformat(), PARA)
    assert counts == {S1: 2, S4: 1}


def test_has_voted(conn) -> None:
    """Проверка «уже голосовал» работает по паре (кто → за кого)."""
    _start_poll(conn)
    _vote_for(conn, S1, S2)

    assert vs.has_voted(conn, GROUP, DAY.isoformat(), PARA, S1, S2) is True
    assert vs.has_voted(conn, GROUP, DAY.isoformat(), PARA, S1, S3) is False


# --- автозачёт по порогу ---

def test_threshold_reached_marks_attendance(conn) -> None:
    """3 голоса при 5 студентах → автоматическая отметка."""
    _start_poll(conn)

    _vote_for(conn, S1, S1)
    _vote_for(conn, S1, S2)
    result = _vote_for(conn, S1, S3)

    assert result["attested"] is True
    row = conn.execute(
        "SELECT status, method, subject FROM attendance"
        " WHERE tg_id = ? AND date_iso = ? AND para = ?",
        (S1, DAY.isoformat(), PARA),
    ).fetchone()
    assert row is not None
    assert row["status"] == "present"
    assert row["method"] == "vote"
    assert row["subject"] == SUBJECT, "предмет берётся из занятия"


def test_below_threshold_not_marked(conn) -> None:
    """2 голоса — порог (3) не набран, отметки нет."""
    _start_poll(conn)

    _vote_for(conn, S1, S1)
    result = _vote_for(conn, S1, S2)

    assert result["attested"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance"
    ).fetchone()[0] == 0


def test_threshold_four_students(conn) -> None:
    """Порог 2 при 4 студентах (ceil(4/2)) — проверяем на своей группе."""
    with transaction(conn):
        conn.execute("DELETE FROM students WHERE tg_id = ?", (S4,))

    assert vs.threshold_for_group(conn, GROUP) == 2

    _start_poll(conn)
    _vote_for(conn, S1, S1)
    result = _vote_for(conn, S1, S2)

    assert result["attested"] is True, "двух голосов хватает"


def test_attested_ids(conn) -> None:
    """attested_ids возвращает зачтённых именно голосованием."""
    _start_poll(conn)
    for voter in (S1, S2, S3):
        _vote_for(conn, S1, voter)

    assert vs.attested_ids(conn, GROUP, DAY.isoformat(), PARA) == {S1}


def test_existing_mark_not_overwritten(conn) -> None:
    """Уже поставленную отметку (например, late) голосование не затирает."""
    _start_poll(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
            " full_name, status, marked_by, marked_at, method, subject)"
            " VALUES (?, ?, ?, ?, 'Студент1 И.И.', 'late', ?, 'x',"
            " 'self', ?)",
            (GROUP, DAY.isoformat(), PARA, S1, S1, SUBJECT),
        )

    for voter in (S1, S2, S3):
        _vote_for(conn, S1, voter)

    row = conn.execute(
        "SELECT status, method FROM attendance WHERE tg_id = ?", (S1,)
    ).fetchone()
    assert row["status"] == "late", "статус сохранён"
    assert row["method"] == "self", "способ отметки не переписан"


def test_candidates_stable_after_attestation(conn) -> None:
    """Список кандидатов не теряет зачтённого: кнопка остаётся с галочкой."""
    _start_poll(conn)
    before = {person["tg_id"] for person in vs.candidates(
        conn, GROUP, DAY.isoformat(), PARA)}

    for voter in (S1, S2, S3):
        _vote_for(conn, S1, voter)

    after = {person["tg_id"] for person in vs.candidates(
        conn, GROUP, DAY.isoformat(), PARA)}
    assert after == before, "кнопки не должны исчезать после зачёта"
# --- закрытие ---

async def test_close_poll_attests_and_reports(conn) -> None:
    """Закрытие: набравшие порог зачтены, остальные в списке «не набрали»."""
    _start_poll(conn)
    for voter in (S1, S2, S3):
        _vote_for(conn, S1, voter)
    bot = FakeBot()

    result = await vs.close_poll(
        conn, bot, vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA))

    assert result["closed"] is True
    assert [p["tg_id"] for p in result["attested"]] == [S1]
    assert S1 not in [p["tg_id"] for p in result["not_attested"]]
    assert len(result["not_attested"]) == 4


async def test_close_poll_marks_final_message(conn) -> None:
    """Итоговое сообщение редактируется и содержит итог."""
    _start_poll(conn)
    for voter in (S1, S2, S3):
        _vote_for(conn, S1, voter)
    bot = FakeBot()

    await vs.close_poll(conn, bot,
                        vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA))

    edits = [m for m in bot.sent if m["method"] == "EditMessageText"]
    assert edits, "сообщение должно быть отредактировано"
    text = edits[-1]["text"]
    assert "Голосование закрыто" in text
    assert "Зачтены" in text
    assert "Не набрали голосов" in text
    assert "/mark" in text


async def test_close_poll_idempotent(conn) -> None:
    """Повторное закрытие ничего не делает."""
    _start_poll(conn)
    bot = FakeBot()
    poll = vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA)

    first = await vs.close_poll(conn, bot, poll)
    second = await vs.close_poll(conn, bot, poll)

    assert first["closed"] is True
    assert second["closed"] is False


async def test_close_poll_marks_borderline_votes(conn) -> None:
    """Голос, добравший порог ровно к закрытию, тоже зачитывается."""
    _start_poll(conn)
    _vote_for(conn, S1, S1)
    _vote_for(conn, S1, S2)
    # Третий голос пишем в обход сервиса: имитация голоса, пришедшего перед
    # самым закрытием (счётчик не успел дойти до автозачёта).
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance_votes (group_name, date_iso, para,"
            " target_tg_id, target_full_name, voter_tg_id, voted_at)"
            " VALUES (?, ?, ?, ?, 'Студент1 И.И.', ?, 'x')",
            (GROUP, DAY.isoformat(), PARA, S1, S3),
        )
    bot = FakeBot()

    result = await vs.close_poll(
        conn, bot, vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA))

    assert S1 in [p["tg_id"] for p in result["attested"]]


async def test_close_due_vote_polls_after_deadline(conn) -> None:
    """Автозакрытие срабатывает после closes_at."""
    _start_poll(conn)
    bot = FakeBot()
    after = datetime(2026, 9, 30, 15, 0, tzinfo=KRASNOYARSK)

    closed = await vs.close_due_vote_polls(conn, bot, after)

    assert closed == 1
    assert vs.get_vote_poll(conn, GROUP, DAY.isoformat(),
                            PARA)["is_closed"] == 1


async def test_close_due_vote_polls_before_deadline_keeps_open(conn) -> None:
    """До closes_at голосование остаётся открытым."""
    _start_poll(conn)
    bot = FakeBot()
    before = datetime(2026, 9, 30, 13, 30, tzinfo=KRASNOYARSK)

    closed = await vs.close_due_vote_polls(conn, bot, before)

    assert closed == 0
    assert vs.get_vote_poll(conn, GROUP, DAY.isoformat(),
                            PARA)["is_closed"] == 0


async def test_close_due_vote_polls_second_run_is_noop(conn) -> None:
    """Повторный проход автозакрытия ничего не закрывает."""
    _start_poll(conn)
    bot = FakeBot()
    after = datetime(2026, 9, 30, 15, 0, tzinfo=KRASNOYARSK)

    assert await vs.close_due_vote_polls(conn, bot, after) == 1
    assert await vs.close_due_vote_polls(conn, bot, after) == 0


async def test_close_due_survives_broken_closes_at(conn) -> None:
    """Испорченный closes_at не роняет проход."""
    _start_poll(conn)
    with transaction(conn):
        conn.execute(
            "UPDATE attendance_vote_polls SET closes_at = 'не дата'"
        )
    bot = FakeBot()

    closed = await vs.close_due_vote_polls(
        conn, bot, datetime(2026, 9, 30, 15, 0, tzinfo=KRASNOYARSK))

    assert closed == 0


def test_open_vote_polls_lists_only_open(conn) -> None:
    """get_open_vote_polls отдаёт только незакрытые."""
    _start_poll(conn)
    assert len(vs.get_open_vote_polls(conn)) == 1

    poll = vs.get_vote_poll(conn, GROUP, DAY.isoformat(), PARA)
    vs.close_vote_poll_row(conn, int(poll["id"]))
    assert vs.get_open_vote_polls(conn) == []


def test_close_vote_poll_row_idempotent(conn) -> None:
    """Закрытие строки идемпотентно."""
    poll_id = _start_poll(conn)
    assert vs.close_vote_poll_row(conn, poll_id) is True
    assert vs.close_vote_poll_row(conn, poll_id) is False


def test_set_vote_poll_message(conn) -> None:
    """message_id записывается после отправки сообщения."""
    poll_id = _start_poll(conn, message_id=None)

    assert vs.set_vote_poll_message(conn, poll_id, 777) is True
    assert int(vs.get_vote_poll(conn, GROUP, DAY.isoformat(),
                                PARA)["message_id"]) == 777
# --- тексты ---

def test_render_poll_shows_counts_and_threshold(conn) -> None:
    """В сообщении есть счётчики и порог."""
    people = vs.missing_students(conn, GROUP, DAY.isoformat(), PARA)
    text = vt.render_vote_poll(GROUP, DAY, PARA, SUBJECT, people,
                               {S1: 3}, 3, {S1}, total=5)

    assert "Голосование за посещаемость" in text
    assert "3 пара · 30.09 · Физика" in text
    assert "3 голоса ✓" in text
    assert "Порог зачёта: <b>3</b> голоса из 5" in text


def test_render_poll_closed_title() -> None:
    """Закрытое голосование получает свой заголовок."""
    text = vt.render_vote_poll(GROUP, DAY, PARA, SUBJECT, [], {}, 3, set(),
                               total=5, closed=True)
    assert "Голосование закрыто" in text


def test_attested_notice_wording() -> None:
    """Объявление о зачёте — с числом голосов."""
    assert vt.attested_notice("Иванов И.И.", 3) == (
        "✅ Иванов И.И. зачтён по голосованию (3 голоса)"
    )


def test_render_poll_escapes_html() -> None:
    """ФИО с HTML-спецсимволами экранируется."""
    people = [{"tg_id": 1, "full_name": "<b>Злой</b> & Ко"}]
    text = vt.render_vote_poll(GROUP, DAY, PARA, "<s>Физика</s>", people,
                               {}, 3, set(), total=1)
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    assert "&lt;s&gt;" in text
    assert "<s>Физика</s>" not in text


def test_render_final_escapes_html() -> None:
    """Итог тоже экранирует ФИО."""
    text = vt.render_vote_final(GROUP, DAY, PARA, SUBJECT,
                                [{"tg_id": 1, "full_name": "<i>X</i>"}], [])
    assert "&lt;i&gt;X&lt;/i&gt;" in text


def test_render_poll_empty_candidates() -> None:
    """Без кандидатов сообщение не падает."""
    text = vt.render_vote_poll(GROUP, DAY, PARA, SUBJECT, [], {}, 3, set(),
                               total=5)
    assert "Не за кого голосовать" in text


def test_vote_button_label_forms() -> None:
    """Подпись кнопки-счётчика согласует число."""
    from bot.attendance import vote_keyboards as vkb

    assert vkb.vote_button_label(1, False) == "1 голос"
    assert vkb.vote_button_label(3, False) == "3 голоса"
    assert vkb.vote_button_label(5, True) == "5 голосов ✓"


def test_confirm_callback_fits_telegram_limit(conn) -> None:
    """Callback голоса укладывается в лимит 64 байта."""
    from bot.attendance import vote_keyboards as vkb

    payload = vkb.confirm_callback("2026-09-30", 3, 999999999999)
    assert len(payload.encode("utf-8")) <= 64


def test_vote_kb_has_candidates_and_close(conn) -> None:
    """Клавиатура: кнопка на каждого кандидата плюс закрытие."""
    from bot.attendance import vote_keyboards as vkb

    people = [{"tg_id": S1, "full_name": "Студент1 И.И."},
              {"tg_id": S2, "full_name": "Студент2 И.И."}]
    kb = vkb.vote_kb(GROUP, DAY.isoformat(), PARA, people, {S1: 3}, {S1})
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    labels = [b.text for row in kb.inline_keyboard for b in row]

    assert data == [
        f"vote:confirm:{DAY.isoformat()}:{PARA}:{S1}",
        f"vote:confirm:{DAY.isoformat()}:{PARA}:{S2}",
        f"vote:close:{DAY.isoformat()}:{PARA}",
    ]
    assert labels[0] == "✅ Студент1 И.И. — 3 голоса ✓"
    assert labels[1] == "✅ Студент2 И.И. — 0 голосов"
    assert labels[-1] == "🔒 Закрыть досрочно"