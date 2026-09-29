"""Логика посещаемости: коды приглашения, ФИО, регистрация, роли (этап 1).

Здесь нет ни SQL, ни Telegram: только правила. Это позволяет тестировать
логику напрямую, без диспетчера и заглушек бота.
"""
from __future__ import annotations

import logging
import random
import re

from bot.attendance import db as att_db
from bot.attendance.models import (
    ROLE_DEPUTY,
    ROLE_STAROSTA,
    ROLE_STUDENT,
)
from bot.parsers.groups import normalize_group_name

logger = logging.getLogger(__name__)

# Длина кода приглашения (цифры).
INVITE_CODE_LENGTH = 6

# Первая цифра кода не может быть нулём: код часто диктуют голосом и
# копируют вручную, а ведущий ноль теряется («0482931» → «482931»).
INVITE_CODE_FIRST_DIGIT = "123456789"

# Сколько похожих групп предлагать при ошибке в названии.
MAX_SUGGESTIONS = 3

# Разделители в ФИО: пробелы (включая неразрывный) и точки.
_NAME_SPLIT_RE = re.compile(r"[\s\u00a0.]+")

# Минимум слов в ФИО: фамилия + хотя бы одна буква имени/отчества.
MIN_NAME_PARTS = 2


def generate_invite_code() -> str:
    """6-значный код из цифр, без ведущего нуля.

    Ведущий ноль исключён намеренно: код диктуют голосом и переписывают
    вручную, и «0482931» превратился бы в «482931» — код не нашёлся бы.

    Returns:
        Строка из 6 цифр, первая из которых не «0».
    """
    first = random.choice(INVITE_CODE_FIRST_DIGIT)
    rest = "".join(random.choice("0123456789")
                   for _ in range(INVITE_CODE_LENGTH - 1))
    return first + rest


def normalize_full_name(text: str | None) -> str | None:
    """Привести ФИО к формату «Фамилия И.О.».

    Принимает любые распространённые варианты написания и приводит их к
    одному виду, чтобы список группы читался единообразно:

    - ``«Абрамчик С.Г.»`` → ``«Абрамчик С.Г.»``
    - ``«абрамчик светлана геннадьевна»`` → ``«Абрамчик С.Г.»``
    - ``«Абрамчик С Г»`` → ``«Абрамчик С.Г.»``
    - ``«  Абрамчик  С.  Г.  »`` → ``«Абрамчик С.Г.»``

    Args:
        text: то, что ввёл студент.

    Returns:
        ``«Фамилия И.О.»`` или None, если распарсить не удалось (нет слов,
        кроме фамилии; нет букв в инициалах).
    """
    if not text:
        return None

    parts = [part for part in _NAME_SPLIT_RE.split(str(text).strip()) if part]
    if len(parts) < MIN_NAME_PARTS:
        return None

    surname = parts[0].capitalize()
    if not surname.isalpha():
        # Фамилия должна быть словом, а не цифрами/символами.
        return None

    initials: list[str] = []
    for part in parts[1:]:
        letter = part[0]
        if not letter.isalpha():
            continue
        # «светлана» → «С», «С» → «С», «геннадьевна» → «Г».
        initials.append(letter.upper())
        if len(initials) == 2:
            break

    if not initials:
        return None

    return f"{surname} {'.'.join(initials)}."


def find_group_by_name(conn, query: str) -> str | None:
    """Найти группу в расписании по неточному вводу (fuzzy).

    Сравнение идёт по списку групп из ``schedule_cache`` — это единственный
    источник «какие группы существуют в КСТ». Ведущий ноль не срезается:
    «25КАД» и «025КАД» — разные группы.

    Args:
        conn: соединение SQLite.
        query: то, что ввёл пользователь («25кад», «26 КАД»).

    Returns:
        Точное имя группы из расписания или None, если совпадения нет.
    """
    import difflib

    normalized = normalize_group_name(query or "")
    if not normalized:
        return None

    available = get_all_group_names(conn)
    if not available:
        return None
    if normalized in available:
        return normalized

    matches = difflib.get_close_matches(
        normalized, available, n=1, cutoff=0.6
    )
    return matches[0] if matches else None


