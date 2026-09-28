"""Чаты групп и каналов: /setup, /unsync, /schedule (шаг 2).

Сценарий: админ добавляет бота в группу или канал КСТ, привязывает чат к
учебной группе командой ``/setup 25КАД`` — и замена по этой группе приходит
прямо в чат, без личной подписки каждого студента.

Особенности:

- **команды только в чатах.** В личке ``/setup`` бессмыслен: там группа
  выбирается через ``/start`` (см. ``bot/handlers/start.py``);
- **только админ чата.** Проверяем фактический статус через
  ``get_chat_member`` — доверять флагу из апдейта нельзя;
- **в канале бот должен быть админом**, иначе Telegram не даст ему читать
  команды; отвечаем понятной инструкцией;
- **anti-flood**: ``/setup`` не чаще одного раза в минуту на ``chat_id``.
"""
from __future__ import annotations

import logging
import time
from html import escape

from aiogram import Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message

from bot import db
from bot.utils.security import sanitize_group

logger = logging.getLogger(__name__)

router = Router(name="group_chats")

# Типы чатов, в которых работает привязка.
# Значения берём через ``.value``: строковое представление enum-а в aiogram
# это «ChatType.SUPERGROUP», а в объектах чата лежит уже готовая строка
# «supergroup» — сравнивать нужно именно значения.
SUPPORTED_CHAT_TYPES = frozenset({
    ChatType.GROUP.value, ChatType.SUPERGROUP.value, ChatType.CHANNEL.value,
})

# Статусы участника, которым разрешено управлять привязкой.
ADMIN_STATUSES = frozenset({
    ChatMemberStatus.CREATOR.value, ChatMemberStatus.ADMINISTRATOR.value,
})

# Статусы «бот в чате есть».
PRESENT_STATUSES = frozenset({
    ChatMemberStatus.MEMBER.value, ChatMemberStatus.ADMINISTRATOR.value,
    ChatMemberStatus.CREATOR.value, ChatMemberStatus.RESTRICTED.value,
})

# Статусы «бот из чата ушёл/выгнан».
GONE_STATUSES = frozenset({
    ChatMemberStatus.LEFT.value, ChatMemberStatus.KICKED.value,
})

# Anti-flood для /setup: не чаще одного раза в минуту на чат.
SETUP_COOLDOWN_SECONDS = 60

# Сколько похожих групп предлагать при опечатке.
MAX_SUGGESTIONS = 3

# Просьба настроить чат (используется и при добавлении, и при /setup без args).
SETUP_HINT = (
    "Привет! Напиши /setup <группа>, например <code>/setup 25КАД</code>"
)

CHANNEL_NEEDS_ADMIN = (
    "Сделайте меня администратором канала, иначе не смогу отвечать."
)

NOT_CHAT_TEXT = (
    "🤖 Эта команда работает в групповом чате или канале.\n"
    "В личке группу можно выбрать через /start."
)

_chat_cooldown: dict[int, float] = {}


def _value_of(raw) -> str:
    """Строковое значение enum-а или строки (``ChatType.SUPERGROUP`` → ``supergroup``)."""
    value = getattr(raw, "value", raw)
    return str(value) if value is not None else ""


def _chat_type(chat) -> str:
    """Тип чата строкой (aiogram отдаёт enum, в БД пишем значение)."""
    return _value_of(getattr(chat, "type", ""))


def _status_value(member) -> str:
    """Статус участника строкой (``creator`` / ``administrator`` / ...)."""
    return _value_of(getattr(member, "status", ""))


def _is_supported_chat(message: Message) -> bool:
    """Чат поддерживаемого типа (group / supergroup / channel)?"""
    chat = message.chat
    if chat is None:
        return False
    return _chat_type(chat) in SUPPORTED_CHAT_TYPES


def _title_of(message: Message) -> str:
    """Название чата для логов и хранения (может быть пустым)."""
    chat = message.chat
    if chat is None:
        return ""
    return chat.title or chat.full_name or ""


