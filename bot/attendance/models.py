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

# --- посещаемость (этап 2) ---

# Статусы отметки.
STATUS_PRESENT = "present"
STATUS_LATE = "late"
STATUS_ABSENT = "absent"
STATUS_EXCUSED = "excused"

# Порядок переключения статуса старостой в /mark (цикл по кнопке).
STATUS_CYCLE = (STATUS_PRESENT, STATUS_LATE, STATUS_ABSENT, STATUS_EXCUSED)

# Все допустимые статусы.
ALL_STATUSES = frozenset(STATUS_CYCLE)

# Способы отметки: студент сам, староста, голосование в чате, автозакрытие.
METHOD_SELF = "self"
METHOD_STAROSTA = "starosta"
METHOD_VOTE = "vote"
# Автоматическая отметка при закрытии опроса «Да/Нет»: студент не ответил
# за окно в 5 минут, поэтому прогул ставит бот (миграция 14).
METHOD_AUTO = "auto"

# Режимы посещаемости группы (``study_groups.attendance_mode``):
# опрос уходит в чат группы или каждому студенту в личку.
MODE_CHAT = "chat"
MODE_DIRECT = "direct"
ALL_MODES = (MODE_CHAT, MODE_DIRECT)

# Типы опросов (``attendance_polls.poll_type``):
# ``self`` — старая механика «Я на паре» (опрос до конца пары, без автоотметок);
# ``check`` — новая механика «Да/Нет» с окном в 5 минут и авто-absent.
POLL_TYPE_SELF = "self"
POLL_TYPE_CHECK = "check"

# Сколько минут длится окно опроса «Да/Нет» (миграция 14).
CHECK_POLL_MINUTES = 5

# Сколько минут после начала пары отметка считается «опоздал».
LATE_AFTER_MINUTES = 15

# Таблица отметок: одна строка на (группа, дата, пара, студент).
#
# ``subject`` — снимок названия предмета на момент отметки (миграция 12).
# Он нужен сводке аттестации: расписание может измениться, а считаться
# должны те предметы, что были в момент отметки. Для записей до миграции
# колонка NULL — тогда предмет подтягивается из ``schedule_cache``.
CREATE_ATTENDANCE = """
    CREATE TABLE IF NOT EXISTS attendance (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT    NOT NULL,
        date_iso   TEXT    NOT NULL,
        para       INTEGER NOT NULL,
        tg_id      INTEGER NOT NULL,
        full_name  TEXT    NOT NULL,
        status     TEXT    NOT NULL,
        marked_by  INTEGER NOT NULL,
        marked_at  TEXT    NOT NULL,
        method     TEXT    NOT NULL,
        subject    TEXT,
        UNIQUE (group_name, date_iso, para, tg_id)
    )
"""

CREATE_ATTENDANCE_GROUP_DATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_attendance_group_date"
    " ON attendance (group_name, date_iso)"
)

CREATE_ATTENDANCE_TG_ID_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_attendance_tg_id"
    " ON attendance (tg_id, date_iso)"
)

# Таблица опросов: один опрос на (группа, дата, пара).
#
# ``mode`` и ``poll_type`` добавлены миграцией 14. Режим хранится в самой
# записи, а не читается из ``study_groups`` на лету: иначе смена режима
# старостой во время активного опроса сломала бы уже разосланные сообщения
# (в ``direct`` одно сообщение на студента, редактировать нечего).
#
# ``message_id`` заполняется только для ``mode='chat'``. В режиме ``direct``
# сообщений много (по одному на студента), и держать их список в этой таблице
# смысла нет: при закрытии рассылка идёт заново каждому студенту.
CREATE_ATTENDANCE_POLLS = """
    CREATE TABLE IF NOT EXISTS attendance_polls (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT    NOT NULL,
        date_iso   TEXT    NOT NULL,
        para       INTEGER NOT NULL,
        chat_id    INTEGER NOT NULL,
        message_id INTEGER,
        started_at TEXT    NOT NULL,
        closes_at  TEXT    NOT NULL,
        is_closed  INTEGER NOT NULL DEFAULT 0,
        mode       TEXT    NOT NULL DEFAULT 'chat',
        poll_type  TEXT    NOT NULL DEFAULT 'self',
        UNIQUE (group_name, date_iso, para)
    )
"""

CREATE_ATTENDANCE_POLLS_GROUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_attendance_polls_group"
    " ON attendance_polls (group_name, date_iso)"
)

# --- голосование за посещаемость (миграция 13) ---

# Таблица голосов: одна строка на пару (голосующий → за кого).
#
# Голос за себя разрешён (студент сам подтверждает, что был), поэтому в
# UNIQUE нет запрета ``target_tg_id = voter_tg_id``: это осознанное решение
# владельца проекта, а не недосмотр.
CREATE_ATTENDANCE_VOTES = """
    CREATE TABLE IF NOT EXISTS attendance_votes (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name       TEXT    NOT NULL,
        date_iso         TEXT    NOT NULL,
        para             INTEGER NOT NULL,
        target_tg_id     INTEGER NOT NULL,
        target_full_name TEXT    NOT NULL,
        voter_tg_id      INTEGER NOT NULL,
        voted_at         TEXT    NOT NULL,
        UNIQUE (group_name, date_iso, para, target_tg_id, voter_tg_id)
    )
"""

CREATE_ATTENDANCE_VOTES_TARGET_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_attendance_votes_target"
    " ON attendance_votes (group_name, date_iso, para, target_tg_id)"
)

# Таблица голосований: одно голосование на (группа, дата, пара).
#
# ``UNIQUE`` не даёт запустить второе голосование по той же паре, если
# староста нажал кнопку дважды (или два прохода цикла совпали).
CREATE_ATTENDANCE_VOTE_POLLS = """
    CREATE TABLE IF NOT EXISTS attendance_vote_polls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name  TEXT    NOT NULL,
        date_iso    TEXT    NOT NULL,
        para        INTEGER NOT NULL,
        chat_id     INTEGER NOT NULL,
        message_id  INTEGER,
        started_by  INTEGER NOT NULL,
        started_at  TEXT    NOT NULL,
        closes_at   TEXT    NOT NULL,
        is_closed   INTEGER NOT NULL DEFAULT 0,
        UNIQUE (group_name, date_iso, para)
    )
"""

# Роли, которым доступны действия старосты (управление группой, назначение
# зама). Зам может отмечать посещаемость, но не управлять группой.
ADMIN_ROLES = frozenset({ROLE_STAROSTA})
MANAGE_ROLES = frozenset({ROLE_STAROSTA})

# Таблица групп: одна строка на учебную группу КСТ.
#
# ``attendance_mode`` (миграция 14) — куда староста хочет получать опросы
# «Да/Нет»: в чат группы (``chat``) или каждому студенту в личку (``direct``).
CREATE_STUDY_GROUPS = """
    CREATE TABLE IF NOT EXISTS study_groups (
        group_name    TEXT PRIMARY KEY,
        invite_code   TEXT UNIQUE NOT NULL,
        starosta_tg_id INTEGER,
        deputy_tg_id   INTEGER,
        created_at    TEXT NOT NULL,
        created_by    INTEGER NOT NULL,
        attendance_mode TEXT NOT NULL DEFAULT 'chat'
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