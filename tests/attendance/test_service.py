"""Тесты логики посещаемости (этап 1): коды, ФИО, регистрация, роли.

Проверяется чистая логика (:mod:`bot.attendance.service`) на in-memory БД —
без Telegram и без диспетчера.
"""

from pathlib import Path

import pytest

from bot.attendance import db as att_db
from bot.attendance import service
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations

GROUP = "25КАД"
OTHER_GROUP = "026КАД"
STAROSTA_ID = 1001
STUDENT_ID = 1002


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и группами в расписании (для fuzzy-поиска)."""
    c = get_connection(tmp_path / "attendance.db")
    apply_migrations(c)
    with transaction(c):
        for name in (GROUP, OTHER_GROUP, "26КАД"):
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, week_type, updated_at)"
                " VALUES (?, 1, 1, 'ОД.01', '', 'x')", (name,)
            )
    yield c
    c.close()


@pytest.fixture()
def group_with_starosta(conn):
    """Группа GROUP с созданным старостой; возвращает код приглашения."""
    return service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")


# --- generate_invite_code ---

def test_invite_code_is_six_digits() -> None:
    """Код — ровно 6 цифр."""
    for _ in range(20):
        code = service.generate_invite_code()
        assert len(code) == service.INVITE_CODE_LENGTH
        assert code.isdigit()


def test_invite_code_has_no_leading_zero() -> None:
    """Ведущего нуля нет: код диктуют голосом, ноль потерялся бы."""
    for _ in range(50):
        assert not service.generate_invite_code().startswith("0")


def test_invite_codes_differ() -> None:
    """Повторные вызовы дают разные коды."""
    codes = {service.generate_invite_code() for _ in range(30)}
    assert len(codes) > 1


# --- normalize_full_name ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("Абрамчик С.Г.", "Абрамчик С.Г."),
    ("абрамчик светлана геннадьевна", "Абрамчик С.Г."),
    ("Абрамчик С Г", "Абрамчик С.Г."),
    ("  Абрамчик  С.  Г.  ", "Абрамчик С.Г."),
    ("АБРАМЧИК С.Г.", "Абрамчик С.Г."),
    ("Абрамчик Светлана", "Абрамчик С."),
    ("Абрамчик С.", "Абрамчик С."),
    ("иванов иван иванович", "Иванов И.И."),
    ("Петров Пётр", "Петров П."),
])
def test_normalize_full_name_ok(raw: str, expected: str) -> None:
    """Разные варианты написания приводятся к «Фамилия И.О.»."""
    assert service.normalize_full_name(raw) == expected


@pytest.mark.parametrize("raw", ["Хренов", "", "   ", None, "...", "123"])
def test_normalize_full_name_none(raw) -> None:
    """Без инициалов или без текста — None."""
    assert service.normalize_full_name(raw) is None


def test_normalize_full_name_caps_initials() -> None:
    """Инициалы всегда заглавные, фамилия с заглавной буквы."""
    assert service.normalize_full_name("абрамчик с.г.") == "Абрамчик С.Г."
    assert service.normalize_full_name("аБрАмЧиК сВеТлАнА") == "Абрамчик С."


# --- find_group_by_name / suggest_group_names ---

def test_find_group_exact_and_normalized(conn) -> None:
    """Точное имя и «25кад» находят одну и ту же группу."""
    assert service.find_group_by_name(conn, "25КАД") == GROUP
    assert service.find_group_by_name(conn, "25кад") == GROUP
    assert service.find_group_by_name(conn, "25 кад") == GROUP


def test_find_group_fuzzy_typo(conn) -> None:
    """«25КД» находит «25КАД» через fuzzy."""
    assert service.find_group_by_name(conn, "25КД") == GROUP


def test_find_group_keeps_leading_zero(conn) -> None:
    """«026КАД» и «26КАД» — разные группы, ноль не срезается."""
    assert service.find_group_by_name(conn, "026 кад") == OTHER_GROUP
    assert service.find_group_by_name(conn, "26кад") == "26КАД"


def test_find_group_unknown_returns_none(conn) -> None:
    """Совсем не похожий ввод — None."""
    assert service.find_group_by_name(conn, "!!!") is None
    assert service.find_group_by_name(conn, "") is None


def test_suggest_group_names(conn) -> None:
    """Подсказки возвращают до трёх похожих групп."""
    found = service.suggest_group_names(conn, "25КД")
    assert GROUP in found
    assert len(found) <= service.MAX_SUGGESTIONS


def test_get_all_group_names_from_schedule(conn) -> None:
    """Список групп берётся из расписания, без дублей и отсортирован."""
    names = service.get_all_group_names(conn)
    assert names == sorted(names)
    assert GROUP in names
    assert len(names) == len(set(names))
# --- create_group ---

def test_create_group_creates_code_and_starosta(conn) -> None:
    """Создание группы: код выдан, создатель стал старостой."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")

    assert len(code) == 6 and code.isdigit()
    group = att_db.get_group(conn, GROUP)
    assert group is not None
    assert group["invite_code"] == code
    assert group["starosta_tg_id"] == STAROSTA_ID
    assert group["created_by"] == STAROSTA_ID

    student = att_db.get_student(conn, STAROSTA_ID)
    assert student["role"] == "starosta"
    assert student["group_name"] == GROUP


