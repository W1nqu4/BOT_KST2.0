"""Профиль пользователя Mini App: имя, группа, роль.

Роль считается так же, как в боте: администратор из ``ADMIN_IDS`` важнее
роли в группе, затем ``students.role``, иначе — студент. Дублировать эту
иерархию нельзя: от неё зависит, какие экраны показывать (управление группой
доступно старосте и админу).
"""
from __future__ import annotations

from bot import db
from bot.attendance import db as att_db
from bot.attendance.models import ROLE_STUDENT

# Роли, которые понимает фронт (тип Role в webapp/lib).
ROLE_ADMIN = "admin"


def resolve_role(conn, tg_id: int, settings=None) -> str:
    """Роль пользователя: ``admin`` | ``starosta`` | ``deputy`` | ``student``.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.
        settings: настройки приложения; ``admin_ids`` задаёт админов.

    Returns:
        Строка роли. Админ из ``ADMIN_IDS`` перекрывает роль в группе.
    """
    admin_ids = getattr(settings, "admin_ids", ()) if settings else ()
    if tg_id in tuple(admin_ids or ()):
        return ROLE_ADMIN

    student = att_db.get_student(conn, tg_id)
    if student is not None:
        role = str(student.get("role") or "").strip()
        if role:
            return role
    return ROLE_STUDENT


def profile_payload(conn, tg_id: int, settings=None) -> dict:
    """Профиль → JSON для экрана «Профиль».

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.
        settings: настройки приложения (для ``admin_ids``).

    Returns:
        ``{'tg_id', 'name', 'group', 'role'}``. Имя берётся из группы
        (``students.full_name`` — «Фамилия И.О.»), если студент зарегистрирован,
        иначе из ``users.full_name`` (имя из Telegram для админки).
    """
    student = att_db.get_student(conn, tg_id)
    user = db.get_user(conn, tg_id)

    name = ""
    group = ""
    if student is not None:
        name = str(student.get("full_name") or "").strip()
        group = str(student.get("group_name") or "").strip()
    if user is not None:
        if not name:
            name = str(user["full_name"] or "").strip()
        if not group:
            group = str(user["group_name"] or "").strip()

    return {
        "tg_id": tg_id,
        "name": name,
        "group": group,
        "role": resolve_role(conn, tg_id, settings),
    }


__all__ = ["ROLE_ADMIN", "profile_payload", "resolve_role"]