def check_setup_rate_limit(chat_id: int, now: float | None = None) -> bool:
    """Не превышен ли лимит ``/setup`` для чата.

    Args:
        chat_id: id чата.
        now: момент (для тестов); по умолчанию ``time.monotonic()``.

    Returns:
        True — можно обрабатывать; False — слишком часто.
    """
    moment = time.monotonic() if now is None else now
    last = _chat_cooldown.get(chat_id)
    if last is not None and moment - last < SETUP_COOLDOWN_SECONDS:
        return False
    _chat_cooldown[chat_id] = moment
    return True


def reset_setup_rate_limit(chat_id: int | None = None) -> None:
    """Сбросить anti-flood (для тестов)."""
    if chat_id is None:
        _chat_cooldown.clear()
    else:
        _chat_cooldown.pop(chat_id, None)
async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    """Является ли пользователь админом/создателем чата.

    Статус спрашиваем у Telegram, а не берём из апдейта: это единственный
    надёжный источник (в канале обычный участник вообще не виден).

    Args:
        bot: объект Bot.
        chat_id: id чата.
        user_id: tg_id проверяемого.

    Returns:
        True для creator/administrator; False при любой ошибке (безопасный
        отказ: без прав ничего не меняем).
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:
        logger.warning("could not check chat admin",
                       extra={"chat_id": chat_id, "user_id": user_id,
                              "error": repr(exc)})
        return False
    status = _status_value(member)
    return status in ADMIN_STATUSES


async def bot_is_channel_admin(bot, chat_id: int) -> bool:
    """Админ ли сам бот в канале (иначе команды не читаются)."""
    return await bot_is_chat_admin(bot, chat_id)


async def bot_is_chat_admin(bot, chat_id: int) -> bool:
    """Админ ли сам бот в чате (нужно для закрепления расписания, шаг 4).

    Проверяем и в канале, и в группе: закрепить сообщение Telegram разрешает
    только администратору с правом «Закрепление сообщений». Статус спрашиваем
    у Telegram (``get_chat_member`` для самого бота), при ошибке считаем, что
    прав нет — так мы не обещаем пользователю закрепление, которого не будет.

    Args:
        bot: объект Bot.
        chat_id: id чата.

    Returns:
        True для статусов administrator/creator, иначе False.
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as exc:
        logger.warning("could not check bot membership",
                       extra={"chat_id": chat_id, "error": repr(exc)})
        return False
    return _status_value(member) in ADMIN_STATUSES


def _parse_group_argument(raw: str | None) -> str | None:
    """Разобрать аргумент ``/setup`` в нормализованное имя группы.

    Нормализация сама убирает пробелы (``«26 кад» → «26КАД»``), поэтому
    весь аргумент отдаётся целиком: если разделять по пробелу, «/setup 26
    кад» превратилось бы в «26» и не нашлось.
    """
    if not raw:
        return None
    return sanitize_group(raw)


def resolve_group_argument(raw: str | None,
                           available: list[str]) -> str | None:
    """Определить группу из аргумента ``/setup`` с учётом списка групп.

    Сначала пробуем весь аргумент целиком (это покрывает «26 кад»), затем —
    укорачиваем его по словам (покрывает «26КАД лишние слова»). Возвращается
    только та группа, которая есть в расписании.

    Args:
        raw: аргумент команды.
        available: доступные группы из кэша расписания.

    Returns:
        Нормализованное имя группы или None, если распознать не удалось.
    """
    whole = _parse_group_argument(raw)
    if not available:
        # Кэш пуст: проверить принадлежность нечем — доверяем формату.
        return whole
    if whole in available:
        return whole

    tokens = (raw or "").split()
    for size in range(1, len(tokens)):
        candidate = sanitize_group(" ".join(tokens[:size]))
        if candidate in available:
            return candidate
    return whole