def suggest_group_names(conn, query: str) -> list[str]:
    """До трёх похожих групп для подсказки (когда точной группы нет).

    Returns:
        Список ближайших названий (может быть пустым).
    """
    import difflib

    normalized = normalize_group_name(query or "")
    available = get_all_group_names(conn)
    if not normalized or not available:
        return []
    return difflib.get_close_matches(
        normalized, available, n=MAX_SUGGESTIONS, cutoff=0.5
    )


def get_all_group_names(conn) -> list[str]:
    """Все имена групп из расписания (DISTINCT, отсортированы).

    Источник — ``schedule_cache``: группа, которой нет в расписании, не может
    быть создана в системе посещаемости.
    """
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM schedule_cache ORDER BY group_name"
    ).fetchall()
    return [str(row["group_name"]) for row in rows]
def create_group(conn, group_name: str, starosta_tg_id: int,
                 full_name: str) -> str:
    """Создать группу и назначить создателя старостой.

    Args:
        conn: соединение SQLite.
        group_name: точное имя группы из расписания.
        starosta_tg_id: tg_id создателя.
        full_name: нормализованное ФИО создателя.

    Returns:
        Код приглашения группы.
    """
    code = generate_invite_code()
    att_db.insert_group(conn, group_name, code, created_by=starosta_tg_id,
                        starosta_tg_id=starosta_tg_id)
    att_db.insert_student(conn, starosta_tg_id, group_name, full_name,
                          role=ROLE_STAROSTA)
    logger.info("study group created",
                extra={"group": group_name, "starosta": starosta_tg_id})
    return code


def join_group(conn, tg_id: int, invite_code: str, full_name: str) -> dict:
    """Зарегистрировать студента в группе по коду приглашения.

    Args:
        conn: соединение SQLite.
        tg_id: tg_id студента.
        invite_code: код от старосты.
        full_name: нормализованное ФИО.

    Returns:
        ``{'ok': bool, 'error': str | None, 'group_name': str}``.
        ``error`` — машинный код причины: ``bad_code_format``,
        ``code_not_found``, ``already_in_group``, ``bad_name``.
    """
    code = (invite_code or "").strip()
    if not code.isdigit() or len(code) != INVITE_CODE_LENGTH:
        return {"ok": False, "error": "bad_code_format", "group_name": ""}

    group = att_db.get_group_by_code(conn, code)
    if group is None:
        return {"ok": False, "error": "code_not_found", "group_name": ""}

    group_name = str(group["group_name"])

    existing = att_db.get_student(conn, tg_id)
    if existing is not None:
        # Один студент — одна группа. Повторный ввод кода своей же группы не
        # ошибка: сообщаем, что он уже там.
        if str(existing["group_name"]) == group_name:
            return {"ok": True, "error": None, "group_name": group_name}
        return {"ok": False, "error": "already_in_group",
                "group_name": group_name}

    if not full_name:
        return {"ok": False, "error": "bad_name", "group_name": group_name}

    att_db.insert_student(conn, tg_id, group_name, full_name,
                          role=ROLE_STUDENT)
    logger.info("student joined group",
                extra={"group": group_name, "tg_id": tg_id})
    return {"ok": True, "error": None, "group_name": group_name}
def get_group(conn, group_name: str) -> dict | None:
    """Группа по имени (обёртка над :mod:`bot.attendance.db`)."""
    return att_db.get_group(conn, group_name)


def get_student(conn, tg_id: int) -> dict | None:
    """Студент по tg_id (обёртка над :mod:`bot.attendance.db`)."""
    return att_db.get_student(conn, tg_id)


