"""Работа с БД посещаемости: отметки и опросы (этап 2).

Только SQL: расчёт пар, статусы и тексты — в
:mod:`bot.attendance.attendance_service` и
:mod:`bot.attendance.attendance_texts`. Записи идут через
:func:`bot.db.transaction`, как в остальном проекте.
"""
from __future__ import annotations

import sqlite3

from bot.attendance.db import _now
from bot.db import transaction


# --- отметки ---

def mark_attendance(conn: sqlite3.Connection, group_name: str, date_iso: str,
                    para: int, tg_id: int, full_name: str, status: str,
                    marked_by: int, method: str, subject: str | None = None,
                    now: str | None = None) -> None:
    """Поставить или обновить отметку студента.

    Ключ ``(group_name, date_iso, para, tg_id)`` уникален, поэтому повторная
    запись обновляет статус (это нужно старосте: в ``/mark`` статус
    переключается циклом).

    Args:
        conn: соединение SQLite.
        group_name: группа.
        date_iso: дата пары.
        para: номер пары.
        tg_id: кого отмечаем.
        full_name: ФИО на момент отметки (снимок).
        status: ``present`` | ``late`` | ``absent`` | ``excused``.
        marked_by: кто поставил отметку.
        method: ``self`` | ``starosta`` | ``vote``.
        subject: название предмета на момент отметки (может быть None, если
            расписание недоступно — тогда предмет подтянется из кэша позже).
        now: момент в ISO (для тестов).
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance"
            " (group_name, date_iso, para, tg_id, full_name, status,"
            "  marked_by, marked_at, method, subject)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(group_name, date_iso, para, tg_id) DO UPDATE SET"
            "   status = excluded.status,"
            "   full_name = excluded.full_name,"
            "   marked_by = excluded.marked_by,"
            "   marked_at = excluded.marked_at,"
            "   method = excluded.method,"
            # Неизвестный предмет (None) не должен затирать уже сохранённый:
            # расписание могло не загрузиться в момент повторной отметки.
            "   subject = COALESCE(excluded.subject, attendance.subject)",
            (group_name, date_iso, para, tg_id, full_name, status,
             marked_by, now or _now(), method, subject),
        )


def attendance_exists(conn: sqlite3.Connection, group_name: str, date_iso: str,
                      para: int, tg_id: int) -> bool:
    """Есть ли уже отметка этого студента на эту пару."""
    row = conn.execute(
        "SELECT 1 FROM attendance"
        " WHERE group_name = ? AND date_iso = ? AND para = ? AND tg_id = ?",
        (group_name, date_iso, para, tg_id),
    ).fetchone()
    return row is not None


def get_attendance(conn: sqlite3.Connection, group_name: str, date_iso: str,
                   para: int, tg_id: int) -> dict | None:
    """Отметка студента на пару или None."""
    row = conn.execute(
        "SELECT * FROM attendance"
        " WHERE group_name = ? AND date_iso = ? AND para = ? AND tg_id = ?",
        (group_name, date_iso, para, tg_id),
    ).fetchone()
    return dict(row) if row is not None else None


def get_attendance_list(conn: sqlite3.Connection, group_name: str,
                        date_iso: str, para: int) -> list[dict]:
    """Все отметки на пару, по алфавиту ФИО."""
    rows = conn.execute(
        "SELECT * FROM attendance"
        " WHERE group_name = ? AND date_iso = ? AND para = ?"
        " ORDER BY full_name COLLATE NOCASE, tg_id",
        (group_name, date_iso, para),
    ).fetchall()
    return [dict(row) for row in rows]


def get_attendance_for_day(conn: sqlite3.Connection, group_name: str,
                           date_iso: str) -> list[dict]:
    """Все отметки группы за день (по номеру пары)."""
    rows = conn.execute(
        "SELECT * FROM attendance"
        " WHERE group_name = ? AND date_iso = ?"
        " ORDER BY para, full_name COLLATE NOCASE",
        (group_name, date_iso),
    ).fetchall()
    return [dict(row) for row in rows]


def get_attendance_for_student(conn: sqlite3.Connection, tg_id: int,
                               date_from: str, date_to: str) -> list[dict]:
    """Отметки студента за период (для сводки «Моя посещаемость»)."""
    rows = conn.execute(
        "SELECT * FROM attendance"
        " WHERE tg_id = ? AND date_iso >= ? AND date_iso <= ?"
        " ORDER BY date_iso, para",
        (tg_id, date_from, date_to),
    ).fetchall()
    return [dict(row) for row in rows]


def get_attendance_for_group_period(conn: sqlite3.Connection, group_name: str,
                                    date_from: str,
                                    date_to: str) -> list[dict]:
    """Все отметки группы за период (для отчёта за неделю)."""
    rows = conn.execute(
        "SELECT * FROM attendance"
        " WHERE group_name = ? AND date_iso >= ? AND date_iso <= ?"
        " ORDER BY date_iso, para, full_name COLLATE NOCASE",
        (group_name, date_from, date_to),
    ).fetchall()
    return [dict(row) for row in rows]
# --- опросы ---

def create_poll(conn: sqlite3.Connection, group_name: str, date_iso: str,
                para: int, chat_id: int, message_id: int | None,
                closes_at: str, now: str | None = None) -> int:
    """Создать опрос на пару. Возвращает id опроса.

    При повторном вызове для той же пары (гонка двух проходов цикла) запись
    не дублируется: обновляются ``chat_id``, ``message_id`` и ``closes_at``.
    """
    started = now or _now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO attendance_polls"
            " (group_name, date_iso, para, chat_id, message_id, started_at,"
            "  closes_at, is_closed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)"
            " ON CONFLICT(group_name, date_iso, para) DO UPDATE SET"
            "   chat_id = excluded.chat_id,"
            "   message_id = excluded.message_id,"
            "   closes_at = excluded.closes_at",
            (group_name, date_iso, para, chat_id, message_id, started,
             closes_at),
        )
        row = conn.execute(
            "SELECT id FROM attendance_polls"
            " WHERE group_name = ? AND date_iso = ? AND para = ?",
            (group_name, date_iso, para),
        ).fetchone()
    return int(row["id"])


def poll_exists(conn: sqlite3.Connection, group_name: str, date_iso: str,
                para: int) -> bool:
    """Есть ли уже опрос на эту пару (любой — открытый или закрытый)."""
    row = conn.execute(
        "SELECT 1 FROM attendance_polls"
        " WHERE group_name = ? AND date_iso = ? AND para = ?",
        (group_name, date_iso, para),
    ).fetchone()
    return row is not None


def get_poll(conn: sqlite3.Connection, group_name: str, date_iso: str,
             para: int) -> dict | None:
    """Опрос по паре или None."""
    row = conn.execute(
        "SELECT * FROM attendance_polls"
        " WHERE group_name = ? AND date_iso = ? AND para = ?",
        (group_name, date_iso, para),
    ).fetchone()
    return dict(row) if row is not None else None


def get_open_polls(conn: sqlite3.Connection) -> list[dict]:
    """Все незакрытые опросы (для закрытия по времени)."""
    rows = conn.execute(
        "SELECT * FROM attendance_polls WHERE is_closed = 0"
        " ORDER BY date_iso, para"
    ).fetchall()
    return [dict(row) for row in rows]


def close_poll(conn: sqlite3.Connection, poll_id: int) -> bool:
    """Пометить опрос закрытым."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE attendance_polls SET is_closed = 1 WHERE id = ?",
            (poll_id,),
        )
    return cursor.rowcount > 0


def update_poll_message_id(conn: sqlite3.Connection, poll_id: int,
                           message_id: int) -> bool:
    """Записать id сообщения опроса (после отправки)."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE attendance_polls SET message_id = ? WHERE id = ?",
            (message_id, poll_id),
        )
    return cursor.rowcount > 0


def count_polls(conn: sqlite3.Connection) -> int:
    """Сколько опросов создано (для диагностики)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM attendance_polls").fetchone()[0]
    )


def count_attendance(conn: sqlite3.Connection) -> int:
    """Сколько отметок всего (для диагностики)."""
    return int(
        conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
    )