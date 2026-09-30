"""Обработчики посещаемости: /start, «📊 Моя группа», код, создание (этап 1).

Поток студента: /start → «📊 Моя группа» → «🔢 Ввести код» → код → ФИО → в группе.
Поток старосты: /start → «📊 Моя группа» → «⭐ Создать группу» → название → ФИО → код.

Кнопки отметок и отчётов пока заглушки: «🚧 Скоро — этап 2».

Почему /start живёт здесь: старая FSM-регистрация по группе удалена (группа
вводится через «Моя группа»), а два обработчика ``CommandStart`` в разных
роутерах конфликтовали бы — aiogram взял бы первый подключённый.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import db
from bot.attendance import db as att_db
from bot.attendance import attendance_handlers as att_marks
from bot.attendance import attendance_keyboards as att_kb
from bot.attendance import keyboards as kb
from bot.attendance import service
from bot.attendance import texts

logger = logging.getLogger(__name__)

router = Router(name="attendance")


class GroupFlow(StatesGroup):
    """Состояния регистрации в группе."""

    waiting_code = State()          # ввод кода приглашения
    waiting_name = State()          # ввод ФИО после найденной группы
    waiting_group_name = State()    # ввод названия группы (староста)
    waiting_starosta_name = State()  # ввод ФИО старосты
    waiting_deputy_choice = State()  # выбор зама (не используется, но резерв)


def _tg_id(message: Message) -> int:
    """tg_id автора сообщения (0, если его нет)."""
    return message.from_user.id if message.from_user else 0


def _callback_tg_id(callback: CallbackQuery) -> int:
    """tg_id автора callback-а."""
    return callback.from_user.id


def _name_of(message: Message) -> str:
    """Имя пользователя Telegram (для приветствия)."""
    return message.from_user.full_name if message.from_user else ""


async def _greet(message: Message, registered: bool, has_schedule_group:
                 bool = False) -> None:
    """Показать приветствие с главным меню.

    «С возвращением» показываем, если пользователь уже настроил хоть что-то:
    группу для посещаемости (``students``) или группу для расписания
    (``users.group_name``). Обе системы сосуществуют, поэтому достаточно
    любой — иначе вернувшийся студент получал бы текст как в первый раз.
    """
    if registered or has_schedule_group:
        name = escape(_name_of(message)) or "студент"
        await message.answer(
            f"👋 С возвращением, <b>{name}</b>!",
            parse_mode="HTML",
            reply_markup=kb.main_kb(),
        )
        return
    await message.answer(
        texts.GREETING, parse_mode="HTML", reply_markup=kb.main_kb(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, conn) -> None:
    """Приветствие + главное меню.

    Уже в группе (любой из двух систем) — короткое приветствие; иначе полный
    текст с подсказкой про «📊 Моя группа».
    """
    await state.clear()
    tg_id = _tg_id(message)
    student = service.get_student(conn, tg_id)
    schedule_group = db.get_user_group(conn, tg_id)
    await _greet(message, registered=student is not None,
                 has_schedule_group=bool(schedule_group))


@router.message(F.text == kb.BTN_MY_GROUP)
async def my_group(message: Message, state: FSMContext, conn) -> None:
    """Раздел «Моя группа»: карточка группы или меню регистрации."""
    from bot.state import SCREEN_MY_GROUP, set_last_screen

    await state.clear()
    tg_id = _tg_id(message)
    await set_last_screen(state, SCREEN_MY_GROUP)

    student = service.get_student(conn, tg_id)
    if student is None:
        await message.answer(
            texts.NOT_REGISTERED, parse_mode="HTML",
            reply_markup=kb.my_group_not_registered_kb(),
        )
        return

    group_name = str(student["group_name"])
    role = service.role_of(student)
    count = len(service.get_group_students(conn, group_name))
    await message.answer(
        texts.group_card(group_name, count, service.role_label(role)),
        parse_mode="HTML",
        reply_markup=(
            kb.my_group_starosta_kb()
            if role in ("starosta", "deputy")
            else kb.my_group_student_kb()
        ),
    )


@router.message(Command("mygroup"))
async def cmd_my_group(message: Message, state: FSMContext, conn) -> None:
    """Команда /mygroup — то же, что кнопка «📊 Моя группа»."""
    await my_group(message, state, conn)
# --- регистрация по коду ---

@router.callback_query(F.data == kb.CB_ENTER_CODE)
async def enter_code(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔢 Ввести код» — просим код приглашения."""
    await state.set_state(GroupFlow.waiting_code)
    if callback.message is not None:
        await callback.message.answer(texts.ASK_CODE, parse_mode="HTML")
    await callback.answer()


