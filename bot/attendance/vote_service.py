"""Логика голосования группы за посещаемость (миграция 13).

Зачем: если студент не успел нажать «Я на паре» во время пары, староста
запускает голосование в чате группы. Каждый жмёт на фамилию того, кого
подтверждает; когда за студента набирается половина группы, он зачитывается
автоматически (``method='vote'``).

Решения владельца проекта, зашитые в код:

- **голос за себя разрешён** — студент сам подтверждает, что был;
- **порог автозачёта — половина группы с округлением вверх**
  (:func:`vote_threshold`): 5 студентов → 3, 4 → 2, 20 → 10;
- **закрытие** — автоматически через :data:`VOTE_DURATION_MINUTES` минут
  после начала пары либо вручную старостой.

Оценок здесь нет: голосование ставит только ``present``, прогул по-прежнему
фиксирует староста вручную через ``/mark``.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta

from bot.attendance import attendance_db as att
from bot.attendance import attendance_service as att_svc
from bot.attendance import db as att_db
from bot.attendance import vote_texts as vtext
from bot.attendance.models import METHOD_VOTE, STATUS_PRESENT
from bot.config import KRASNOYARSK

logger = logging.getLogger(__name__)

# Через сколько минут после начала пары голосование закрывается само.
VOTE_DURATION_MINUTES = 45


def vote_threshold(students_count: int) -> int:
    """Сколько голосов нужно для зачёта: половина группы вверх.

    Args:
        students_count: число студентов в группе.

    Returns:
        Порог голосов. Для пустой группы — 1 (зачитывать некого, но и
        делить на ноль нельзя).
    """
    if students_count <= 0:
        return 1
    return math.ceil(students_count / 2)


def vote_closes_at(day: date, para: int) -> datetime:
    """Момент закрытия голосования: начало пары + ``VOTE_DURATION_MINUTES``.

    Отсчёт идёт от **начала** пары по звонкам дня (в субботу они другие).
    Если звонков нет (пары в этот день не существует), берём текущий момент
    — голосование закроется на следующем проходе цикла, а не повиснет.

    Args:
        day: дата пары.
        para: номер пары.

    Returns:
        Момент закрытия по Красноярску.
    """
    start: time | None = att_svc.para_start_time(day, para)
    if start is None:
        return datetime.now(KRASNOYARSK)
    moment = datetime.combine(day, start).replace(tzinfo=KRASNOYARSK)
    return moment + timedelta(minutes=VOTE_DURATION_MINUTES)


def missing_students(conn, group: str, date_iso: str,
                     para: int) -> list[dict]:
    """Студенты группы без отметки на пару (кандидаты в голосование).

    Args:
        conn: соединение SQLite.
        group: группа.
        date_iso: дата пары.
        para: номер пары.

    Returns:
        Список ``[{'tg_id', 'full_name'}]`` по алфавиту.
    """
    marks = att.get_attendance_list(conn, group, date_iso, para)
    marked = {int(mark["tg_id"]) for mark in marks}
    return [
        {"tg_id": int(student["tg_id"]),
         "full_name": str(student["full_name"])}
        for student in att_db.get_group_students(conn, group)
        if int(student["tg_id"]) not in marked
    ]


def _transaction(conn):
    """Ленивый ``transaction`` (избегаем цикла импортов на уровне модуля)."""
    from bot.db import transaction

    return transaction(conn)
# --- БД голосований ---

def create_vote_poll(conn, group_name: str, date_iso: str, para: int,
                     chat_id: int, started_by: int,
                     closes_at: str | None = None,
                     message_id: int | None = None,
                     now: str | None = None) -> int:
    """Создать голосование на пару. Возвращает id голосования.

    Повторный вызов для той же пары не дублирует запись, а обновляет
    ``chat_id``/``message_id``/``closes_at`` — как в опросах «кто на паре».
    """
    from bot.attendance.db import _now

    moment = now or _now()
    closes = closes_at or vote_closes_at(
        date.fromisoformat(date_iso), para
    ).isoformat(timespec="seconds")

    with _transaction(conn):
        conn.execute(
            "INSERT INTO attendance_vote_polls"
            " (group_name, date_iso, para, chat_id, message_id, started_by,"
            "  started_at, closes_at, is_closed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)"
            " ON CONFLICT(group_name, date_iso, para) DO UPDATE SET"
            "   chat_id = excluded.chat_id,"
            "   message_id = excluded.message_id,"
            "   started_by = excluded.started_by,"
            "   closes_at = excluded.closes_at",
            (group_name, date_iso, para, chat_id, message_id, started_by,
             moment, closes),
        )
        row = conn.execute(
            "SELECT id FROM attendance_vote_polls"
            " WHERE group_name = ? AND date_iso = ? AND para = ?",
            (group_name, date_iso, para),
        ).fetchone()
    return int(row["id"])


def get_vote_poll(conn, group_name: str, date_iso: str,
                  para: int) -> dict | None:
    """Голосование по паре или None."""
    row = conn.execute(
        "SELECT * FROM attendance_vote_polls"
        " WHERE group_name = ? AND date_iso = ? AND para = ?",
        (group_name, date_iso, para),
    ).fetchone()
    return dict(row) if row is not None else None


def vote_poll_exists(conn, group_name: str, date_iso: str, para: int) -> bool:
    """Есть ли голосование по этой паре (любое — открытое или закрытое)."""
    return get_vote_poll(conn, group_name, date_iso, para) is not None


def get_open_vote_polls(conn) -> list[dict]:
    """Все незакрытые голосования (для автозакрытия по времени)."""
    rows = conn.execute(
        "SELECT * FROM attendance_vote_polls WHERE is_closed = 0"
        " ORDER BY date_iso, para"
    ).fetchall()
    return [dict(row) for row in rows]


def set_vote_poll_message(conn, poll_id: int, message_id: int) -> bool:
    """Записать id сообщения голосования (после отправки)."""
    with _transaction(conn):
        cursor = conn.execute(
            "UPDATE attendance_vote_polls SET message_id = ? WHERE id = ?",
            (message_id, poll_id),
        )
    return cursor.rowcount > 0


def close_vote_poll_row(conn, poll_id: int) -> bool:
    """Пометить голосование закрытым (идемпотентно).

    Returns:
        True, если запись была открыта и её закрыли; False, если она уже
        закрыта (повторный вызов ничего не меняет).
    """
    with _transaction(conn):
        cursor = conn.execute(
            "UPDATE attendance_vote_polls SET is_closed = 1"
            " WHERE id = ? AND is_closed = 0",
            (poll_id,),
        )
    return cursor.rowcount > 0


# --- голоса ---

def get_votes(conn, group_name: str, date_iso: str,
              para: int) -> list[dict]:
    """Все голоса по паре."""
    rows = conn.execute(
        "SELECT * FROM attendance_votes"
        " WHERE group_name = ? AND date_iso = ? AND para = ?"
        " ORDER BY target_full_name COLLATE NOCASE, voted_at",
        (group_name, date_iso, para),
    ).fetchall()
    return [dict(row) for row in rows]


def vote_counts(conn, group_name: str, date_iso: str,
                para: int) -> dict[int, int]:
    """Счётчик голосов по каждому студенту: ``{tg_id: количество}``."""
    rows = conn.execute(
        "SELECT target_tg_id, COUNT(*) AS cnt FROM attendance_votes"
        " WHERE group_name = ? AND date_iso = ? AND para = ?"
        " GROUP BY target_tg_id",
        (group_name, date_iso, para),
    ).fetchall()
    return {int(row["target_tg_id"]): int(row["cnt"]) for row in rows}


def has_voted(conn, group_name: str, date_iso: str, para: int,
              target_tg_id: int, voter_tg_id: int) -> bool:
    """Голосовал ли уже этот человек за этого студента."""
    row = conn.execute(
        "SELECT 1 FROM attendance_votes"
        " WHERE group_name = ? AND date_iso = ? AND para = ?"
        "   AND target_tg_id = ? AND voter_tg_id = ?",
        (group_name, date_iso, para, target_tg_id, voter_tg_id),
    ).fetchone()
    return row is not None
# --- голосование ---

def add_vote(conn, group_name: str, date_iso: str, para: int,
             target_tg_id: int, target_full_name: str, voter_tg_id: int,
             now: str | None = None) -> dict:
    """Записать голос за студента (голос за себя разрешён).

    Проверки существования голосования и группы — на вызывающей стороне:
    здесь только запись и перевод «набрал порог» в отметку.

    Args:
        conn: соединение SQLite.
        group_name: группа.
        date_iso: дата пары.
        para: номер пары.
        target_tg_id: за кого голосуют.
        target_full_name: ФИО на момент голосования (снимок).
        voter_tg_id: кто голосует.
        now: момент в ISO (для тестов).

    Returns:
        ``{'ok': bool, 'error': str | None, 'count': int,
        'attested': bool}``. ``error`` — ``already_voted``.
    """
    from bot.attendance.db import _now

    if has_voted(conn, group_name, date_iso, para, target_tg_id, voter_tg_id):
        # UNIQUE в БД тоже защищает, но проверка даёт понятный ответ без
        # ловли исключения (и без ложного «сбой БД» в логах).
        return {
            "ok": False,
            "error": "already_voted",
            "count": current_votes(conn, group_name, date_iso, para,
                                   target_tg_id),
            "attested": False,
        }

    with _transaction(conn):
        conn.execute(
            "INSERT INTO attendance_votes"
            " (group_name, date_iso, para, target_tg_id, target_full_name,"
            "  voter_tg_id, voted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_name, date_iso, para, target_tg_id, target_full_name,
             voter_tg_id, now or _now()),
        )

    count = current_votes(conn, group_name, date_iso, para, target_tg_id)
    attested = _maybe_attest(conn, group_name, date_iso, para, target_tg_id,
                             target_full_name, count)
    logger.info("vote accepted",
                extra={"group": group_name, "date": date_iso, "para": para,
                       "target": target_tg_id, "voter": voter_tg_id,
                       "count": count, "attested": attested})
    return {"ok": True, "error": None, "count": count, "attested": attested}


def current_votes(conn, group_name: str, date_iso: str, para: int,
                  target_tg_id: int) -> int:
    """Сколько голосов набрал студент."""
    row = conn.execute(
        "SELECT COUNT(*) FROM attendance_votes"
        " WHERE group_name = ? AND date_iso = ? AND para = ?"
        "   AND target_tg_id = ?",
        (group_name, date_iso, para, target_tg_id),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def threshold_for_group(conn, group_name: str) -> int:
    """Порог голосов для группы: половина состава вверх."""
    return vote_threshold(len(att_db.get_group_students(conn, group_name)))


def _maybe_attest(conn, group_name: str, date_iso: str, para: int,
                  target_tg_id: int, target_full_name: str,
                  count: int) -> bool:
    """Зачесть студента, если голосов хватает.

    Отметка ставится только если её ещё нет: ``mark_attendance`` умеет
    перезаписывать статус, а затирать уже поставленную отметку (например,
    ``late`` от самопроверки) голосованием нельзя.

    Returns:
        True, если отметка поставлена этим вызовом.
    """
    if count < threshold_for_group(conn, group_name):
        return False
    if att.attendance_exists(conn, group_name, date_iso, para, target_tg_id):
        return False

    day = date.fromisoformat(date_iso)
    lesson = att_svc.get_lesson_for_para(conn, group_name, day, para)
    att.mark_attendance(
        conn, group_name, date_iso, para, target_tg_id, target_full_name,
        status=STATUS_PRESENT, marked_by=target_tg_id, method=METHOD_VOTE,
        subject=str(lesson["subject"]) if lesson else None,
    )
    logger.info("student attested by vote",
                extra={"group": group_name, "date": date_iso, "para": para,
                       "tg_id": target_tg_id, "votes": count})
    return True


def attested_ids(conn, group_name: str, date_iso: str,
                 para: int) -> set[int]:
    """Кого из группы уже зачли отметкой с способом ``vote``."""
    rows = conn.execute(
        "SELECT tg_id FROM attendance"
        " WHERE group_name = ? AND date_iso = ? AND para = ? AND method = ?",
        (group_name, date_iso, para, METHOD_VOTE),
    ).fetchall()
    return {int(row["tg_id"]) for row in rows}


def candidates(conn, group_name: str, date_iso: str,
               para: int) -> list[dict]:
    """Кандидаты голосования для кнопок.

    Объединение трёх множеств:

    - отсутствующие сейчас (``missing_students``);
    - те, за кого уже голосовали (иначе кнопка исчезала бы после первого
      голоса — счётчик негде было бы показать);
    - зачтённые голосованием (кнопка остаётся с галочкой).

    Так список кнопок стабилен и не «прыгает» при каждом обновлении
    сообщения, а новых колонок в схеме не требуется.

    Returns:
        Список ``[{'tg_id', 'full_name'}]`` по алфавиту.
    """
    students = {int(student["tg_id"]): str(student["full_name"])
                for student in att_db.get_group_students(conn, group_name)}

    known: dict[int, str] = {}
    for student in missing_students(conn, group_name, date_iso, para):
        known[int(student["tg_id"])] = str(student["full_name"])
    for vote in get_votes(conn, group_name, date_iso, para):
        tg_id = int(vote["target_tg_id"])
        known.setdefault(tg_id, str(vote["target_full_name"]))
    for tg_id in attested_ids(conn, group_name, date_iso, para):
        known.setdefault(tg_id, students.get(tg_id, "без имени"))

    return [{"tg_id": tg_id, "full_name": known[tg_id]}
            for tg_id in sorted(known, key=lambda key: known[key])]
# --- сообщение и закрытие ---

def _subject(conn, group: str, day: date, para: int) -> str:
    """Название предмета пары (пустая строка, если пары нет)."""
    lesson = att_svc.get_lesson_for_para(conn, group, day, para)
    return str(lesson["subject"]) if lesson else ""


async def update_vote_message(conn, bot, poll: dict) -> bool:
    """Обновить сообщение голосования: счётчики голосов по кандидатам.

    Редактирование может не пройти (сообщение удалили, текст не изменился) —
    это не ошибка, поэтому ``TelegramBadRequest`` глушим.

    Returns:
        True, если сообщение отредактировано.
    """
    from aiogram.exceptions import TelegramBadRequest

    from bot.attendance import vote_keyboards as vote_kb_mod

    message_id = poll.get("message_id")
    if not message_id:
        return False

    group = str(poll["group_name"])
    date_iso = str(poll["date_iso"])
    para = int(poll["para"])
    day = date.fromisoformat(date_iso)
    closed = bool(poll.get("is_closed"))

    people = candidates(conn, group, date_iso, para)
    votes = vote_counts(conn, group, date_iso, para)
    attested = attested_ids(conn, group, date_iso, para)
    threshold = threshold_for_group(conn, group)

    text = vtext.render_vote_poll(
        group=group, day=day, para=para,
        subject=_subject(conn, group, day, para),
        candidates=people, votes=votes, threshold=threshold,
        attested=attested,
        total=len(att_db.get_group_students(conn, group)),
        closed=closed,
    )
    try:
        await bot.edit_message_text(
            chat_id=int(poll["chat_id"]), message_id=int(message_id),
            text=text, parse_mode="HTML",
            reply_markup=(None if closed
                          else vote_kb_mod.vote_kb(group, date_iso, para,
                                                   people, votes, attested)),
        )
    except TelegramBadRequest as exc:
        logger.debug("vote message not edited",
                     extra={"error": str(exc), "poll_id": poll.get("id")})
        return False
    except Exception as exc:
        logger.warning("vote message edit failed",
                       extra={"error": repr(exc), "poll_id": poll.get("id")})
        return False
    return True
async def close_poll(conn, bot, poll: dict) -> dict:
    """Закрыть голосование: зачесть набравших порог, показать итог.

    Идемпотентна: повторный вызов на уже закрытом голосовании ничего не
    делает и возвращает ``{'closed': False}``. Это важно, потому что закрытие
    может прийти одновременно от кнопки старосты и от фонового цикла.

    Кто не набрал порога — не наказывается: прогул фиксирует староста
    вручную через ``/mark`` (автоматический ``absent`` в проекте не ставится
    нигде).

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        poll: строка ``attendance_vote_polls``.

    Returns:
        ``{'closed': bool, 'attested': [{'tg_id', 'full_name'}],
        'not_attested': [{'tg_id', 'full_name'}]}``.
    """
    poll_id = int(poll["id"])
    group = str(poll["group_name"])
    date_iso = str(poll["date_iso"])
    para = int(poll["para"])
    day = date.fromisoformat(date_iso)

    if not close_vote_poll_row(conn, poll_id):
        return {"closed": False, "attested": [], "not_attested": []}

    # Последний шанс зачесть тех, кто добрал голоса ровно к закрытию.
    votes = vote_counts(conn, group, date_iso, para)
    people = candidates(conn, group, date_iso, para)
    for person in people:
        tg_id = int(person["tg_id"])
        _maybe_attest(conn, group, date_iso, para, tg_id,
                      str(person["full_name"]), votes.get(tg_id, 0))

    attested_now = attested_ids(conn, group, date_iso, para)
    attested = [person for person in people
                if int(person["tg_id"]) in attested_now]
    not_attested = [person for person in people
                    if int(person["tg_id"]) not in attested_now]

    await _edit_final(conn, bot, {**poll, "is_closed": 1}, day, attested,
                      not_attested)
    logger.info("vote poll closed",
                extra={"group": group, "date": date_iso, "para": para,
                       "attested": len(attested),
                       "not_attested": len(not_attested)})
    return {"closed": True, "attested": attested,
            "not_attested": not_attested}


async def _edit_final(conn, bot, poll: dict, day: date, attested: list[dict],
                      not_attested: list[dict]) -> bool:
    """Показать итог закрытого голосования (без кнопок)."""
    from aiogram.exceptions import TelegramBadRequest

    message_id = poll.get("message_id")
    if not message_id:
        return False

    group = str(poll["group_name"])
    para = int(poll["para"])
    text = vtext.render_vote_final(
        group=group, day=day, para=para,
        subject=_subject(conn, group, day, para),
        attested=attested, not_attested=not_attested,
    )
    try:
        await bot.edit_message_text(
            chat_id=int(poll["chat_id"]), message_id=int(message_id),
            text=text, parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        logger.debug("final vote message not edited",
                     extra={"error": str(exc), "poll_id": poll.get("id")})
        return False
    except Exception as exc:
        logger.warning("final vote message failed",
                       extra={"error": repr(exc), "poll_id": poll.get("id")})
        return False
    return True


async def close_due_vote_polls(conn, bot,
                               now: datetime | None = None) -> int:
    """Закрыть голосования, у которых вышел срок (автозакрытие).

    Вызывается из общего прохода посещаемости (``attendance_service.tick``):
    отдельную фоновую задачу не заводим, чтобы не плодить циклы с почти
    одинаковым периодом.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now: текущий момент (для тестов).

    Returns:
        Сколько голосований закрыто.
    """
    moment = now or datetime.now(KRASNOYARSK)
    closed = 0

    for poll in get_open_vote_polls(conn):
        raw = str(poll.get("closes_at") or "")
        try:
            closes = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("could not parse vote closes_at",
                           extra={"value": raw, "poll_id": poll.get("id")})
            continue
        if closes.tzinfo is None:
            closes = closes.replace(tzinfo=KRASNOYARSK)
        if closes >= moment:
            continue

        await close_poll(conn, bot, poll)
        closed += 1

    return closed