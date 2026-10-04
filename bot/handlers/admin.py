"""Админка: /stats, /broadcast, /reparse, /users, /deactivate (шаг 11).

Доступ — только для ``settings.admin_ids`` через фильтр :class:`IsAdmin`.
Для посторонних команды **не подтверждают своего существования**: ответ
нейтральный («Команда не найдена»), чтобы не подсказывать злоумышленнику,
какие команды есть у бота.

Рассылка не трогает ``is_active`` у тех, кто сообщение получил: этот флаг
означает «бот не заблокирован», а не «получал рассылку».
"""
from __future__ import annotations

import asyncio
import logging
import os
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import db
from bot.config import NOTIFY_CONCURRENCY, NOTIFY_SEND_DELAY_SECONDS
from bot.keyboards import reply as reply_kb
from bot.services import cache_service

logger = logging.getLogger(__name__)

router = Router(name="admin")

# Столько строк группы показываем в /users, дальше — «и ещё N».
USERS_PAGE_LIMIT = 50

# --- Заявки преподавателей (/teachers) ---

# Сколько ожидающих заявок показывать в тексте и сколько кнопок рисовать.
PENDING_PAGE_LIMIT = 20
APPROVED_PAGE_LIMIT = 10

# Кнопок «одобрить» — не больше, чем влезает в экран.
APPROVE_BUTTONS_LIMIT = 10

# Обрезка ФИО в подписи кнопки (у Telegram лимит на текст кнопки).
BUTTON_LABEL_LIMIT = 30

# Callback-данные модерации заявок.
CB_TEACHER_APPROVE_PREFIX = "teacher_appr:"
CB_TEACHER_YES_PREFIX = "teacher_confirm_yes:"
CB_TEACHER_NO_PREFIX = "teacher_confirm_no:"
CB_TEACHER_REFRESH = "teacher_refresh"


class IsAdmin(BaseFilter):
    """Пропускает только сообщения от админов из ``settings.admin_ids``."""

    async def __call__(self, message: Message, settings) -> bool:
        """Проверить, что автор — админ.

        Args:
            message: входящее сообщение.
            settings: настройки из ``workflow_data`` (aiogram подставляет сам).

        Returns:
            True, если ``from_user.id`` входит в ``settings.admin_ids``.
        """
        if message.from_user is None or settings is None:
            return False
        return message.from_user.id in set(settings.admin_ids)


class Broadcast(StatesGroup):
    """Состояния рассылки: текст → подтверждение."""

    waiting_text = State()
    waiting_confirm = State()


NOT_FOUND_TEXT = (
    "🤔 Команда не найдена. Посмотри /help — там всё, что умеет бот."
)

BROADCAST_PROMPT = (
    "📣 <b>Рассылка</b>\n\n"
    "Пришли текст сообщения. HTML разрешён (<code>b</code>, "
    "<code>i</code>, <code>code</code>), но без ссылок.\n\n"
    f"Отменить — кнопкой «{reply_kb.BTN_CANCEL}»."
)


def _format_size(num_bytes: int) -> str:
    """Размер файла в КБ (одна цифра после запятой)."""
    return f"{num_bytes / 1024:.1f} КБ"


def _format_moment(raw: str | None) -> str:
    """Момент из меты в короткий вид (``27.09 18:40``) или «нет данных»."""
    if not raw:
        return "нет данных"
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw).strftime("%d.%m %H:%M")
    except ValueError:
        return raw


def format_relative_time(iso_ts: str) -> str:
    """Момент времени в виде «5 минут назад», «2 часа назад», «вчера».

    Нужно в списке заявок: админу важно понять, сколько человек уже ждёт
    решения, а не абсолютную дату подачи.

    Args:
        iso_ts: момент в ISO (как в ``teachers.applied_at``).

    Returns:
        Человекочитаемую строку. Если момент не разбирается — исходное
        значение (лучше показать как есть, чем упасть).
    """
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return iso_ts or "—"

    # Наивное время сравниваем с наивным, осведомлённое — с осведомлённым.
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    seconds = int((now - moment).total_seconds())

    if seconds < 0:
        # Момент в будущем (часы сервера разъехались) — не показываем «-5 мин».
        return "только что"
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    if seconds < 86400 * 2:
        return "вчера"
    return f"{seconds // 86400} дн назад"