@router.message(GroupFlow.waiting_code)
async def process_code(message: Message, state: FSMContext, conn) -> None:
    """Проверить код, найти группу и перейти к вводу ФИО."""
    raw = (message.text or "").strip()

    if raw.startswith("/"):
        await message.answer(texts.ASK_CODE, parse_mode="HTML")
        return

    if not raw.isdigit() or len(raw) != service.INVITE_CODE_LENGTH:
        await message.answer(texts.ASK_CODE_AGAIN, parse_mode="HTML")
        return

    group = att_db.get_group_by_code(conn, raw)
    if group is None:
        logger.info("attendance: code not found")
        await message.answer(texts.CODE_NOT_FOUND, parse_mode="HTML")
        return

    group_name = str(group["group_name"])

    # Уже в этой же группе — не гоняем по кругу.
    existing = service.get_student(conn, _tg_id(message))
    if existing is not None and str(existing["group_name"]) == group_name:
        await state.clear()
        await message.answer(
            texts.ALREADY_IN_GROUP.format(group=escape(group_name)),
            parse_mode="HTML",
            reply_markup=kb.main_kb(),
        )
        return

    await state.update_data(pending_group=group_name)
    await state.set_state(GroupFlow.waiting_name)
    await message.answer(
        f"✅ Это группа <b>{escape(group_name)}</b>.\n\n{texts.ASK_FULL_NAME}",
        parse_mode="HTML",
    )


@router.message(GroupFlow.waiting_name)
async def process_name(message: Message, state: FSMContext, conn) -> None:
    """Разобрать ФИО и зарегистрировать студента в найденной группе."""
    data = await state.get_data()
    group_name = str(data.get("pending_group") or "")

    full_name = service.normalize_full_name(message.text)
    if full_name is None:
        await message.answer(texts.ASK_FULL_NAME_AGAIN, parse_mode="HTML")
        return

    if not group_name:
        # Состояние потерялось (перезапуск) — просим код заново.
        await state.set_state(GroupFlow.waiting_code)
        await message.answer(texts.ASK_CODE, parse_mode="HTML")
        return

    result = service.join_group(conn, _tg_id(message), _code_of(conn, group_name),
                                full_name)
    if not result["ok"]:
        await state.clear()
        await message.answer(
            _join_error_text(result, group_name),
            parse_mode="HTML",
            reply_markup=kb.main_kb(),
        )
        return

    await state.clear()
    await message.answer(
        texts.joined_ok(escape(str(result["group_name"]))),
        parse_mode="HTML",
        reply_markup=kb.main_kb(),
    )


def _code_of(conn, group_name: str) -> str:
    """Код приглашения группы (нужен, т.к. join_group принимает код)."""
    group = service.get_group(conn, group_name)
    return str(group["invite_code"]) if group else ""


def _join_error_text(result: dict, group_name: str) -> str:
    """Текст ошибки регистрации по машинному коду причины."""
    error = result.get("error")
    if error == "already_in_group":
        return texts.ALREADY_IN_GROUP.format(group=escape(group_name))
    if error == "code_not_found":
        return texts.CODE_NOT_FOUND
    return texts.ASK_FULL_NAME_AGAIN
# --- создание группы (староста) ---

@router.callback_query(F.data == kb.CB_CREATE)
async def create_group_start(callback: CallbackQuery, state: FSMContext) -> None:
    """«⭐ Создать группу» — просим название группы."""
    await state.set_state(GroupFlow.waiting_group_name)
    if callback.message is not None:
        await callback.message.answer(texts.ASK_GROUP_NAME, parse_mode="HTML")
    await callback.answer()


