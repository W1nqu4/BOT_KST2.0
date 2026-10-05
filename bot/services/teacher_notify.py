"""Уведомление админов о заявке преподавателя — общее для TG и VK.

Функция вызывается из двух мест: Telegram-хендлер заявки
(:mod:`bot.handlers.teacher_apply`) и VK-хендлер
(:mod:`bot_vk.handlers`). Поэтому модуль обязан оставаться импортируемым
без aiogram: ``aiogram`` подгружается ЛЕНИВО, внутри :func:`application_kb`.
Иначе ``import bot.services.teacher_notify`` в ``bot_vk`` поднял бы весь
Telegram-стек в процессе VK-бота.

Текст уведомления один и тот же, отличается только строка «Источник: …» —
админ сразу видит, откуда пришла заявка (из TG или из VK).
"""
from __future__ import annotations

import logging
from html import escape

from bot import db

logger = logging.getLogger(__name__)

# Как показывать источник заявки в уведомлении.
SOURCE_LABELS = {"tg": "Telegram", "vk": "VK"}

# Сколько ФИО/статусов упоминать. Значение по умолчанию — на случай, если
# вызывающий передал неизвестный источник.
SOURCE_UNKNOWN = "неизвестно"


def application_kb():
    """Кнопка «Обработать заявки» для уведомления админу.

    Ведёт на общий список заявок (callback ``teacher_refresh``): из уведомления
    админ попадает прямо к модерации, не вспоминая команду.

    Импорт aiogram — внутри функции, см. модульный docstring.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚙️ Обработать заявки",
            callback_data="teacher_refresh",
        )],
    ])


def _resolve_admin_ids(settings) -> tuple[int, ...]:
    """Кого уведомлять: из переданных настроек, иначе из окружения.

    Настройки приходят из хендлеров Telegram. Вызов из VK передаёт ``None``
    (там нет объекта ``Settings``), поэтому значения читаются из env —
    ``ADMIN_IDS`` есть в окружении процесса в любом случае. Ошибка
    конфигурации (нет ``BOT_TOKEN``) не должна валить уведомление: в этом
    случае просто некому писать, о чём и предупреждаем логом.

    Returns:
        Кортеж Telegram id (может быть пустым).
    """
    if settings is not None:
        return tuple(getattr(settings, "admin_ids", ()) or ())

    from bot.config import ConfigError, Settings

    try:
        return Settings.from_env().admin_ids
    except ConfigError as exc:
        logger.warning(
            "teacher application: настройки недоступны, уведомление не ушло",
            extra={"error": str(exc)},
        )
        return ()


async def notify_admin_about_teacher_application(
    bot_aiogram, conn, tg_id: int, full_name: str, source: str,
    settings=None,
) -> bool:
    """Сообщить админам о новой заявке преподавателя.

    Без уведомления заявка лежала бы до тех пор, пока админ сам не заглянет в
    список, — преподаватель ждал бы неизвестно сколько.

    Args:
        bot_aiogram: объект ``aiogram.Bot`` (в VK-боте приходит из
            :mod:`bot_vk.tg_bridge`). Если его нет, уведомить нечем: возвращаем
            False и пишем warning — выдумывать одноразовый клиент здесь нельзя,
            иначе тест или VK-процесс дёрнули бы реальный Telegram.
        conn: соединение SQLite.
        tg_id: кто подал заявку (заявки живут в таблице ``teachers`` по tg_id).
        full_name: ФИО из справочника.
        source: ``'tg'`` | ``'vk'`` — откуда пришла заявка.
        settings: настройки с ``admin_ids`` (в TG-хендлерах приходит
            автоматически).

    Returns:
        True, если уведомление доставлено хотя бы одному админу.
    """
    admin_ids = _resolve_admin_ids(settings)
    label = SOURCE_LABELS.get(str(source or "").lower(), SOURCE_UNKNOWN)
    pending = len(db.list_pending_teachers(conn))
    text = (
        "👨‍🏫 <b>Новая заявка преподавателя</b>\n\n"
        f"ФИО: <b>{escape(full_name)}</b>\n"
        f"От: id <code>{tg_id}</code>\n"
        f"Источник: {label}\n"
        f"Заявок в очереди: {pending}\n\n"
        "Одобрить или отклонить: /teachers"
    )

    if not admin_ids:
        logger.warning(
            "teacher application: ADMIN_IDS пуст, уведомление не отправлено",
            extra={"tg_id": tg_id, "source": label},
        )
        return False

    if bot_aiogram is None:
        # Так выглядит заявка из VK, когда процесс запущен без Telegram-бота
        # (например, только `python -m bot_vk.main_vk`): уведомить некого.
        logger.warning(
            "teacher application: нет Telegram-бота, уведомление не отправлено",
            extra={"tg_id": tg_id, "source": label},
        )
        return False

    delivered = False
    for admin_id in admin_ids:
        try:
            await bot_aiogram.send_message(
                admin_id, text, parse_mode="HTML",
                reply_markup=application_kb(),
            )
            delivered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "teacher application notify failed",
                extra={"admin_id": admin_id, "error": repr(exc)},
            )
    return delivered


__all__ = ("application_kb", "notify_admin_about_teacher_application")