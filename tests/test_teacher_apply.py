"""Тесты роли «преподаватель»: заявка, модерация, группы и пары.

Сети нет: работаем с БД в памяти и реальным справочником
:data:`bot.parsers.teachers.TEACHERS`.
"""

from datetime import date

import pytest

from bot.db import (
    TEACHER_APPROVED,
    TEACHER_PENDING,
    TEACHER_REJECTED,
    apply_teacher,
    approve_teacher,
    cancel_teacher_application,
    get_teacher,
    get_teacher_groups,
    get_teacher_lessons_for_day,
    is_teacher,
    list_approved_teachers,
    list_pending_teachers,
    reject_teacher,
)
from bot.db import get_connection
from bot.handlers.teacher_apply import available_names, match_names
from bot.migrations import MIGRATIONS, apply_migrations, get_schema_version
from bot.parsers.teachers import PLACEHOLDER_MARK, TEACHERS

TG_ID = 908084777
ADMIN_ID = 111222333
OTHER_TG = 555000111

# Реальные ФИО из справочника (проверяются ниже) — не выдуманные.
FIO = "Богатырева Ирина Павловна"
FIO_2 = "Виссарионова Анна Сергеевна"


@pytest.fixture()
def conn(tmp_path):
    """БД со всеми миграциями и кэшем расписания из двух преподавателей."""
    connection = get_connection(tmp_path / "teachers.db")
    apply_migrations(connection)

    connection.executemany(
        "INSERT INTO schedule_cache"
        " (group_name, day_of_week, para_number, subject, teacher, room,"
        "  week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00')",
        [
            # Первый препод: две группы, понедельник.
            ("25КАД", 1, 1, "Математика", FIO, "204", ""),
            ("26ИМС1", 1, 2, "Физика", FIO, "316А", ""),
            # Второй: одна группа.
            ("25ПГ", 1, 3, "История", FIO_2, "101", ""),
            # Составная ячейка: два препода в одной паре.
            ("24МОСДР1", 2, 1, "Геодезия",
             f"{FIO}, {FIO_2}", "411Б", ""),
            # Пара только по чётным неделям.
            ("25КАД", 3, 1, "Чертёж", FIO, "207", "Чет"),
        ],
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def monday() -> date:
    """Понедельник — день, на который в кэше есть пары."""
    return date(2026, 10, 5)


# --- миграция ---

def test_migration_17_applied(conn) -> None:
    """Миграция 17 применена, схема доведена до неё."""
    assert 17 in MIGRATIONS
    assert get_schema_version(conn) == max(MIGRATIONS)


def test_teachers_table_columns(conn) -> None:
    """Таблица teachers содержит нужные поля."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(teachers)")}
    assert columns == {
        "tg_id", "full_name", "status", "applied_at", "approved_at",
        "approved_by",
    }


def test_schedule_teacher_index_exists(conn) -> None:
    """Индекс по преподавателю создан (ускоряет выборку расписания)."""
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_schedule_teacher" in names
    assert "idx_teachers_status" in names
    assert "idx_teachers_full_name" in names


def test_existing_tables_untouched(conn) -> None:
    """Роли students/users/vk_users не менялись миграцией 17."""
    users = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    students = [r["name"] for r in conn.execute("PRAGMA table_info(students)")]
    vk_users = [r["name"] for r in conn.execute("PRAGMA table_info(vk_users)")]
    assert users == ["tg_id", "group_name", "full_name", "is_active",
                     "created_at", "notifications_enabled"]
    assert students == ["tg_id", "group_name", "full_name", "role", "joined_at"]
    assert vk_users == ["vk_id", "group_name", "full_name", "created_at",
                        "updated_at"]


# --- справочник ---

def test_reference_names_are_available(conn) -> None:
    """Все ФИО со статусом «уточняется» в заявку не предлагаются."""
    names = available_names()
    assert names, "список ФИО не должен быть пуст"
    assert not any(PLACEHOLDER_MARK in name for name in names)


def test_fio_used_in_tests_exists() -> None:
    """ФИО, используемые в тестах, реально есть в справочнике."""
    assert FIO in TEACHERS.values()
    assert FIO_2 in TEACHERS.values()
    assert PLACEHOLDER_MARK not in FIO


def test_match_names_by_surname() -> None:
    """Поиск по подстроке находит ФИО (регистр не важен)."""
    assert FIO in match_names("Богатырева")
    assert FIO in match_names("богатырева")
    assert FIO in match_names("богатыр")


def test_match_names_empty_query() -> None:
    """Пустой запрос не даёт совпадений."""
    assert match_names("") == []
    assert match_names("   ") == []


def test_match_names_unknown() -> None:
    """Неизвестная фамилия — пустой список."""
    assert match_names("Пупкинехко") == []


def test_match_names_respects_limit() -> None:
    """Число совпадений ограничено."""
    from bot.handlers.teacher_apply import MAX_CHOICES

    assert len(match_names("а", limit=3)) <= 3
    assert len(match_names("а")) <= MAX_CHOICES
# --- заявка ---

def test_apply_teacher_success(conn) -> None:
    """Первая заявка создаётся со статусом pending."""
    result = apply_teacher(conn, TG_ID, FIO)

    assert result["ok"] is True
    assert result["status"] == TEACHER_PENDING
    teacher = get_teacher(conn, TG_ID)
    assert teacher["full_name"] == FIO
    assert teacher["status"] == TEACHER_PENDING
    assert teacher["applied_at"], "время подачи должно быть заполнено"
    assert teacher["approved_at"] is None
    assert teacher["approved_by"] is None


def test_apply_teacher_duplicate_pending(conn) -> None:
    """Повторная заявка при pending отклоняется."""
    apply_teacher(conn, TG_ID, FIO)
    again = apply_teacher(conn, TG_ID, FIO_2)

    assert again["ok"] is False
    assert again["status"] == TEACHER_PENDING
    assert get_teacher(conn, TG_ID)["full_name"] == FIO, "ФИО не подменилось"


def test_apply_teacher_duplicate_approved(conn) -> None:
    """После одобрения новая заявка не создаётся."""
    apply_teacher(conn, TG_ID, FIO)
    approve_teacher(conn, TG_ID, ADMIN_ID)
    again = apply_teacher(conn, TG_ID, FIO_2)

    assert again["ok"] is False
    assert again["status"] == TEACHER_APPROVED


def test_apply_teacher_rejected_tells_to_contact_admin(conn) -> None:
    """После отклонения подать заново нельзя — только к админу."""
    apply_teacher(conn, TG_ID, FIO)
    reject_teacher(conn, TG_ID, ADMIN_ID)

    again = apply_teacher(conn, TG_ID, FIO)
    assert again["ok"] is False
    assert again["status"] == TEACHER_REJECTED


def test_apply_teacher_rejects_unknown_fio(conn) -> None:
    """ФИО не из справочника не принимается (защита от подделки)."""
    result = apply_teacher(conn, TG_ID, "Пупкин Василий Иванович")

    assert result["ok"] is False
    assert "справочник" in result["error"]
    assert get_teacher(conn, TG_ID) is None


def test_apply_teacher_rejects_placeholder_fio(conn) -> None:
    """ФИО с пометкой «уточняется» не принимается: одобрять нечего."""
    placeholder = next(v for v in TEACHERS.values() if PLACEHOLDER_MARK in v)
    result = apply_teacher(conn, TG_ID, placeholder)

    assert result["ok"] is False
    assert get_teacher(conn, TG_ID) is None


def test_apply_teacher_rejects_empty(conn) -> None:
    """Пустое ФИО не создаёт заявку."""
    assert apply_teacher(conn, TG_ID, "")["ok"] is False
    assert apply_teacher(conn, TG_ID, "   ")["ok"] is False
    assert get_teacher(conn, TG_ID) is None


# --- get_teacher / is_teacher ---

def test_get_teacher_none_for_stranger(conn) -> None:
    """Без заявки get_teacher возвращает None."""
    assert get_teacher(conn, TG_ID) is None


def test_is_teacher_only_after_approval(conn) -> None:
    """Прав нет ни до заявки, ни в pending, ни в rejected."""
    assert is_teacher(conn, TG_ID) is False

    apply_teacher(conn, TG_ID, FIO)
    assert is_teacher(conn, TG_ID) is False, "pending не даёт прав"

    approve_teacher(conn, TG_ID, ADMIN_ID)
    assert is_teacher(conn, TG_ID) is True


def test_is_teacher_false_after_reject(conn) -> None:
    """Отклонённая заявка прав не даёт."""
    apply_teacher(conn, TG_ID, FIO)
    reject_teacher(conn, TG_ID, ADMIN_ID)

    assert is_teacher(conn, TG_ID) is False
# --- модерация ---

def test_approve_teacher(conn) -> None:
    """Одобрение фиксирует время и админа."""
    apply_teacher(conn, TG_ID, FIO)
    assert approve_teacher(conn, TG_ID, ADMIN_ID) is True

    teacher = get_teacher(conn, TG_ID)
    assert teacher["status"] == TEACHER_APPROVED
    assert teacher["approved_at"]
    assert teacher["approved_by"] == ADMIN_ID


def test_approve_unknown_teacher(conn) -> None:
    """Одобрить несуществующую заявку нельзя."""
    assert approve_teacher(conn, TG_ID, ADMIN_ID) is False


def test_reject_teacher_clears_approval(conn) -> None:
    """Отклонение снимает отметку одобрения."""
    apply_teacher(conn, TG_ID, FIO)
    approve_teacher(conn, TG_ID, ADMIN_ID)
    assert reject_teacher(conn, TG_ID, ADMIN_ID) is True

    teacher = get_teacher(conn, TG_ID)
    assert teacher["status"] == TEACHER_REJECTED
    assert teacher["approved_at"] is None


def test_reject_unknown_teacher(conn) -> None:
    """Отклонить несуществующую заявку нельзя."""
    assert reject_teacher(conn, TG_ID, ADMIN_ID) is False


def test_list_pending_and_approved(conn) -> None:
    """Списки заявок разделены по статусам."""
    apply_teacher(conn, TG_ID, FIO)
    apply_teacher(conn, OTHER_TG, FIO_2)

    pending = list_pending_teachers(conn)
    assert {t["tg_id"] for t in pending} == {TG_ID, OTHER_TG}
    assert list_approved_teachers(conn) == []

    approve_teacher(conn, TG_ID, ADMIN_ID)
    assert [t["tg_id"] for t in list_approved_teachers(conn)] == [TG_ID]
    assert [t["tg_id"] for t in list_pending_teachers(conn)] == [OTHER_TG]


def test_cancel_only_pending(conn) -> None:
    """Отменить можно только нерассмотренную заявку."""
    apply_teacher(conn, TG_ID, FIO)
    assert cancel_teacher_application(conn, TG_ID) is True
    assert get_teacher(conn, TG_ID) is None


def test_cancel_approved_is_refused(conn) -> None:
    """Одобренную роль сам препод не снимает — это делает админ."""
    apply_teacher(conn, TG_ID, FIO)
    approve_teacher(conn, TG_ID, ADMIN_ID)

    assert cancel_teacher_application(conn, TG_ID) is False
    assert is_teacher(conn, TG_ID) is True, "права остались"


def test_cancel_without_application(conn) -> None:
    """Отмена без заявки — False."""
    assert cancel_teacher_application(conn, TG_ID) is False


# --- группы и пары ---

def test_get_teacher_groups(conn) -> None:
    """Группы преподавателя берутся из кэша расписания."""
    groups = get_teacher_groups(conn, FIO)

    assert groups == ["24МОСДР1", "25КАД", "26ИМС1"], groups


def test_get_teacher_groups_returns_sorted_unique(conn) -> None:
    """Список групп отсортирован и без повторов."""
    groups = get_teacher_groups(conn, FIO)

def test_get_teacher_lessons_for_day(conn, monday) -> None:
    """Пары на понедельник: только свои и в нужном порядке."""
    lessons = get_teacher_lessons_for_day(conn, FIO, monday)

    assert [lesson["para_number"] for lesson in lessons] == [1, 2]
    assert {lesson["group_name"] for lesson in lessons} == {"25КАД", "26ИМС1"}
    assert all(lesson["teacher"] == FIO for lesson in lessons)


def test_lessons_include_time_range(conn, monday) -> None:
    """В каждой паре есть время по звонкам."""
    lessons = get_teacher_lessons_for_day(conn, FIO, monday)

    assert lessons
    assert all(lesson["time_range"] for lesson in lessons)


def test_lessons_respect_week_type(conn) -> None:
    """Пара «по чётным» не показывается в нечётную неделю.

    Чётность считается по числу месяца: 07.10 (день 7) — нечётная,
    14.10 (день 14) — чётная. Пара «Чертёж» стоит в среду с week_type='Чет'.
    """
    odd_wed = date(2026, 10, 7)
    even_wed = date(2026, 10, 14)

    odd_lessons = get_teacher_lessons_for_day(conn, FIO, odd_wed)
    even_lessons = get_teacher_lessons_for_day(conn, FIO, even_wed)

    assert not any(item["subject"] == "Чертёж" for item in odd_lessons), \
        odd_lessons
    assert any(item["subject"] == "Чертёж" for item in even_lessons), \
        even_lessons


def test_lessons_other_day_is_empty(conn) -> None:
    """В день без пар список пуст."""
    saturday = date(2026, 10, 10)

    assert get_teacher_lessons_for_day(conn, FIO, saturday) == []


def test_lessons_unknown_teacher(conn, monday) -> None:
    """У неизвестного преподавателя пар нет."""
    assert get_teacher_lessons_for_day(
        conn, "Пупкин Василий Иванович", monday
    ) == []


def test_student_role_is_not_affected(conn) -> None:
    """Роль преподавателя не затрагивает students и users.

    Требование: если у препода есть группа как у студента, он остаётся
    и студентом, и преподом.
    """
    # Студент в группе посещаемости: нужна сама учебная группа (FK).
    conn.execute(
        "INSERT INTO study_groups (group_name, invite_code, created_by,"
        " created_at) VALUES ('25КАД', 'CODE123', ?, 'x')", (TG_ID,)
    )
    conn.execute(
        "INSERT INTO students (tg_id, group_name, full_name, role, joined_at)"
        " VALUES (?, '25КАД', 'Иван', 'student', 'x')", (TG_ID,)
    )
    conn.execute(
        "INSERT INTO users (tg_id, group_name, full_name, is_active,"
        " created_at, notifications_enabled)"
        " VALUES (?, '25КАД', 'Иван', 1, 'x', 1)", (TG_ID,)
    )
    conn.commit()

    apply_teacher(conn, TG_ID, FIO)
    approve_teacher(conn, TG_ID, ADMIN_ID)

    assert is_teacher(conn, TG_ID) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM students WHERE tg_id = ?", (TG_ID,)
    ).fetchone()[0] == 1, "студент не удалён"
    assert conn.execute(
        "SELECT COUNT(*) FROM users WHERE tg_id = ?", (TG_ID,)
    ).fetchone()[0] == 1, "пользователь расписания не удалён"


def test_get_teacher_groups_from_shared_cell(conn) -> None:
    """Преподаватель находится и в ячейке с несколькими ФИО.

    В ``schedule_cache.teacher`` часто стоит «ФИО1, ФИО2» (подгруппы одного
    занятия). Простой ``WHERE teacher = ?`` такие пары пропустил бы.
    """
    assert "24МОСДР1" in get_teacher_groups(conn, FIO)
    assert "24МОСДР1" in get_teacher_groups(conn, FIO_2)


def test_get_teacher_groups_unknown(conn) -> None:
    """У неизвестного преподавателя групп нет."""
    assert get_teacher_groups(conn, "Пупкин Василий Иванович") == []