def suggest_groups(raw: str, available: list[str]) -> list[str]:
    """Похожие группы при опечатке (difflib, как в регистрации)."""
    import difflib

    return difflib.get_close_matches(
        raw, available, n=MAX_SUGGESTIONS, cutoff=0.5
    )
# --- приветствие при добавлении бота ---

@router.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated, bot, conn) -> None:
    """Реакция на изменение статуса бота в чате.

    Добавили → приветствие с инструкцией про ``/setup``.
    Удалили → чистим привязку, чтобы рассылка не билась в мёртвый чат.
    """
    chat = event.chat
    if chat is None:
        return
    if _chat_type(chat) not in SUPPORTED_CHAT_TYPES:
        return

    new_status = _value_of(event.new_chat_member.status)
    old_status = (_value_of(event.old_chat_member.status)
                  if event.old_chat_member else "")

    if new_status in GONE_STATUSES:
        # Бота удалили (или он сам вышел): привязка больше не нужна.
        removed = db.remove_group_chat(conn, chat.id)
        logger.info("bot removed from chat",
                    extra={"chat_id": chat.id, "chat_type": _chat_type(chat),
                           "unlinked": removed})
        return

    became_present = new_status in PRESENT_STATUSES
    was_gone = old_status in GONE_STATUSES or not old_status
    if became_present and was_gone:
        logger.info("bot added to chat",
                    extra={"chat_id": chat.id, "chat_type": _chat_type(chat)})
        try:
            await bot.send_message(chat.id, SETUP_HINT, parse_mode="HTML")
        except Exception as exc:
            # В канале без прав админа писать нельзя — это ожидаемо.
            logger.warning("could not greet chat",
                           extra={"chat_id": chat.id, "error": repr(exc)})


# --- /setup ---

@router.message(Command("setup"))
async def cmd_setup(message: Message, command: CommandObject, conn, bot) -> None:
    """Привязать чат к группе КСТ: ``/setup 25КАД``."""
    if not _is_supported_chat(message):
        await message.answer(NOT_CHAT_TEXT, parse_mode="HTML")
        return

    chat = message.chat
    user_id = message.from_user.id if message.from_user else 0

    # В канале команды доходят только если бот админ — предупреждаем сразу.
    if _chat_type(chat) == ChatType.CHANNEL.value:
        if not await bot_is_channel_admin(bot, chat.id):
            await message.answer(CHANNEL_NEEDS_ADMIN, parse_mode="HTML")
            return

    if not await is_chat_admin(bot, chat.id, user_id):
        await message.answer(
            "⛔ Привязывать чат может только администратор.",
            parse_mode="HTML",
        )
        return

    if not check_setup_rate_limit(chat.id):
        await message.answer(
            "⏳ Слишком часто. Попробуй через минуту.", parse_mode="HTML"
        )
        return

    available = db.list_available_groups(conn)
    group = resolve_group_argument(command.args, available)
    if group is None:
        await message.answer(SETUP_HINT, parse_mode="HTML")
        return

    if available and group not in available:
        suggestions = suggest_groups(group, available)
        text = f"🤔 Группы <code>{escape(group)}</code> нет в расписании."
        if suggestions:
            text += "\n\nПохожие: " + ", ".join(
                f"<code>{escape(name)}</code>" for name in suggestions
            )
            text += "\n\nНапиши /setup с точным номером."
        else:
            text += "\nПроверь номер и попробуй ещё раз."
        await message.answer(text, parse_mode="HTML")
        return

    db.add_group_chat(
        conn,
        chat_id=chat.id,
        chat_title=_title_of(message),
        chat_type=_chat_type(chat),
        group_name=group,
        added_by=user_id,
    )
    logger.info("chat linked to group",
                extra={"chat_id": chat.id, "group": group, "by": user_id})

    # Шаг 4: закрепление расписания требует права «Закрепление сообщений».
    # Привязка работает и без него, но пользователю говорим прямо, иначе он
    # будет ждать закреплённого расписания и не понимать, почему его нет.
    if await bot_is_chat_admin(bot, chat.id):
        await message.answer(
            f"✅ Чат привязан к группе <b>{escape(group)}</b>.\n"
            "🔔 Замены и расписание будут приходить сюда, "
            "закреплю до конца пар.\n\n"
            "<i>Отключить — /unsync. Расписание на сегодня — /schedule.</i>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "⚠️ <b>Сделайте меня администратором</b> группы с правом "
            "«Закрепление сообщений», иначе не смогу закреплять "
            "расписание. Замены будут приходить, но без закрепления.\n\n"
            f"✅ Чат привязан к группе <b>{escape(group)}</b>.",
            parse_mode="HTML",
        )
