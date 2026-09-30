"""Миграции посещаемости: этап 1 (группы, студенты) и этап 2 (отметки).

Миграции живут в пакете ``bot.attendance``, но подключаются в общий список
``bot.migrations.MIGRATIONS`` — так сохраняется единая нумерация версий схемы
и идемпотентность ``apply_migrations``.

Ничего из существующих таблиц не меняется: ``users.group_name`` (старая
система, нужна расписанию и заменам) остаётся как есть, а ``students`` и
``attendance`` — отдельные таблицы для посещаемости.
"""
from __future__ import annotations

import sqlite3

from bot.attendance.models import (
    CREATE_ATTENDANCE,
    CREATE_ATTENDANCE_GROUP_DATE_INDEX,
    CREATE_ATTENDANCE_POLLS,
    CREATE_ATTENDANCE_POLLS_GROUP_INDEX,
    CREATE_ATTENDANCE_TG_ID_INDEX,
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


def migrate_11_attendance_marks(conn: sqlite3.Connection) -> None:
    """Миграция 10 → 11: ``attendance`` и ``attendance_polls``.

    ``attendance`` — отметки. ``UNIQUE (group_name, date_iso, para, tg_id)``
    не даёт поставить две отметки на одну пару: вместо этого статус
    обновляется (см. :func:`bot.attendance.attendance_db.mark_attendance`).

    ``attendance_polls`` — опросы «кто на паре». ``UNIQUE (group_name,
    date_iso, para)`` гарантирует один опрос на пару: фоновая задача
    проверяет пары раз в минуту и без этого ключа слала бы опрос повторно.
    ``message_id`` редактируется по мере отметок, ``closes_at`` — конец пары.

    Автоматический ``absent`` по закрытию опроса НЕ ставится: прогул
    фиксирует только староста вручную (``/mark``).
    """
    conn.execute(CREATE_ATTENDANCE)
    conn.execute(CREATE_ATTENDANCE_GROUP_DATE_INDEX)
    conn.execute(CREATE_ATTENDANCE_TG_ID_INDEX)
    conn.execute(CREATE_ATTENDANCE_POLLS)
    conn.execute(CREATE_ATTENDANCE_POLLS_GROUP_INDEX)


def migrate_12_attendance_subject(conn: sqlite3.Connection) -> None:
    """Миграция 11 → 12: колонка ``subject`` в ``attendance``.

    Предмет сохраняется в самой отметке, потому что расписание может
    измениться (замена, новая версия DOCX), а сводка аттестации должна
    считать пары по тем предметам, которые были в момент отметки.

    Колонка nullable: у записей, сделанных до этой миграции, предмета нет —
    для них работает fallback по ``schedule_cache`` (см.
    :func:`bot.attendance.attestation_service.subject_for_mark`).

    Повторный ``ALTER TABLE ADD COLUMN`` упал бы с ``duplicate column name``,
    поэтому сначала проверяем состав колонок.
    """
    columns = {str(row["name"]) for row in conn.execute(
        "PRAGMA table_info(attendance)"
    )}
    if "subject" not in columns:
        conn.execute("ALTER TABLE attendance ADD COLUMN subject TEXT")