def build_teachers_text(conn) -> str:
    """Текст списка заявок преподавателей для ``/teachers``.

    Показывает ожидающие и одобренные отдельными секциями: админу нужны
    прежде всего те, что ждут решения.

    Args:
        conn: соединение SQLite.

    Returns:
        HTML-текст сообщения.
    """
    pending = db.list_pending_teachers(conn)
    approved = db.list_approved_teachers(conn)

    if not pending and not approved:
        return (
            "👨‍🏫 Заявок преподавателей нет.\n\n"
            "Преподаватель может подать заявку командой /teacher_apply."
        )

    lines = ["👨‍🏫 <b>Заявки преподавателей</b>", ""]

    if pending:
        lines.append(f"⏳ <b>Ожидают ({len(pending)}):</b>")
        for item in pending[:PENDING_PAGE_LIMIT]:
            lines.append(
                f"  • {escape(str(item['full_name']))}\n"
                f"    id <code>{item['tg_id']}</code> · "
                f"{format_relative_time(str(item['applied_at']))}"
            )
        if len(pending) > PENDING_PAGE_LIMIT:
            lines.append(f"  и ещё {len(pending) - PENDING_PAGE_LIMIT}")
        lines.append("")

    if approved:
        lines.append(f"✅ <b>Одобрено ({len(approved)}):</b>")
        for item in approved[:APPROVED_PAGE_LIMIT]:
            lines.append(f"  • {escape(str(item['full_name']))}")
        if len(approved) > APPROVED_PAGE_LIMIT:
            lines.append(f"  и ещё {len(approved) - APPROVED_PAGE_LIMIT}")

    return "\n".join(lines)


def teachers_kb(conn) -> InlineKeyboardMarkup | None:
    """Кнопки для обработки заявок: по одной на ожидающую.

    Args:
        conn: соединение SQLite.

    Returns:
        Клавиатура или None, если ожидающих заявок нет.
    """
    pending = db.list_pending_teachers(conn)
    if not pending:
        return None

    rows = []
    for item in pending[:APPROVE_BUTTONS_LIMIT]:
        name = str(item["full_name"])
        rows.append([InlineKeyboardButton(
            text=f"✅ {name[:BUTTON_LABEL_LIMIT]}",
            callback_data=f"{CB_TEACHER_APPROVE_PREFIX}{item['tg_id']}",
        )])
    rows.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data=CB_TEACHER_REFRESH,
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_stats_text(conn, db_path: str) -> str:
    """Собрать текст /stats.

    Args:
        conn: соединение SQLite.
        db_path: путь к файлу БД (для размера).

    Returns:
        HTML-текст отчёта.
    """
    total = db.count_users(conn)
    recent = db.count_active_users_since(conn, days=7)
    groups = db.count_users_by_group(conn, limit=10)

    try:
        size = _format_size(os.path.getsize(db_path))
    except OSError:
        size = "нет файла"

    lessons = conn.execute("SELECT COUNT(*) FROM schedule_cache").fetchone()[0]
    lesson_groups = conn.execute(
        "SELECT COUNT(DISTINCT group_name) FROM schedule_cache"
    ).fetchone()[0]
    subs = conn.execute("SELECT COUNT(*) FROM substitutions_cache").fetchone()[0]

    schedule_meta = _format_moment(
        cache_service.get_meta(conn, cache_service.META_LAST_SCHEDULE)
    )
    subs_meta = _format_moment(
        cache_service.get_meta(conn, cache_service.META_LAST_SUBSTITUTIONS)
    )

    lines = [
        "📊 <b>Статистика</b>",
        f"👥 Всего: <b>{total}</b> | активных за 7 дней: <b>{recent}</b>",
    ]
    if groups:
        lines.append("🎓 <b>По группам:</b>")
        for name, count in groups:
            lines.append(f"   {escape(name)} — {count}")
    else:
        lines.append("🎓 <b>По группам:</b> пока нет данных")

    history_rows = db.count_substitution_history(conn)
    history_dates = db.count_substitution_history_dates(conn)
    history_earliest = db.earliest_substitution_history_date(conn)
    history_latest = db.latest_substitution_history_date(conn)

    lines.extend([
        f"💾 Размер БД: <b>{size}</b>",
        f"🗓 Расписание: обновлено {schedule_meta}, занятий "
        f"<b>{lessons}</b>, групп <b>{lesson_groups}</b>",
        f"🔔 Замены: обновлено {subs_meta}, строк <b>{subs}</b>",
    ])
    if history_earliest is None:
        lines.append("📜 История замен: пусто")
    else:
        lines.append(
            f"📜 История замен: <b>{history_rows}</b> записей за "
            f"<b>{history_dates}</b> дн., с {escape(history_earliest)} "
            f"по {escape(str(history_latest))}"
        )
    return "\n".join(lines)