def get_group_students(conn, group_name: str) -> list[dict]:
    """Студенты группы по алфавиту."""
    return att_db.get_group_students(conn, group_name)


def get_all_groups(conn) -> list[dict]:
    """Все созданные группы."""
    return att_db.get_all_groups(conn)


def is_group_admin(conn, group_name: str, tg_id: int) -> bool:
    """Имеет ли пользователь права старосты в указанной группе."""
    group = att_db.get_group(conn, group_name)
    if group is None:
        return False
    return group.get("starosta_tg_id") == tg_id


def is_starosta(conn, tg_id: int) -> bool:
    """Является ли пользователь старостой своей группы."""
    student = att_db.get_student(conn, tg_id)
    return student is not None and str(student["role"]) == ROLE_STAROSTA


def is_group_created(conn, group_name: str) -> bool:
    """Создана ли группа в системе посещаемости."""
    return att_db.get_group(conn, group_name) is not None


def regenerate_invite_code(conn, group_name: str,
                           requester_tg_id: int) -> str | None:
    """Сменить код приглашения. Только староста группы.

    Args:
        conn: соединение SQLite.
        group_name: группа.
        requester_tg_id: кто запросил смену.

    Returns:
        Новый код или None, если прав нет.
    """
    if not is_group_admin(conn, group_name, requester_tg_id):
        logger.info("invite code regeneration denied",
                    extra={"group": group_name, "tg_id": requester_tg_id})
        return None

    code = generate_invite_code()
    att_db.update_invite_code(conn, group_name, code)
    logger.info("invite code regenerated", extra={"group": group_name})
    return code


def set_deputy(conn, group_name: str, student_tg_id: int,
               requester_tg_id: int) -> bool:
    """Назначить зама. Только староста и только студента своей группы.

    Староста не может назначить себя (зам — отдельная роль для студента) и не
    может назначить человека из другой группы.

    Args:
        conn: соединение SQLite.
        group_name: группа.
        student_tg_id: кого назначаем.
        requester_tg_id: кто назначает (должен быть старостой).

    Returns:
        True, если зам назначен.
    """
    if not is_group_admin(conn, group_name, requester_tg_id):
        return False
    if student_tg_id == requester_tg_id:
        # Староста не может быть замом самому себе.
        return False

    student = att_db.get_student(conn, student_tg_id)
    if student is None or str(student["group_name"]) != group_name:
        return False

    # Зам в группе один: прежний снова становится студентом.
    group = att_db.get_group(conn, group_name)
    previous = group.get("deputy_tg_id") if group else None
    if previous and previous != student_tg_id:
        att_db.set_student_role(conn, int(previous), ROLE_STUDENT)

    att_db.set_student_role(conn, student_tg_id, ROLE_DEPUTY)
    att_db.set_group_deputy(conn, group_name, student_tg_id)
    logger.info("deputy assigned",
                extra={"group": group_name, "deputy": student_tg_id})
    return True


def deputy_candidates(conn, group_name: str) -> list[dict]:
    """Кого можно назначить замом: студенты без старосты и текущего зама."""
    group = att_db.get_group(conn, group_name)
    starosta = group.get("starosta_tg_id") if group else None
    deputy = group.get("deputy_tg_id") if group else None

    return [
        student for student in att_db.get_group_students(conn, group_name)
        if student["tg_id"] != starosta and student["tg_id"] != deputy
        and str(student["role"]) != ROLE_STAROSTA
    ]


def role_of(student: dict | None) -> str:
    """Роль студента строкой (``'student'``, если студента нет)."""
    if student is None:
        return ROLE_STUDENT
    return str(student.get("role") or ROLE_STUDENT)


def role_label(role: str) -> str:
    """Человекочитаемая подпись роли для списка группы."""
    return {
        ROLE_STAROSTA: "⭐ староста",
        ROLE_DEPUTY: "👤 зам",
        ROLE_STUDENT: "",
    }.get(role, "")