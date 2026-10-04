"""Связка аккаунтов Telegram ↔ VK: команды ``/link`` и ``/unlink``.

Студент может пользоваться ботом и в Telegram, и во ВКонтакте. Чтобы группа и
расписание совпадали, аккаунты связываются одноразовым кодом: Telegram-бот
выдаёт код, VK-бот его принимает. Связка по @username не годится — в TG и VK
это разные сущности, совпадение имён ничего не значит.

Хранение: ``account_links`` (миграция 16) — пара ``tg_id ↔ vk_id``.
Синхронизацию группы при смене на любой платформе делает
:func:`sync_group_name`.

Команда живёт отдельным роутером: ``/link`` и ``/unlink`` не должны
перехватываться общим вводом группы (``bot.handlers.group_input`` идёт
последним, но порядок роутеров здесь тоже важен — см. :mod:`bot.handlers`).
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot import db

logger = logging.getLogger(__name__)

router = Router(name="account_link")

# Имя Telegram-бота для ссылки-приглашения. Совпадает с текстом в
# bot.services.notify_service (там же упоминается @kst24_bot).
BOT_USERNAME = "kst24_bot"

# Префикс deep-link: t.me/kst24_bot?start=link_ABC123
DEEPLINK_PREFIX = "link_"


def link_instructions(code: str) -> str:
    """Текст с кодом для привязки VK-аккаунта."""
    return (
        "🔗 <b>Связка с VK</b>\n\n"
        "Открой VK-бота и отправь команду:\n"
        f"<code>/link {escape(code)}</code>\n\n"
        f"Код действует {db.LINK_CODE_TTL_MINUTES} минут.\n"
        f"Или открой ссылку: t.me/{BOT_USERNAME}?start=link_{escape(code)}"
    )


def sync_group_name(conn, tg_id: int, group_name: str) -> None:
    """Записать группу в Telegram и, если VK связан, в VK.

    Вызывается при смене группы в TG. Без этого смена группы в Telegram
    оставила бы VK со старой группой, и студент видел бы разное расписание
    на платформах.

    Args:
        conn: соединение SQLite.
        tg_id: Telegram id.
        group_name: новое имя группы (уже нормализованное).
    """
    vk_id = db.get_vk_id_by_tg(conn, tg_id)
    if vk_id is None:
        return
    db.update_vk_user_group(conn, vk_id, group_name)


@router.message(Command("link"))
async def cmd_link(message: Message, conn) -> None:
    """Выдать код для привязки VK-аккаунта (или напомнить о существующей связке)."""
    if message.from_user is None:
        return
    tg_id = message.from_user.id

    existing_vk = db.get_vk_id_by_tg(conn, tg_id)
    if existing_vk:
        await message.answer(
            f"✅ Твой аккаунт уже связан с VK (id {existing_vk}).\n"
            "Отвязать: /unlink",
        )
        return

    code = db.create_link_code(conn, tg_id)
    await message.answer(link_instructions(code), parse_mode="HTML")


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, conn) -> None:
    """Удалить связку с VK-аккаунтом."""
    if message.from_user is None:
        return

    if db.unlink_account(conn, message.from_user.id):
        await message.answer(
            "✅ Связка с VK удалена.\n"
            "<i>Группа в VK осталась прежней — она теперь отдельная.</i>",
            parse_mode="HTML",
        )
    else:
        await message.answer("У тебя нет связанного VK.")


@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject,
                             conn) -> None:
    """Обработка ссылки ``t.me/<bot>?start=link_XXXXXX``.

    Deep-link намеренно только принимает код и повторяет инструкцию: связку
    создаёт VK-бот, когда студент присылает код там. Иначе связка появилась бы
    до того, как студент открыл VK, и «одноразовость» кода потеряла смысл.
    """
    payload = (command.args or "").strip()
    if not payload.lower().startswith(DEEPLINK_PREFIX):
        # Чужой deep-link — не наш случай, отвечаем как на обычный /start.
        await message.answer(
            "Не понял ссылку. Напиши /link, чтобы связать аккаунты.",
        )
        return

    code = payload[len(DEEPLINK_PREFIX):].strip().upper()
    if not code:
        await message.answer("В ссылке нет кода. Напиши /link.")
        return

    await message.answer(
        f"Код <code>{escape(code)}</code> принят.\n\n"
        "Теперь открой VK-бота и отправь:\n"
        f"<code>/link {escape(code)}</code>",
        parse_mode="HTML",
    )