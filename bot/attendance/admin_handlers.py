"""Админ-хендлеры посещаемости: /admin, /make_starosta (этап 1).

Доступ — только для tg_id из ``settings.admin_ids`` (env ``ADMIN_IDS``).
Никаких хардкодов: тот же ID, что в конфиге, — и ничего больше.

Для посторонних команды **не подтверждают своего существования**: ответ
нейтральный («Команда не найдена»), как в админке проекта
(:mod:`bot.handlers.admin`).

Команд в ``BOT_COMMANDS`` нет намеренно: они не должны светиться в меню
Telegram у обычных студентов.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.attendance import admin_service
from bot.attendance import db as att_db
from bot.attendance import keyboards as kb
from bot.attendance import service
from bot.attendance import texts

logger = logging.getLogger(__name__)

router = Router(name="attendance_admin")

# Сколько позиций показывать в списках одним сообщением.
GROUPS_PAGE_LIMIT = 20
STUDENTS_PAGE_LIMIT = 50


class IsAdmin(BaseFilter):
    """Пропускает только админов из ``settings.admin_ids``."""

    async def __call__(self, event, settings) -> bool:
        """Проверить права по tg_id автора события.

        Args:
            event: сообщение или callback (у обоих есть ``from_user``).
            settings: настройки из ``workflow_data`` (aiogram подставляет сам).

        Returns:
            True, если автор — админ.
        """
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        return admin_service.is_admin(user.id, settings)


class AdminFlow(StatesGroup):
    """Состояния админ-действий с вводом текста."""

    waiting_find_group = State()
    waiting_delete_group = State()
    waiting_broadcast_text = State()


def _tg_id(message: Message) -> int:
    """tg_id автора сообщения."""
    return message.from_user.id if message.from_user else 0


async def _deny(message: Message) -> None:
    """Нейтральный ответ посторонним: команды не существует."""
    await message.answer(texts.ADMIN_NOT_FOUND, parse_mode="HTML")


async def _send_panel(message: Message, conn) -> None:
    """Отправить админ-панель со счётчиками."""
    stats = admin_service.get_bot_stats(conn)
    await message.answer(
        texts.admin_panel(stats), parse_mode="HTML",
        reply_markup=kb.admin_panel_kb(),
    )


@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, conn, settings) -> None:
    """👑 Админ-панель со сводкой и кнопками."""
    await _send_panel(message, conn)


@router.message(Command("admin"))
async def cmd_admin_denied(message: Message) -> None:
    """Посторонним — «Команда не найдена», без подсказок."""
    await _deny(message)


@router.callback_query(F.data == kb.CB_ADM_BACK)
async def back_to_panel(callback: CallbackQuery, state: FSMContext,
                        conn) -> None:
    """«🔙 Назад» — вернуться в админ-панель."""
    await state.clear()
    if callback.message is not None:
        await _send_panel(callback.message, conn)
    await callback.answer()


@router.callback_query(F.data == kb.CB_ADM_GROUPS)
async def list_groups(callback: CallbackQuery, conn) -> None:
    """📋 Все группы: карточки с числом студентов, старостой и кодом."""
    groups = admin_service.list_all_groups_stats(conn)
    body, _hidden = texts.admin_groups_list(groups, limit=GROUPS_PAGE_LIMIT)

    if callback.message is not None:
        await callback.message.answer(
            body, parse_mode="HTML", reply_markup=kb.admin_back_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == kb.CB_ADM_STUDENTS)
async def list_students(callback: CallbackQuery, conn) -> None:
    """👥 Все студенты, сгруппированные по группам."""
    students = admin_service.list_all_students(conn)
    body, _hidden = texts.admin_students_list(students,
                                              limit=STUDENTS_PAGE_LIMIT)

    if callback.message is not None:
        await callback.message.answer(
            body, parse_mode="HTML", reply_markup=kb.admin_back_kb(),
        )
    await callback.answer()
# --- поиск группы ---

@router.callback_query(F.data == kb.CB_ADM_FIND)
async def find_group_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """🔍 Найти группу: просим название (FSM)."""
    await state.set_state(AdminFlow.waiting_find_group)
    if callback.message is not None:
        await callback.message.answer(texts.ASK_ADMIN_GROUP, parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlow.waiting_find_group, IsAdmin())
async def process_find_group(message: Message, state: FSMContext, conn) -> None:
    """Показать карточку найденной группы."""
    query = (message.text or "").strip()
    group_name = service.find_group_by_name(conn, query)

    # Ищем среди СОЗДАННЫХ групп: админа интересует состояние, а не расписание.
    if group_name is None or att_db.get_group(conn, group_name) is None:
        await message.answer(
            texts.ADMIN_FIND_NOT_FOUND.format(group=escape(query)),
            parse_mode="HTML",
        )
        return

    stats = {item["group_name"]: item
             for item in admin_service.list_all_groups_stats(conn)}
    item = stats.get(group_name) or {}
    await state.clear()
    await message.answer(
        texts.admin_group_card({
            **item,
            "group_name": escape(group_name),
            "starosta_name": escape(item.get("starosta_name") or ""),
        }),
        parse_mode="HTML",
        reply_markup=kb.admin_back_kb(),
    )


# --- удаление группы ---

@router.callback_query(F.data == kb.CB_ADM_DELETE)
async def delete_group_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """🗑 Удалить группу: просим название (FSM)."""
    await state.set_state(AdminFlow.waiting_delete_group)
    if callback.message is not None:
        await callback.message.answer(texts.ASK_ADMIN_DELETE_GROUP,
                                      parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlow.waiting_delete_group, IsAdmin())
async def process_delete_group(message: Message, state: FSMContext,
                               conn) -> None:
    """Предпросмотр удаления: спрашиваем подтверждение кнопкой."""
    query = (message.text or "").strip()
    group_name = service.find_group_by_name(conn, query)

    if group_name is None or att_db.get_group(conn, group_name) is None:
        await message.answer(
            texts.ADMIN_DELETE_NOT_FOUND.format(group=escape(query)),
            parse_mode="HTML",
        )
        return

    students = att_db.count_group_students(conn, group_name)
    await state.update_data(delete_group=group_name)
    await message.answer(
        texts.admin_delete_confirm(escape(group_name), students),
        parse_mode="HTML",
        reply_markup=kb.delete_confirm_kb(group_name),
    )


@router.callback_query(F.data.startswith(kb.CB_ADM_DELETE_OK_PREFIX),
                       IsAdmin())
async def delete_group_confirmed(callback: CallbackQuery, state: FSMContext,
                                 conn) -> None:
    """Подтверждённое удаление группы вместе со студентами."""
    group_name = (callback.data or "").removeprefix(
        kb.CB_ADM_DELETE_OK_PREFIX
    )
    result = admin_service.delete_group(conn, group_name)
    await state.clear()

    if callback.message is not None:
        if result["ok"]:
            await callback.message.answer(
                texts.admin_delete_done(escape(group_name),
                                        result["students_deleted"]),
                parse_mode="HTML",
                reply_markup=kb.admin_back_kb(),
            )
        else:
            await callback.message.answer(
                texts.ADMIN_DELETE_NOT_FOUND.format(group=escape(group_name)),
                parse_mode="HTML",
                reply_markup=kb.admin_back_kb(),
            )
    await callback.answer()
# --- рассылка ---

@router.callback_query(F.data == kb.CB_ADM_BROADCAST)
async def broadcast_menu(callback: CallbackQuery, state: FSMContext,
                         conn) -> None:
    """📢 Рассылка: просим текст (FSM)."""
    students = admin_service.list_all_students(conn)
    if callback.message is not None:
        if not students:
            await callback.message.answer(texts.ADMIN_BROADCAST_EMPTY,
                                          parse_mode="HTML",
                                          reply_markup=kb.admin_back_kb())
        else:
            await state.set_state(AdminFlow.waiting_broadcast_text)
            await callback.message.answer(texts.ASK_ADMIN_BROADCAST,
                                          parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlow.waiting_broadcast_text, IsAdmin())
async def process_broadcast_text(message: Message, state: FSMContext,
                                 conn) -> None:
    """Предпросмотр рассылки со счётчиком получателей."""
    from bot.keyboards.reply import BTN_CANCEL

    text = (message.text or "").strip()
    if text == BTN_CANCEL:
        await state.clear()
        await message.answer(texts.ADMIN_BROADCAST_CANCELLED,
                             parse_mode="HTML",
                             reply_markup=kb.admin_back_kb())
        return
    if not text:
        await message.answer(texts.ASK_ADMIN_BROADCAST, parse_mode="HTML")
        return

    recipients = len(admin_service.list_all_students(conn))
    await state.update_data(broadcast_text=text)
    await message.answer(
        texts.admin_broadcast_preview(escape(text), recipients),
        parse_mode="HTML",
        reply_markup=kb.broadcast_confirm_kb(),
    )


@router.callback_query(F.data == kb.CB_ADM_BC_SEND, IsAdmin())
async def broadcast_send(callback: CallbackQuery, state: FSMContext, conn,
                         bot, settings) -> None:
    """Отправить рассылку и показать отчёт."""
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "")
    await state.clear()

    if not text:
        await callback.answer(texts.ADMIN_BROADCAST_EMPTY, show_alert=True)
        return

    result = await admin_service.broadcast_to_all(conn, bot, text, settings)

    if callback.message is not None:
        await callback.message.answer(
            texts.admin_broadcast_report(result), parse_mode="HTML",
            reply_markup=kb.admin_back_kb(),
        )
    await callback.answer("Готово")
# --- назначение старосты ---

def resolve_student(conn, raw: str) -> dict | None:
    """Найти студента по tg_id (числом).

    Username Telegram в БД не хранится (есть только ``tg_id`` и ФИО), поэтому
    ``@username`` распознать нельзя: возвращаем None, а вызывающий код
    подсказывает формат с tg_id.

    Args:
        conn: соединение SQLite.
        raw: аргумент команды (tg_id или @username).

    Returns:
        Словарь студента или None.
    """
    value = (raw or "").strip()
    if not value.isdigit():
        return None
    return att_db.get_student(conn, int(value))


def assign_starosta(conn, group_name: str, tg_id: int) -> dict:
    """Назначить старосту: обновить роль и ``study_groups``.

    Прежний староста становится обычным студентом — староста в группе один.

    Args:
        conn: соединение SQLite.
        group_name: группа.
        tg_id: tg_id нового старосты (должен быть студентом этой группы).

    Returns:
        ``{'ok': bool, 'previous_tg_id': int | None}``.
    """
    from bot.attendance.models import ROLE_STAROSTA, ROLE_STUDENT

    if att_db.get_group(conn, group_name) is None:
        return {"ok": False, "previous_tg_id": None}

    student = att_db.get_student(conn, tg_id)
    if student is None or str(student["group_name"]) != group_name:
        return {"ok": False, "previous_tg_id": None}

    group = att_db.get_group(conn, group_name) or {}
    previous = group.get("starosta_tg_id")
    if previous and int(previous) != tg_id:
        att_db.set_student_role(conn, int(previous), ROLE_STUDENT)

    att_db.set_student_role(conn, tg_id, ROLE_STAROSTA)
    att_db.set_group_starosta(conn, group_name, tg_id)
    logger.info("starosta assigned by admin",
                extra={"group": group_name, "tg_id": tg_id})
    return {"ok": True, "previous_tg_id": previous}


@router.message(Command("make_starosta"), IsAdmin())
async def cmd_make_starosta(message: Message, command: CommandObject,
                            conn, settings) -> None:
    """Назначить старосту: ``/make_starosta 25КАД 908084777``."""
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Формат: <code>/make_starosta 25КАД 908084777</code>\n"
            "<i>Нужен tg_id студента (узнать: @userinfobot).</i>",
            parse_mode="HTML",
        )
        return

    group_query, target_raw = args[0], args[1]
    group_name = service.find_group_by_name(conn, group_query)
    if group_name is None or att_db.get_group(conn, group_name) is None:
        await message.answer(
            texts.ADMIN_FIND_NOT_FOUND.format(group=escape(group_query)),
            parse_mode="HTML",
        )
        return

    target = resolve_student(conn, target_raw)
    if target is None:
        await message.answer(
            f"🤔 Не нашёл студента по <code>{escape(target_raw)}</code>.\n"
            "Нужен tg_id числом: узнать можно через @userinfobot.",
            parse_mode="HTML",
        )
        return

    if str(target["group_name"]) != group_name:
        await message.answer(
            f"⛔ <b>{escape(str(target['full_name']))}</b> учится в группе "
            f"<b>{escape(str(target['group_name']))}</b>, а не в "
            f"<b>{escape(group_name)}</b>.",
            parse_mode="HTML",
        )
        return

    result = assign_starosta(conn, group_name, int(target["tg_id"]))
    if not result["ok"]:
        await message.answer("🤔 Не удалось назначить старосту.",
                             parse_mode="HTML")
        return

    await message.answer(
        texts.starosta_assigned(escape(group_name),
                                escape(str(target["full_name"]))),
        parse_mode="HTML",
    )


@router.message(Command("make_starosta"))
async def cmd_make_starosta_denied(message: Message) -> None:
    """Посторонним — «Команда не найдена»."""
    await _deny(message)