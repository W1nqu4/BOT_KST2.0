"""Админ-логика посещаемости: сводки и обслуживание групп (этап 1).

Только SQL и агрегаты: доступ по роли (кто админ) и тексты ответов — в
:mod:`bot.attendance.admin_handlers`. Ключ к правам — ``settings.admin_ids``
(env ``ADMIN_IDS``), никаких хардкодов tg_id в коде.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.attendance import db as att_db
from bot.config import NOTIFY_CONCURRENCY, NOTIFY_SEND_DELAY_SECONDS

logger = logging.getLogger(__name__)


def is_admin(tg_id: int, settings) -> bool:
    """True, если tg_id входит в ``settings.admin_ids``.

    Args:
        tg_id: Telegram id пользователя.
        settings: настройки приложения (может быть None в тестах).

    Returns:
        True для админа; False, если настройки недоступны или список пуст —
        без админов не должен проходить никто.
    """
    if settings is None:
        return False
    admin_ids = getattr(settings, "admin_ids", ()) or ()
    return tg_id in set(admin_ids)


def list_all_groups_stats(conn) -> list[dict]:
    """Сводка по всем созданным группам.

    Returns:
        Список словарей ``{'group_name', 'students_count', 'starosta_name',
        'invite_code', 'created_at'}``, отсортированный по имени группы.
        ``starosta_name`` — пустая строка, если староста не найден.
    """
    groups = att_db.get_all_groups(conn)
    result: list[dict] = []

    for group in groups:
        group_name = str(group["group_name"])
        starosta_id = group.get("starosta_tg_id")

        starosta_name = ""
        if starosta_id:
            student = att_db.get_student(conn, int(starosta_id))
            if student is not None:
                starosta_name = str(student["full_name"])

        result.append({
            "group_name": group_name,
            "students_count": att_db.count_group_students(conn, group_name),
            "starosta_name": starosta_name,
            "invite_code": str(group.get("invite_code") or ""),
            "created_at": str(group.get("created_at") or ""),
        })
    return result


def list_all_students(conn) -> list[dict]:
    """Все студенты всех групп, сгруппированные по группе.

    Returns:
        Список словарей ``{'tg_id', 'group_name', 'full_name', 'role',
        'joined_at'}``; порядок — по имени группы, внутри группы по ФИО.
    """
    rows = conn.execute(
        "SELECT tg_id, group_name, full_name, role, joined_at"
        " FROM students"
        " ORDER BY group_name, full_name COLLATE NOCASE, tg_id"
    ).fetchall()
    return [dict(row) for row in rows]


def get_bot_stats(conn) -> dict:
    """Сводные счётчики для админ-панели.

    Returns:
        ``{'groups': N, 'students': M, 'active_codes': K}``, где
        ``active_codes`` — группы с непустым кодом приглашения (то есть
        группы, куда ещё можно войти по коду).
    """
    return {
        "groups": int(conn.execute(
            "SELECT COUNT(*) FROM study_groups"
        ).fetchone()[0]),
        "students": int(conn.execute(
            "SELECT COUNT(*) FROM students"
        ).fetchone()[0]),
        "active_codes": int(conn.execute(
            "SELECT COUNT(*) FROM study_groups"
            " WHERE invite_code IS NOT NULL AND TRIM(invite_code) != ''"
        ).fetchone()[0]),
    }


def delete_group(conn, group_name: str) -> dict:
    """Удалить группу вместе со всеми её студентами.

    Студенты удаляются первыми: на ``students.group_name`` есть внешний ключ
    на ``study_groups``, поэтому обратный порядок нарушил бы ссылочную
    целостность (``PRAGMA foreign_keys=ON``).

    Args:
        conn: соединение SQLite.
        group_name: имя группы.

    Returns:
        ``{'ok': bool, 'students_deleted': N}``.
    """
    from bot.db import transaction

    group = att_db.get_group(conn, group_name)
    if group is None:
        return {"ok": False, "students_deleted": 0}

    students = att_db.count_group_students(conn, group_name)
    with transaction(conn):
        conn.execute("DELETE FROM students WHERE group_name = ?", (group_name,))
        conn.execute("DELETE FROM study_groups WHERE group_name = ?",
                     (group_name,))

    logger.info("study group deleted",
                extra={"group": group_name, "students": students})
    return {"ok": True, "students_deleted": students}
async def broadcast_to_all(conn, bot, text: str, settings,
                           throttle: bool = True) -> dict:
    """Разослать сообщение всем студентам всех групп.

    Ограничения те же, что у рассылки замен: не более
    :data:`bot.config.NOTIFY_CONCURRENCY` отправок одновременно и пауза
    :data:`bot.config.NOTIFY_SEND_DELAY_SECONDS` между ними — иначе Telegram
    включит флуд-контроль.

    Заблокировавшие бота (``TelegramForbiddenError``) попадают в ``failed``.
    Поля ``is_active`` в ``students`` нет (в отличие от ``users``), поэтому
    помечать неактивным некого — просто считаем ошибку.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        text: текст сообщения (HTML).
        settings: настройки (не используется; оставлен для совместимости).
        throttle: выдерживать ли паузу (в тестах — False).

    Returns:
        ``{'sent': N, 'failed': M, 'total': T}``.
    """
    students = list_all_students(conn)
    total = len(students)
    sent = 0
    failed = 0
    semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)

    async def _one(tg_id: int) -> bool:
        """Отправить одному студенту; False при недоставке."""
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
            return True
        except TelegramForbiddenError:
            logger.info("broadcast: student blocked the bot",
                        extra={"tg_id": tg_id})
            return False
        except TelegramRetryAfter as exc:
            pause = int(getattr(exc, "retry_after", 1)) + 1
            logger.warning("broadcast: flood control",
                           extra={"tg_id": tg_id, "retry_after": pause})
            await asyncio.sleep(pause)
            try:
                await bot.send_message(tg_id, text, parse_mode="HTML")
                return True
            except Exception:
                return False
        except Exception as exc:
            logger.warning("broadcast: send failed",
                           extra={"tg_id": tg_id, "error": repr(exc)})
            return False

    for student in students:
        tg_id = int(student["tg_id"])
        async with semaphore:
            if await _one(tg_id):
                sent += 1
            else:
                failed += 1
            if throttle and NOTIFY_SEND_DELAY_SECONDS:
                await asyncio.sleep(NOTIFY_SEND_DELAY_SECONDS)

    logger.info("broadcast finished",
                extra={"sent": sent, "failed": failed, "total": total})
    return {"sent": sent, "failed": failed, "total": total}