def test_create_group_code_is_unique(conn) -> None:
    """У разных групп разные коды."""
    first = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    second = service.create_group(conn, OTHER_GROUP, 2001, "Иванов И.И.")
    assert first != second


# --- join_group ---

def test_join_group_by_code(conn, group_with_starosta) -> None:
    """Код найден → студент добавлен с ролью student."""
    result = service.join_group(conn, STUDENT_ID, group_with_starosta,
                                "Иванов И.И.")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["group_name"] == GROUP

    student = att_db.get_student(conn, STUDENT_ID)
    assert student["role"] == "student"
    assert student["full_name"] == "Иванов И.И."


def test_join_group_unknown_code(conn, group_with_starosta) -> None:
    """Несуществующий код → ошибка code_not_found."""
    result = service.join_group(conn, STUDENT_ID, "999999", "Иванов И.И.")
    assert result["ok"] is False
    assert result["error"] == "code_not_found"
    assert att_db.get_student(conn, STUDENT_ID) is None


def test_join_group_bad_code_format(conn) -> None:
    """Код не из 6 цифр → ошибка формата."""
    for bad in ("", "abc", "12345", "1234567"):
        result = service.join_group(conn, STUDENT_ID, bad, "Иванов И.И.")
        assert result["ok"] is False
        assert result["error"] == "bad_code_format"


def test_join_group_already_in_other_group(conn, group_with_starosta) -> None:
    """Уже в другой группе → ошибка already_in_group."""
    other_code = service.create_group(conn, OTHER_GROUP, 2001, "Петров П.П.")
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")

    result = service.join_group(conn, STUDENT_ID, other_code, "Иванов И.И.")

    assert result["ok"] is False
    assert result["error"] == "already_in_group"
    # Остался в первой группе.
    assert att_db.get_student(conn, STUDENT_ID)["group_name"] == GROUP


def test_join_group_same_group_twice_is_ok(conn, group_with_starosta) -> None:
    """Повторный ввод кода своей же группы — не ошибка, дубля нет."""
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")
    result = service.join_group(conn, STUDENT_ID, group_with_starosta,
                                "Иванов И.И.")

    assert result["ok"] is True
    assert len(service.get_group_students(conn, GROUP)) == 2  # староста + студент


def test_join_group_bad_name(conn, group_with_starosta) -> None:
    """ФИО не распознано → ошибка bad_name, студент не добавлен."""
    result = service.join_group(conn, STUDENT_ID, group_with_starosta, "")

    assert result["ok"] is False
    assert result["error"] == "bad_name"
    assert att_db.get_student(conn, STUDENT_ID) is None
# --- set_deputy ---

def test_set_deputy_by_starosta(conn, group_with_starosta) -> None:
    """Староста назначает студента замом."""
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")

    ok = service.set_deputy(conn, GROUP, STUDENT_ID, STAROSTA_ID)

    assert ok is True
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "deputy"
    assert att_db.get_group(conn, GROUP)["deputy_tg_id"] == STUDENT_ID


def test_set_deputy_by_student_denied(conn, group_with_starosta) -> None:
    """Студент не может назначать зама."""
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")

    ok = service.set_deputy(conn, GROUP, STUDENT_ID, STUDENT_ID)

    assert ok is False
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "student"


