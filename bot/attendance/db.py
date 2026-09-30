"""Работа с БД посещаемости: группы и студенты (этап 1).

Только SQL: логика (генерация кодов, роли, валидация) — в
:mod:`bot.attendance.service`. Все записи идут через :func:`bot.db.transaction`,
как и в остальном проекте.
"""
from __future__ import annotations

import sqlite3

from bot.db import transaction


def _now() -> str:
    """Текущий момент в ISO-8601 с поясом техникума."""
    from datetime import datetime

    from bot.config import TIMEZONE

    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


# --- группы ---

def insert_group(conn: sqlite3.Connection, group_name: str, invite_code: str,
                 created_by: int, starosta_tg_id: int | None = None,
                 now: str | None = None) -> None:
    """Создать учебную группу с кодом приглашения."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO study_groups"
            " (group_name, invite_code, starosta_tg_id, deputy_tg_id,"
            "  created_at, created_by)"
            " VALUES (?, ?, ?, NULL, ?, ?)",
            (group_name, invite_code, starosta_tg_id, now or _now(), created_by),
        )


def get_group(conn: sqlite3.Connection, group_name: str) -> dict | None:
    """Группа по имени или None, если она ещё не создана."""
    row = conn.execute(
        "SELECT * FROM study_groups WHERE group_name = ?", (group_name,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_group_by_code(conn: sqlite3.Connection, invite_code: str) -> dict | None:
    """Группа по коду приглашения или None."""
    row = conn.execute(
        "SELECT * FROM study_groups WHERE invite_code = ?", (invite_code,)
    ).fetchone()
    return dict(row) if row is not None else None


def update_invite_code(conn: sqlite3.Connection, group_name: str,
                       invite_code: str) -> bool:
    """Заменить код приглашения группы."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE study_groups SET invite_code = ? WHERE group_name = ?",
            (invite_code, group_name),
        )
    return cursor.rowcount > 0


def set_group_starosta(conn: sqlite3.Connection, group_name: str,
                       tg_id: int) -> bool:
    """Записать старосту в ``study_groups``."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE study_groups SET starosta_tg_id = ? WHERE group_name = ?",
            (tg_id, group_name),
        )
    return cursor.rowcount > 0


def set_group_deputy(conn: sqlite3.Connection, group_name: str,
                     tg_id: int | None) -> bool:
    """Записать зама в ``study_groups`` (None — снять зама)."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE study_groups SET deputy_tg_id = ? WHERE group_name = ?",
            (tg_id, group_name),
        )
    return cursor.rowcount > 0


def get_all_groups(conn: sqlite3.Connection) -> list[dict]:
    """Все созданные учебные группы (для админки и тестов)."""
    rows = conn.execute(
        "SELECT * FROM study_groups ORDER BY group_name"
    ).fetchall()
    return [dict(row) for row in rows]


# --- студенты ---

def insert_student(conn: sqlite3.Connection, tg_id: int, group_name: str,
                   full_name: str, role: str = "student",
                   now: str | None = None) -> None:
    """Добавить студента в группу."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO students"
            " (tg_id, group_name, full_name, role, joined_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (tg_id, group_name, full_name, role, now or _now()),
        )


def get_student(conn: sqlite3.Connection, tg_id: int) -> dict | None:
    """Студент по tg_id или None, если он ещё не в группе."""
    row = conn.execute(
        "SELECT * FROM students WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def set_student_role(conn: sqlite3.Connection, tg_id: int, role: str) -> bool:
    """Сменить роль студента."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE students SET role = ? WHERE tg_id = ?", (role, tg_id)
        )
    return cursor.rowcount > 0


def get_attendance_mode(conn: sqlite3.Connection,
                        group_name: str) -> str:
    """Режим посещаемости группы: ``chat`` или ``direct``.

    Читается при создании опроса и запоминается в самом опросе: иначе смена
    режима старостой во время активного опроса сломала бы разосланные
    сообщения.

    Args:
        conn: соединение SQLite.
        group_name: группа.

    Returns:
        Значение ``attendance_mode``; ``'chat'``, если группа не найдена
        (поведение по умолчанию — как было до миграции 14).
    """
    from bot.attendance.models import MODE_CHAT

    row = conn.execute(
        "SELECT attendance_mode FROM study_groups WHERE group_name = ?",
        (group_name,),
    ).fetchone()
    if row is None:
        return MODE_CHAT
    return str(row["attendance_mode"] or MODE_CHAT)


def set_attendance_mode(conn: sqlite3.Connection, group_name: str,
                        mode: str) -> bool:
    """Сменить режим посещаемости группы.

    Args:
        conn: соединение SQLite.
        group_name: группа.
        mode: ``chat`` или ``direct``.

    Returns:
        True, если группа найдена и режим записан.
    """
    from bot.attendance.models import ALL_MODES

    if mode not in ALL_MODES:
        raise ValueError(f"unknown attendance mode: {mode!r}")

    with transaction(conn):
        cursor = conn.execute(
            "UPDATE study_groups SET attendance_mode = ? WHERE group_name = ?",
            (mode, group_name),
        )
    return cursor.rowcount > 0


def get_group_students(conn: sqlite3.Connection,
                       group_name: str) -> list[dict]:
    """Студенты группы по алфавиту."""
    rows = conn.execute(
        "SELECT * FROM students WHERE group_name = ?"
        " ORDER BY full_name COLLATE NOCASE, tg_id",
        (group_name,),
    ).fetchall()
    return [dict(row) for row in rows]


def count_group_students(conn: sqlite3.Connection, group_name: str) -> int:
    """Сколько студентов в группе."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM students WHERE group_name = ?", (group_name,)
        ).fetchone()[0]
    )