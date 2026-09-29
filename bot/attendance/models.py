"""SQL-схемы таблиц посещаемости (этап 1).

Схемы вынесены сюда отдельно от миграций: так видно «как выглядит модель»
без чтения кода миграции, а тесты и документация ссылаются на один источник.

Соглашения по типам — как в остальной схеме проекта:
- моменты времени — TEXT в ISO-8601 с поясом Красноярска;
- роли — TEXT: ``'student'`` | ``'deputy'`` | ``'starosta'``;
- ``group_name`` — нормализованное имя группы (совпадает с
  ``schedule_cache.group_name``, например «25КАД»).
"""
from __future__ import annotations

# Роли студента в группе.
ROLE_STUDENT = "student"
ROLE_DEPUTY = "deputy"
ROLE_STAROSTA = "starosta"

# Роли, которым доступны действия старосты (управление группой, назначение
# зама). Зам может отмечать посещаемость, но не управлять группой.
ADMIN_ROLES = frozenset({ROLE_STAROSTA})
MANAGE_ROLES = frozenset({ROLE_STAROSTA})

# Таблица групп: одна строка на учебную группу КСТ.
CREATE_STUDY_GROUPS = """
    CREATE TABLE IF NOT EXISTS study_groups (
        group_name    TEXT PRIMARY KEY,
        invite_code   TEXT UNIQUE NOT NULL,
        starosta_tg_id INTEGER,
        deputy_tg_id   INTEGER,
        created_at    TEXT NOT NULL,
        created_by    INTEGER NOT NULL
    )
"""

CREATE_STUDY_GROUPS_CODE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_study_groups_code"
    " ON study_groups (invite_code)"
)

# Таблица студентов: один студент — одна группа (tg_id первичный ключ).
CREATE_STUDENTS = """
    CREATE TABLE IF NOT EXISTS students (
        tg_id      INTEGER PRIMARY KEY,
        group_name TEXT NOT NULL,
        full_name  TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT 'student',
        joined_at  TEXT NOT NULL,
        FOREIGN KEY (group_name) REFERENCES study_groups (group_name)
    )
"""

CREATE_STUDENTS_GROUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_students_group"
    " ON students (group_name)"
)