def test_set_deputy_starosta_cannot_self(conn, group_with_starosta) -> None:
    """Староста не может назначить замом себя."""
    assert service.set_deputy(conn, GROUP, STAROSTA_ID, STAROSTA_ID) is False
    assert att_db.get_student(conn, STAROSTA_ID)["role"] == "starosta"


def test_set_deputy_from_other_group_denied(conn, group_with_starosta) -> None:
    """Нельзя назначить замом человека из другой группы."""
    other_code = service.create_group(conn, OTHER_GROUP, 2001, "Петров П.П.")
    service.join_group(conn, STUDENT_ID, other_code, "Иванов И.И.")

    ok = service.set_deputy(conn, GROUP, STUDENT_ID, STAROSTA_ID)

    assert ok is False
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "student"


def test_set_deputy_replaces_previous(conn, group_with_starosta) -> None:
    """Зам в группе один: прежний становится обычным студентом."""
    service.join_group(conn, 3001, group_with_starosta, "Первый П.П.")
    service.join_group(conn, 3002, group_with_starosta, "Второй В.В.")

    service.set_deputy(conn, GROUP, 3001, STAROSTA_ID)
    service.set_deputy(conn, GROUP, 3002, STAROSTA_ID)

    assert att_db.get_student(conn, 3001)["role"] == "student"
    assert att_db.get_student(conn, 3002)["role"] == "deputy"
    assert att_db.get_group(conn, GROUP)["deputy_tg_id"] == 3002


def test_deputy_candidates_exclude_starosta(conn, group_with_starosta) -> None:
    """В кандидатах нет старосты."""
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")
    candidates = service.deputy_candidates(conn, GROUP)

    ids = [c["tg_id"] for c in candidates]
    assert STUDENT_ID in ids
    assert STAROSTA_ID not in ids


# --- regenerate_invite_code ---

def test_regenerate_code_by_starosta(conn, group_with_starosta) -> None:
    """Староста меняет код; старый перестаёт работать."""
    new_code = service.regenerate_invite_code(conn, GROUP, STAROSTA_ID)

    assert new_code is not None
    assert new_code != group_with_starosta
    assert att_db.get_group(conn, GROUP)["invite_code"] == new_code

    # Старый код больше не находит группу.
    old = service.join_group(conn, 4001, group_with_starosta, "Старый С.С.")
    assert old["ok"] is False
    # Новый работает.
    fresh = service.join_group(conn, 4002, new_code, "Новый Н.Н.")
    assert fresh["ok"] is True


def test_regenerate_code_by_student_denied(conn, group_with_starosta) -> None:
    """Не староста код не меняет."""
    service.join_group(conn, STUDENT_ID, group_with_starosta, "Иванов И.И.")

    assert service.regenerate_invite_code(conn, GROUP, STUDENT_ID) is None
    assert att_db.get_group(conn, GROUP)["invite_code"] == group_with_starosta


# --- список и роли ---

def test_get_group_students_sorted(conn, group_with_starosta) -> None:
    """Студенты возвращаются по алфавиту."""
    service.join_group(conn, 5001, group_with_starosta, "Яковлев Я.Я.")
    service.join_group(conn, 5002, group_with_starosta, "Абрамов А.А.")

    names = [s["full_name"] for s in service.get_group_students(conn, GROUP)]
    assert names == sorted(names)


def test_role_helpers(conn, group_with_starosta) -> None:
    """Роли и их подписи согласованы."""
    assert service.is_starosta(conn, STAROSTA_ID) is True
    assert service.is_starosta(conn, STUDENT_ID) is False
    assert service.is_group_admin(conn, GROUP, STAROSTA_ID) is True
    assert service.is_group_admin(conn, GROUP, STUDENT_ID) is False

    assert service.role_label("starosta") == "⭐ староста"
    assert service.role_label("deputy") == "👤 зам"
    assert service.role_label("student") == ""


def test_is_group_created(conn, group_with_starosta) -> None:
    """Признак «группа уже создана» для защиты от повторного создания."""
    assert service.is_group_created(conn, GROUP) is True
    assert service.is_group_created(conn, OTHER_GROUP) is False