@router.message(Command("stats"), IsAdmin())
async def cmd_stats(message: Message, conn, settings) -> None:
    """Отчёт по боту (только админ)."""
    await message.answer(
        build_stats_text(conn, settings.db_path), parse_mode="HTML"
    )


@router.message(Command("stats"))
async def cmd_stats_denied(message: Message) -> None:
    """Посторонним отвечаем нейтрально, не подтверждая существование команды."""
    await message.answer(NOT_FOUND_TEXT)


@router.message(Command("teachers"), IsAdmin())
async def cmd_teachers(message: Message, conn) -> None:
    """Список заявок преподавателей с кнопками для обработки (только админ)."""
    await message.answer(
        build_teachers_text(conn),
        parse_mode="HTML",
        reply_markup=teachers_kb(conn),
    )


@router.message(Command("teachers"))
async def cmd_teachers_denied(message: Message) -> None:
    """Постороннему не подтверждаем существование команды.

    Telegram не умеет скрывать команды из меню для части пользователей,
    поэтому единственный способ не светить админку — нейтральный ответ.
    """
    await message.answer(NOT_FOUND_TEXT)


def teacher_card_kb(tg_id: int) -> InlineKeyboardMarkup:
    """Кнопки карточки заявки: одобрить, отклонить, назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Одобрить",
            callback_data=f"{CB_TEACHER_YES_PREFIX}{tg_id}",
        )],
        [InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"{CB_TEACHER_NO_PREFIX}{tg_id}",
        )],
        [InlineKeyboardButton(
            text="🔙 Назад",
            callback_data=CB_TEACHER_REFRESH,
        )],
    ])


@router.callback_query(F.data.startswith(CB_TEACHER_APPROVE_PREFIX),
                       IsAdmin())
async def cb_teacher_approve(callback: CallbackQuery, conn) -> None:
    """Показать карточку заявки с подтверждением.

    Отдельный шаг нужен, чтобы одобрение не срабатывало от одного случайного
    касания: доступ к чужим группам выдаётся осознанно.
    """
    if callback.data is None or callback.message is None:
        return

    raw = callback.data[len(CB_TEACHER_APPROVE_PREFIX):]
    try:
        tg_id = int(raw)
    except ValueError:
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    teacher = db.get_teacher(conn, tg_id)
    if teacher is None or teacher["status"] != db.TEACHER_PENDING:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    full_name = str(teacher["full_name"])
    await callback.message.answer(
        "👨‍🏫 <b>Заявка</b>\n\n"
        f"ФИО: <b>{escape(full_name)}</b>\n"
        f"От: id <code>{tg_id}</code>\n"
        f"Подана: {format_relative_time(str(teacher['applied_at']))}\n\n"
        "Проверь, что человек совпадает с ФИО в справочнике.",
        parse_mode="HTML",
        reply_markup=teacher_card_kb(tg_id),
    )
    await callback.answer()


async def _notify_teacher(bot, tg_id: int, text: str) -> bool:
    """Сообщить преподавателю о решении (ошибка доставки не критична).

    Человек мог заблокировать бота — тогда решение всё равно остаётся в БД,
    и преподаватель увидит статус через ``/teacher_status``.
    """
    try:
        await bot.send_message(tg_id, text, parse_mode="HTML")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not notify teacher",
            extra={"tg_id": tg_id, "error": repr(exc)},
        )
        return False


def _parse_teacher_id(data: str, prefix: str) -> int | None:
    """Вытащить tg_id из callback-данных (None, если мусор)."""
    try:
        return int(data[len(prefix):])
    except (ValueError, TypeError):
        return None


@router.callback_query(F.data.startswith(CB_TEACHER_YES_PREFIX), IsAdmin())
async def cb_teacher_confirm_yes(callback: CallbackQuery, conn) -> None:
    """Одобрить заявку и сообщить преподавателю."""
    if callback.data is None or callback.message is None:
        return
    if callback.from_user is None:
        return

    tg_id = _parse_teacher_id(callback.data, CB_TEACHER_YES_PREFIX)
    if tg_id is None:
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    # Проверяем статус ДО изменения: approve_teacher идемпотентна (обновит и
    # уже одобренную), поэтому без этой проверки повторное нажатие выглядело
    # бы успешным и второй раз слало уведомление преподавателю.
    current = db.get_teacher(conn, tg_id)
    if current is None or current["status"] != db.TEACHER_PENDING:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    if not db.approve_teacher(conn, tg_id, callback.from_user.id):
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    teacher = db.get_teacher(conn, tg_id)
    full_name = str(teacher["full_name"]) if teacher else "?"

    await _notify_teacher(
        callback.bot, tg_id,
        "✅ <b>Твоя заявка одобрена!</b>\n\n"
        f"ФИО: {escape(full_name)}\n\n"
        "Команды:\n"
        "/my_lessons — моё расписание\n"
        "/my_groups — мои группы\n"
        "/attendance <группа> — посещаемость",
    )

    try:
        await callback.message.edit_text(
            f"✅ Заявка <b>{escape(full_name)}</b> одобрена.",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        # Сообщение могли удалить — решению это не мешает.
        logger.debug("could not edit teacher card", exc_info=True)
    await callback.answer("Одобрено")


@router.callback_query(F.data.startswith(CB_TEACHER_NO_PREFIX), IsAdmin())
async def cb_teacher_confirm_no(callback: CallbackQuery, conn) -> None:
    """Отклонить заявку и сообщить преподавателю.

    Заявка не удаляется: остаётся в БД со статусом ``rejected`` — история
    решений нужна, если человек придёт разбираться.
    """
    if callback.data is None or callback.message is None:
        return
    if callback.from_user is None:
        return

    tg_id = _parse_teacher_id(callback.data, CB_TEACHER_NO_PREFIX)
    if tg_id is None:
        await callback.answer("Некорректная заявка", show_alert=True)
        return

    # Статус проверяем до изменения — по той же причине, что и при одобрении.
    current = db.get_teacher(conn, tg_id)
    if current is None or current["status"] != db.TEACHER_PENDING:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    if not db.reject_teacher(conn, tg_id, callback.from_user.id):
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    teacher = db.get_teacher(conn, tg_id)
    full_name = str(teacher["full_name"]) if teacher else "?"

    await _notify_teacher(
        callback.bot, tg_id,
        "❌ <b>Заявка отклонена</b>\n\n"
        f"ФИО: {escape(full_name)}\n\n"
        "Если это ошибка — напиши: @W1nqu4",
    )

    try:
        await callback.message.edit_text(
            f"❌ Заявка <b>{escape(full_name)}</b> отклонена.",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        logger.debug("could not edit teacher card", exc_info=True)
    await callback.answer("Отклонено")


@router.callback_query(F.data == CB_TEACHER_REFRESH, IsAdmin())
async def cb_teacher_refresh(callback: CallbackQuery, conn) -> None:
    """Обновить список заявок (кнопка из уведомления и карточки)."""
    if callback.message is None:
        return

    await callback.message.answer(
        build_teachers_text(conn),
        parse_mode="HTML",
        reply_markup=teachers_kb(conn),
    )
    await callback.answer("Обновлено")


@router.message(Command("reparse"), IsAdmin())
async def cmd_reparse(message: Message, conn) -> None:
    """Принудительно обновить расписание и лист замен (только админ).

    Обе функции ``cache_service`` асинхронные (внутри — скачивание по HTTP),
    поэтому вызываются напрямую: ``asyncio.to_thread`` здесь не подходит — он
    предназначен для синхронных функций и вернул бы корутину, а не результат.

    Отчёт содержит числа и время: админу важно видеть, что данные обновились
    и сколько это заняло. Отрицательное число от ``cache_service`` означает
    «скачать не удалось, кэш не тронут» — показываем это словами, а не «-1».

    История замен сохраняется внутри ``refresh_substitutions`` — отдельный
    вызов не нужен (см. :func:`bot.services.cache_service.refresh_substitutions`).
    """
    import time

    await message.answer("🔄 <b>Принудительный перепарсинг</b>\n"
                         "<i>Скачиваю расписание и лист замен…</i>",
                         parse_mode="HTML")
    started = time.monotonic()

    try:
        lessons = await cache_service.refresh_schedule(conn)
        subs = await cache_service.refresh_substitutions(conn)
    except Exception as exc:
        # Падение не должно оставлять админа без ответа: он не знает, упало
        # или просто долго.
        logger.exception("reparse failed")
        await message.answer(
            f"❌ <b>Ошибка перепарсинга</b>\n\n<code>{escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    elapsed = time.monotonic() - started
    groups = conn.execute(
        "SELECT COUNT(DISTINCT group_name) FROM schedule_cache"
    ).fetchone()[0]

    def _line(title: str, count: int, unit: str) -> str:
        if count < 0:
            return f"{title}: <b>ошибка</b> (кэш не изменён)"
        return f"{title}: <b>{count}</b> {unit}"

    await message.answer(
        "🔄 <b>Принудительный перепарсинг</b>\n\n"
        + _line("📆 Расписание", lessons, "занятий")
        + f", <b>{int(groups)}</b> групп\n"
        + _line("🔔 Замены", subs, "строк")
        + f"\n⏱ Заняло: {elapsed:.1f} сек",
        parse_mode="HTML",
    )


@router.message(Command("reparse"))
async def cmd_reparse_denied(message: Message) -> None:
    """Посторонним — нейтральный ответ."""
    await message.answer(NOT_FOUND_TEXT)
@router.message(Command("users"), IsAdmin())
async def cmd_users(message: Message, conn, command: CommandObject) -> None:
    """Список активных пользователей группы (только админ).

    Кнопка-команда: ``/users 26КАД``. Группа нормализуется, чтобы
    «26 кад» и «26КАД» давали один результат.
    """
    from bot.parsers.groups import normalize_group_name

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Использование: <code>/users 26КАД</code>", parse_mode="HTML",
        )
        return

    group = normalize_group_name(raw)
    total = db.count_users_in_group(conn, group)
    if total == 0:
        await message.answer(
            f"👥 В группе <b>{escape(group)}</b> нет активных пользователей.",
            parse_mode="HTML",
        )
        return

    users = db.list_users_in_group(conn, group, limit=USERS_PAGE_LIMIT)
    lines = [
        f"👥 <b>{escape(group)}</b> — {total} активных:",
    ]
    for index, user in enumerate(users, 1):
        name = escape(user["full_name"] or "без имени")
        lines.append(f"{index}. <code>{user['tg_id']}</code> — {name}")

    hidden = total - len(users)
    if hidden > 0:
        lines.append(f"… и ещё <b>{hidden}</b>")
    lines.append("")
    lines.append("Отключить: <code>/deactivate &lt;tg_id&gt;</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users_denied(message: Message) -> None:
    """Посторонним — нейтральный ответ."""
    await message.answer(NOT_FOUND_TEXT)


@router.message(Command("deactivate"), IsAdmin())
async def cmd_deactivate(message: Message, conn, command: CommandObject) -> None:
    """Вручную пометить пользователя неактивным (только админ)."""
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/deactivate 123456789</code>",
            parse_mode="HTML",
        )
        return

    tg_id = int(raw)
    user = db.get_user(conn, tg_id)
    if user is None:
        await message.answer(f"Пользователь <code>{tg_id}</code> не найден.",
                             parse_mode="HTML")
        return

    db.deactivate_user(conn, tg_id)
    logger.info("user deactivated by admin",
                extra={"tg_id": tg_id, "admin_id": message.from_user.id})
    await message.answer(
        f"🚫 Пользователь <code>{tg_id}</code> отключён "
        f"(больше не получает уведомления).",
        parse_mode="HTML",
    )


@router.message(Command("deactivate"))
async def cmd_deactivate_denied(message: Message) -> None:
    """Посторонним — нейтральный ответ."""
    await message.answer(NOT_FOUND_TEXT)
def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Кнопки подтверждения рассылки."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="bc:send"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="bc:cancel"),
    ]])


@router.message(Command("broadcast"), IsAdmin())
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """Начать рассылку: ждём текст."""
    await state.set_state(Broadcast.waiting_text)
    await message.answer(BROADCAST_PROMPT, parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast_denied(message: Message) -> None:
    """Посторонним — нейтральный ответ."""
    await message.answer(NOT_FOUND_TEXT)


@router.message(Broadcast.waiting_text, F.text == reply_kb.BTN_CANCEL)
async def broadcast_cancel_text(message: Message, state: FSMContext) -> None:
    """Отмена рассылки текстовой кнопкой."""
    await state.clear()
    await message.answer("↩️ Рассылка отменена.", reply_markup=reply_kb.main_kb())


@router.message(Broadcast.waiting_text)
async def broadcast_take_text(message: Message, state: FSMContext, conn) -> None:
    """Принять текст рассылки и показать предпросмотр."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли текст сообщения (или отмени).")
        return

    recipients = len(db.iter_active_users(conn))
    await state.update_data(broadcast_text=text)
    await state.set_state(Broadcast.waiting_confirm)

    await message.answer(
        "📣 <b>Предпросмотр рассылки</b>\n"
        f"👥 Получателей: <b>{recipients}</b>\n\n"
        "———\n"
        f"{text}\n"
        "———\n\n"
        "Отправляем?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(F.data == "bc:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена рассылки кнопкой."""
    await state.clear()
    if callback.message is not None:
        await callback.message.answer("↩️ Рассылка отменена.")
        await callback.message.answer("Главное меню:",
                                      reply_markup=reply_kb.main_kb())
    await callback.answer()
async def run_broadcast(conn, bot, text: str,
                        throttle: bool = True) -> dict:
    """Разослать текст всем активным пользователям.

    Обработка ошибок:

    - ``TelegramForbiddenError`` — пользователь заблокировал бота, помечаем
      ``is_active = 0`` и больше ему не пишем;
    - ``TelegramRetryAfter`` — флуд-контроль: пауза и одна повторная попытка;
    - прочие ошибки — считаем как ошибку и продолжаем (одна неудачная
      отправка не должна срывать рассылку остальным).

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        text: текст сообщения (HTML разрешён).
        throttle: выдерживать ли антифлуд-паузу (в тестах — False).

    Returns:
        ``{'sent': N, 'failed': M, 'blocked': K, 'total': T}``.
    """
    users = db.iter_active_users(conn)
    semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
    sent = 0
    failed = 0
    blocked = 0

    for user in users:
        async with semaphore:
            try:
                await bot.send_message(user["tg_id"], text, parse_mode="HTML")
                sent += 1
            except TelegramForbiddenError:
                db.deactivate_user(conn, user["tg_id"])
                blocked += 1
                logger.info("broadcast: user blocked bot",
                            extra={"tg_id": user["tg_id"]})
            except TelegramRetryAfter as exc:
                pause = int(getattr(exc, "retry_after", 1)) + 1
                await asyncio.sleep(pause)
                try:
                    await bot.send_message(user["tg_id"], text, parse_mode="HTML")
                    sent += 1
                except Exception:
                    failed += 1
            except Exception as exc:
                failed += 1
                logger.warning("broadcast: send failed",
                               extra={"tg_id": user["tg_id"], "error": repr(exc)})

            if throttle and NOTIFY_SEND_DELAY_SECONDS:
                await asyncio.sleep(NOTIFY_SEND_DELAY_SECONDS)

    result = {"sent": sent, "failed": failed, "blocked": blocked,
              "total": len(users)}
    logger.info("broadcast finished", extra=result)
    return result


@router.callback_query(F.data == "bc:send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext, conn,
                         bot) -> None:
    """Подтверждено: разослать текст и прислать отчёт."""
    data = await state.get_data()
    text = (data.get("broadcast_text") or "").strip()
    await state.clear()

    if not text:
        await callback.answer("Текст потерялся, начни заново", show_alert=True)
        return

    if callback.message is not None:
        await callback.message.answer("📣 Отправляю…")

    report = await run_broadcast(conn, bot, text)

    logger.info("broadcast by admin",
                extra={"admin_id": callback.from_user.id, **report})
    if callback.message is not None:
        await callback.message.answer(
            "✅ <b>Рассылка завершена</b>\n"
            f"👥 Всего получателей: <b>{report['total']}</b>\n"
            f"📬 Отправлено: <b>{report['sent']}</b>\n"
            f"⚠️ Ошибок: <b>{report['failed']}</b>\n"
            f"🚫 Заблокировали бота: <b>{report['blocked']}</b>",
            parse_mode="HTML",
        )
    await callback.answer("Готово")