# --- /unsync ---

@router.message(Command("unsync"))
async def cmd_unsync(message: Message, conn, bot) -> None:
    """Отвязать чат от группы КСТ."""
    if not _is_supported_chat(message):
        await message.answer(NOT_CHAT_TEXT, parse_mode="HTML")
        return

    chat = message.chat
    user_id = message.from_user.id if message.from_user else 0

    if _chat_type(chat) == ChatType.CHANNEL.value:
        if not await bot_is_channel_admin(bot, chat.id):
            await message.answer(CHANNEL_NEEDS_ADMIN, parse_mode="HTML")
            return

    if not await is_chat_admin(bot, chat.id, user_id):
        await message.answer(
            "⛔ Отвязывать чат может только администратор.", parse_mode="HTML"
        )
        return

    link = db.get_group_chat(conn, chat.id)
    if link is None:
        await message.answer(
            "🤷 Чат не привязан. Напиши /setup <группа>, чтобы настроить.",
            parse_mode="HTML",
        )
        return

    db.remove_group_chat(conn, chat.id)
    logger.info("chat unlinked", extra={"chat_id": chat.id})
    await message.answer(
        f"✅ Чат отвязан от группы <b>{escape(str(link['group_name']))}</b>.\n"
        "Замены больше не приходят. Вернуть — /setup.",
        parse_mode="HTML",
    )


# --- /schedule ---

@router.message(Command("schedule"))
async def cmd_schedule(message: Message, conn) -> None:
    """Расписание на сегодня для группы, привязанной к чату."""
    if not _is_supported_chat(message):
        await message.answer(NOT_CHAT_TEXT, parse_mode="HTML")
        return

    link = db.get_group_chat(conn, message.chat.id)
    if link is None:
        await message.answer(
            "🤷 Чат не привязан к группе. Напиши /setup <группа>.",
            parse_mode="HTML",
        )
        return

    from datetime import date

    from bot.handlers.schedule import render_day
    from bot.services.schedule_service import (
        apply_substitutions,
        get_lessons_for_day,
    )

    group = str(link["group_name"])
    today = date.today()
    lessons = apply_substitutions(
        conn, get_lessons_for_day(conn, group, today), group, today
    )
    await message.answer(render_day(group, today, lessons), parse_mode="HTML")


# --- /chat_status (диагностика для участников чата) ---

@router.message(Command("chat_status"))
async def cmd_chat_status(message: Message, conn) -> None:
    """Показать текущую привязку чата (только чтение, прав не требует)."""
    if not _is_supported_chat(message):
        await message.answer(NOT_CHAT_TEXT, parse_mode="HTML")
        return

    link = db.get_group_chat(conn, message.chat.id)
    if link is None:
        await message.answer(
            "🤷 Чат не привязан. Напиши /setup <группа>.", parse_mode="HTML"
        )
        return

    enabled = "включены 🔔" if link.get("notifications_enabled") else "выключены 🔕"
    await message.answer(
        f"🔗 Группа: <b>{escape(str(link['group_name']))}</b>\n"
        f"🔔 Уведомления: <b>{enabled}</b>\n"
        f"🆔 Чат: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )