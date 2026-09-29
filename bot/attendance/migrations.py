"""Миграция 10: таблицы групп и студентов (этап 1 посещаемости).

Миграция живёт в пакете ``bot.attendance``, но подключается в общий список
``bot.migrations.MIGRATIONS`` — так сохраняется единая нумерация версий схемы
и идемпотентность ``apply_migrations``.

Ничего из существующих таблиц не меняется: ``users.group_name`` (старая
система, нужна расписанию и заменам) остаётся как есть, а ``students`` —
отдельная таблица для посещаемости.
"""
from __future__ import annotations

import sqlite3

from bot.attendance.models import (
    CREATE_STUDENTS,
    CREATE_STUDENTS_GROUP_INDEX,
    CREATE_STUDY_GROUPS,
    CREATE_STUDY_GROUPS_CODE_INDEX,
)


def migrate_10_attendance_groups(conn: sqlite3.Connection) -> None:
    """Миграция 9 → 10: ``study_groups`` и ``students``.

    ``study_groups`` — учебная группа КСТ с кодом приглашения; один код на
    группу, уникальный индекс по ``invite_code`` нужен для быстрого поиска
    при вводе кода и защищает от коллизий при генерации.

    ``students`` — студент в группе; ``tg_id`` первичный ключ, то есть один
    человек состоит ровно в одной группе (при повторном вводе кода другой
    группы будет ошибка — так и задумано).

    ``starosta_tg_id``/``deputy_tg_id`` держим и в ``study_groups`` (быстрый
    доступ «кто главный»), и в ``students.role`` (роль конкретного студента) —
    источники согласованы в :mod:`bot.attendance.service`.
    """
    conn.execute(CREATE_STUDY_GROUPS)
    conn.execute(CREATE_STUDY_GROUPS_CODE_INDEX)
    conn.execute(CREATE_STUDENTS)
    conn.execute(CREATE_STUDENTS_GROUP_INDEX)