@router.message(GroupFlow.waiting_group_name)
async def process_group_name(message: Message, state: FSMContext, conn) -> None:
    """Найти группу в расписании и перейти к вводу ФИО старосты."""
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await message.answer(texts.ASK_GROUP_NAME, parse_mode="HTML")
        return

    group_name = service.find_group_by_name(conn, raw)
    if group_name is None:
        suggestions = service.suggest_group_names(conn, raw)
        if suggestions:
            await message.answer(
                texts.GROUP_SUGGESTIONS_HEADER.format(group=escape(raw))
                + "\n\n" + texts.GROUP_SUGGESTIONS_HINT,
                parse_mode="HTML",
                reply_markup=kb.group_suggestions_kb(suggestions),
            )
        else:
            await message.answer(
                texts.GROUP_NOT_FOUND.format(group=escape(raw)),
                parse_mode="HTML",
            )
        return

    if service.is_group_created(conn, group_name):
        await message.answer(
            texts.GROUP_ALREADY_CREATED.format(group=escape(group_name)),
            parse_mode="HTML",
        )
        return

    await state.update_data(pending_group=group_name)
    await state.set_state(GroupFlow.waiting_starosta_name)
    await message.answer(
        f"✅ Это группа <b>{escape(group_name)}</b>.\n\n{texts.ASK_FULL_NAME}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(kb.CB_PICK_GROUP_PREFIX))
async def pick_suggested_group(callback: CallbackQuery, state: FSMContext,
                               conn) -> None:
    """Староста выбрал группу из подсказанных."""
    group_name = (callback.data or "").removeprefix(kb.CB_PICK_GROUP_PREFIX)

    if service.is_group_created(conn, group_name):
        if callback.message is not None:
            await callback.message.answer(
                texts.GROUP_ALREADY_CREATED.format(group=escape(group_name)),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    await state.update_data(pending_group=group_name)
    await state.set_state(GroupFlow.waiting_starosta_name)
    if callback.message is not None:
        await callback.message.answer(
            f"✅ Это группа <b>{escape(group_name)}</b>.\n\n"
            f"{texts.ASK_FULL_NAME}",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(GroupFlow.waiting_starosta_name)
async def process_starosta_name(message: Message, state: FSMContext,
                                conn) -> None:
    """Создать группу и выдать код приглашения."""
    data = await state.get_data()
    group_name = str(data.get("pending_group") or "")

    full_name = service.normalize_full_name(message.text)
    if full_name is None:
        await message.answer(texts.ASK_FULL_NAME_AGAIN, parse_mode="HTML")
        return

    if not group_name:
        await state.set_state(GroupFlow.waiting_group_name)
        await message.answer(texts.ASK_GROUP_NAME, parse_mode="HTML")
        return

    code = service.create_group(conn, group_name, _tg_id(message), full_name)
    await state.clear()
    await message.answer(
        texts.starosta_created(escape(group_name), code),
        parse_mode="HTML",
        reply_markup=kb.new_code_kb(),
    )
# --- управление группой (только староста) ---

def _require_student(conn, tg_id: int) -> dict | None:
    """Студент, а не None: для обработчиков, которым нужна группа."""
    return service.get_student(conn, tg_id)


@router.callback_query(F.data == kb.CB_MANAGE)
async def manage(callback: CallbackQuery, conn) -> None:
    """«⚙️ Управление группой» — только староста."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    if not service.is_group_admin(conn, group_name,
                                  _callback_tg_id(callback)):
        await callback.answer(texts.MANAGE_DENIED, show_alert=True)
        return

    group = service.get_group(conn, group_name) or {}
    if callback.message is not None:
        await callback.message.answer(
            texts.group_management(escape(group_name),
                                   str(group.get("invite_code") or "")),
            parse_mode="HTML",
            reply_markup=kb.group_management_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == kb.CB_SHOW_CODE)
async def show_code(callback: CallbackQuery, conn) -> None:
    """«🔑 Показать код» — только староста."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    if not service.is_group_admin(conn, group_name,
                                  _callback_tg_id(callback)):
        await callback.answer(texts.MANAGE_DENIED, show_alert=True)
        return

    group = service.get_group(conn, group_name) or {}
    if callback.message is not None:
        await callback.message.answer(
            texts.starosta_created(escape(group_name),
                                   str(group.get("invite_code") or "")),
            parse_mode="HTML",
            reply_markup=kb.new_code_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == kb.CB_NEW_CODE)
async def new_code(callback: CallbackQuery, conn) -> None:
    """«🔄 Новый код» — перегенерация (только староста)."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    code = service.regenerate_invite_code(conn, group_name,
                                          _callback_tg_id(callback))
    if code is None:
        await callback.answer(texts.MANAGE_DENIED, show_alert=True)
        return

    if callback.message is not None:
        await callback.message.answer(
            texts.new_code(code), parse_mode="HTML",
            reply_markup=kb.group_management_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == kb.CB_MAKE_DEPUTY)
async def make_deputy(callback: CallbackQuery, conn) -> None:
    """«👤 Назначить зама» — список студентов без зама (только староста)."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    if not service.is_group_admin(conn, group_name,
                                  _callback_tg_id(callback)):
        await callback.answer(texts.MANAGE_DENIED, show_alert=True)
        return

    candidates = service.deputy_candidates(conn, group_name)
    if callback.message is not None:
        if not candidates:
            await callback.message.answer(texts.DEPUTY_NOBODY,
                                          parse_mode="HTML",
                                          reply_markup=kb.group_management_kb())
        else:
            await callback.message.answer(
                "👤 <b>Кого назначить замом?</b>", parse_mode="HTML",
                reply_markup=kb.deputy_candidates_kb(candidates),
            )
    await callback.answer()


@router.callback_query(F.data.startswith(kb.CB_DEPUTY_PREFIX))
async def set_deputy_from_list(callback: CallbackQuery, conn) -> None:
    """Староста выбрал студента — назначаем замом."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    raw = (callback.data or "").removeprefix(kb.CB_DEPUTY_PREFIX)
    try:
        target_tg_id = int(raw)
    except ValueError:
        await callback.answer()
        return

    target = service.get_student(conn, target_tg_id)
    if target is None or str(target["group_name"]) != group_name:
        await callback.answer(texts.NOT_YOUR_GROUP, show_alert=True)
        return

    ok = service.set_deputy(conn, group_name, target_tg_id,
                            _callback_tg_id(callback))
    if not ok:
        await callback.answer(texts.MANAGE_DENIED, show_alert=True)
        return

    if callback.message is not None:
        await callback.message.answer(
            texts.deputy_assigned(escape(str(target["full_name"]))),
            parse_mode="HTML",
            reply_markup=kb.group_management_kb(),
        )
    await callback.answer()
# --- список группы ---

@router.callback_query(F.data == kb.CB_LIST)
async def show_list(callback: CallbackQuery, conn) -> None:
    """«📋 Список группы» — студенты по алфавиту, с ролями."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    students = service.get_group_students(conn, group_name)

    lines: list[str] = []
    for index, item in enumerate(students, 1):
        label = service.role_label(service.role_of(item))
        row = f"{index}. {escape(str(item['full_name']))}"
        if label:
            row += f" — {label}"
        lines.append(row)

    if callback.message is not None:
        await callback.message.answer(
            texts.group_list(escape(group_name), lines),
            parse_mode="HTML",
            reply_markup=kb.back_to_group_kb(),
        )
    await callback.answer()


@router.callback_query(F.data == kb.CB_BACK)
async def back_to_group(callback: CallbackQuery, state: FSMContext, conn) -> None:
    """«🔙 Назад» — вернуться к карточке группы."""
    if callback.message is not None:
        await my_group(callback.message, state, conn)
    await callback.answer()


# --- кнопки посещаемости: реальные обработчики (этап 2) ---

@router.callback_query(F.data == kb.CB_MARK)
async def btn_mark(callback: CallbackQuery, conn) -> None:
    """«✏️ Отметиться на паре» — экран отметки (этап 2)."""
    if callback.message is not None:
        await att_marks.cmd_attendance(callback.message, conn)
    await callback.answer()


@router.callback_query(F.data == kb.CB_MY_ATTENDANCE)
async def btn_my_attendance(callback: CallbackQuery, conn) -> None:
    """«📊 Моя посещаемость» — сводка за месяц (этап 2).

    Идентификатор берём у автора нажатия (``callback.from_user``), а не у
    ``callback.message``: сообщение с кнопкой отправлено ботом, и его
    ``from_user`` — сам бот, а не студент.
    """
    if callback.message is not None:
        await att_marks.send_my_attendance(callback.message, conn,
                                           callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "grp:my_att")
async def btn_my_attendance_legacy(callback: CallbackQuery, conn) -> None:
    """Старое значение callback кнопки «📊 Моя посещаемость».

    Кнопка жила в «Моей группе» и вела на ``grp:my_att``; после переезда в
    «Профиль» она стала ``profile:my_attendance``. Сообщения, отправленные
    раньше, всё ещё несут старый callback — нажатие по ним должно работать,
    а не молчать.
    """
    if callback.message is not None:
        await att_marks.send_my_attendance(callback.message, conn,
                                           callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == kb.CB_MARK_MANUAL)
async def btn_mark_manual(callback: CallbackQuery, conn, bot) -> None:
    """«✏️ Отметить вручную» — запускает опрос по текущей паре.

    Полноценная ручная отметка доступна командой ``/mark дата пара``:
    там список студентов с переключением статуса. Здесь — быстрый путь:
    отправить опрос в чат группы досрочно.
    """
    from datetime import datetime

    from bot.attendance import attendance_service as att_svc
    from bot.config import KRASNOYARSK

    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    group_name = str(student["group_name"])
    now = datetime.now(KRASNOYARSK)
    para = att_svc.current_para(now)

    if callback.message is not None:
        if para is None:
            await callback.message.answer(
                "Сейчас пары нет. Укажи её явно: "
                "<code>/mark сегодня 2</code>",
                parse_mode="HTML",
            )
        else:
            lesson = att_svc.get_lesson_for_para(conn, group_name,
                                                 now.date(), para)
            if lesson is None:
                await callback.message.answer(
                    "🤔 У группы нет пары с таким номером сейчас.",
                    parse_mode="HTML",
                )
            else:
                await callback.message.answer(
                    f"✏️ <b>Ручная отметка</b>\n\n"
                    f"{para} пара · {now.date().strftime('%d.%m')}\n\n"
                    "Отправить опрос в чат группы или отметь вручную: "
                    f"<code>/mark сегодня {para}</code>",
                    parse_mode="HTML",
                    reply_markup=att_kb.request_poll_kb(
                        now.date().isoformat(), para),
                )
    await callback.answer()


@router.callback_query(F.data == kb.CB_REPORT)
async def btn_report(callback: CallbackQuery, conn) -> None:
    """«📊 Отчёт за неделю» — отчёт старосты (этап 2)."""
    student = _require_student(conn, _callback_tg_id(callback))
    if student is None:
        await callback.answer(texts.NOT_REGISTERED, show_alert=True)
        return

    if callback.message is not None:
        await att_marks.send_week_report(callback.message, conn,
                                        str(student["group_name"]))
    await callback.answer()


# --- /make_deputy как команда ---

@router.message(Command("make_deputy"))
async def cmd_make_deputy(message: Message, conn) -> None:
    """Команда /make_deputy — показать список для назначения зама."""
    student = _require_student(conn, _tg_id(message))
    if student is None:
        await message.answer(texts.NOT_REGISTERED, parse_mode="HTML")
        return

    group_name = str(student["group_name"])
    if not service.is_group_admin(conn, group_name, _tg_id(message)):
        await message.answer(texts.MANAGE_DENIED, parse_mode="HTML")
        return

    candidates = service.deputy_candidates(conn, group_name)
    if not candidates:
        await message.answer(texts.DEPUTY_NOBODY, parse_mode="HTML")
        return

    await message.answer(
        "👤 <b>Кого назначить замом?</b>", parse_mode="HTML",
        reply_markup=kb.deputy_candidates_kb(candidates),
    )