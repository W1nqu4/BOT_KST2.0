"""Обработчики команд VK-бота.

Все хендлеры регистрируются внутри :func:`register_handlers`. Порядок правил
важен: конкретные команды объявляются раньше, «ловим всё» (fallback) — строго
последним, иначе он перехватит остальные (vkbottle проверяет правила по
порядку регистрации, первое подошедшее блокирует остальные).

Состояние диалога (ожидание ввода группы) — через штатный
:class:`vkbottle.BaseStateGroup` и ``state_dispenser``: состояние хранится по
``peer_id``, поэтому у каждого пользователя оно своё.

Что переиспользуется из Telegram-бота: ``bot.db`` (соединение),
``bot.migrations`` (схема), ``bot.services.schedule_service`` (расписание и
замены). Импортировать ``bot.handlers.*`` и ``bot.attendance.service`` нельзя —
они тянут aiogram. Своя логика VK живёт в :mod:`bot_vk.storage` и
:mod:`bot_vk.view`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from vkbottle import BaseStateGroup, GroupEventType
from vkbottle.bot import Bot, Message, MessageEvent

from bot import db
from bot.services import teacher_names
from bot.services.teacher_notify import notify_admin_about_teacher_application
from bot_vk import keyboards, storage, texts, tg_bridge, view

logger = logging.getLogger(__name__)

# Слова, которыми пользователь отменяет ввод группы.
CANCEL_WORDS = {"отмена", "cancel", "стоп", "назад", "не надо"}

# Недельного экрана в VK нет: в Telegram день выбирается навигацией внутри
# расписания (week_nav_kb), а не отдельной кнопкой. Набор кнопок совпадает,
# поэтому константы «сколько дней в неделе» здесь больше не нужно.

# Кнопки меню: по ним состояние ввода группы не должно перехватывать нажатие.
MENU_TEXTS = {
    keyboards.BTN_SCHEDULE, "Расписание", "расписание",
    keyboards.BTN_DEADLINES, "Дедлайны", "дедлайны",
    keyboards.BTN_MY_GROUP, "Моя группа", "моя группа",
    keyboards.BTN_PROFILE, "Профиль", "профиль",
    "/start", "start", "начать", "/link", "/unlink", "/mygroup",
    # Команды преподавателя: под состояние ввода группы/фамилии они попадать
    # не должны — иначе «/teacher_cancel» ушёл бы в поиск по справочнику.
    "/teacher_apply", "teacher_apply", "/teacher_status", "teacher_status",
    "/teacher_cancel", "teacher_cancel",
}
# Формат номера группы (как в bot.handlers.start.GROUP_PATTERN):
# буквы/цифры/дефис/слэш, 2..12 символов, строго целиком.
GROUP_PATTERN = re.compile(r"^[А-ЯЁA-Z0-9\-/]{2,12}$")

# Требование к кандидату от fuzzy-поиска: у номера группы есть цифра
# (год набора). Без этого «незнаю» или опечатка из одних букв может
# нечётко совпасть с чужой группой и молча её выбрать.
GROUP_HAS_DIGIT = re.compile(r"\d")


class UserState(BaseStateGroup):
    """Состояния диалога VK-бота."""

    waiting_group = "waiting_group"


class TeacherApplyState(BaseStateGroup):
    """Состояния заявки на роль преподавателя."""

    waiting_name = "teacher_apply_waiting_name"


def parse_link_code(text: str) -> str | None:
    """Вытащить код связки из сообщения вида ``/link ABC123``.

    Args:
        text: текст сообщения.

    Returns:
        Код в верхнем регистре или None, если кода нет.
    """
    import re as _re

    match = _re.match(r"^/link\s+([A-Za-z0-9]{4,12})\s*$", (text or "").strip())
    return match.group(1).upper() if match else None


def is_group_input(message) -> bool:
    """Ввод в состоянии ожидания — обрабатывать ли его как ввод группы.

    Правило для шага ``waiting_group``. Нужно потому, что правило по одному
    состоянию перехватывает ЛЮБОЙ текст: если пользователь так и не ввёл группу
    (состояние застряло), под него попадали бы команды и нажатия кнопок меню —
    снаружи это выглядит как «бот перестал отвечать на /start».

    Пропускаем дальше только то, что заведомо адресовано меню: команды («/...»)
    и подписи кнопок. Всё остальное (в том числе «не знаю») обрабатывает
    ``process_group`` и даёт понятную подсказку — студент не остаётся без ответа.
    """
    text = (message.text or "").strip()
    if text.startswith("/"):
        return False
    if text in MENU_TEXTS:
        return False
    return True


def register_error_logging(bot: Bot) -> None:
    """Логировать ошибки VK API с кодом — иначе сбой выглядит как тишина.

    vkbottle при ошибке внутри хендлера не роняет задачу: он передаёт её в
    ``error_handler`` и переходит к следующему хендлеру. Если ошибка при
    отправке (например, VK отверг параметры), пользователь не получает ничего,
    а в наших логах не остаётся ни строки — именно так выглядит «бот молчит».

    Обработчик ниже печатает код ошибки VK и метод, чтобы причину было видно
    в логах Railway, а не только в интерфейсе VK.
    """
    from vkbottle.exception_factory import VKAPIError

    # У настоящего Bot error_handler есть всегда; заглушки в тестах могут его
    # не иметь — тогда логирование просто не настраиваем.
    handler = getattr(bot, "error_handler", None)
    if handler is None or not hasattr(handler, "register_error_handler"):
        logger.debug("vk: error_handler недоступен, логирование не настроено")
        return

    @handler.register_undefined_error_handler
    async def on_undefined_error(error: Exception, *args, **kwargs) -> None:
        logger.error(
            "vk: необработанная ошибка хендлера: %s: %s",
            type(error).__name__, error,
            exc_info=not isinstance(error, VKAPIError),
        )

    @handler.register_error_handler(VKAPIError)
    async def on_vk_error(error: VKAPIError, *args, **kwargs) -> None:
        # Код из ответа VK (900-е — ошибки отправки сообщений:
        # 901 нет прав, 902 приватность, 911 неверная клавиатура и т.д.).
        logger.error(
            "vk: ошибка VK API: code=%s msg=%s",
            getattr(error, "code", "?"), error,
        )


async def send_text(message: Message, text: str, keyboard: str | None = None
                    ) -> bool:
    """Отправить сообщение, не теряя текст при отказе клавиатуры.

    VK может отвергнуть сообщение с клавиатурой (912 — «возможности бота
    выключены в сообществе», 911 — неверный формат). Раньше это давало полную
    тишину: vkbottle передаёт исключение в ``error_handler`` и идёт к
    следующему хендлеру, поэтому пользователь не получал ничего.

    При отказе повторяем отправку без клавиатуры: ответ важнее кнопок.
    Дубля не будет — повтор идёт из того же хендлера, а vkbottle считает
    хендлер успешным (исключение не всплывает) и останавливает цепочку.

    Args:
        message: входящее сообщение.
        text: текст ответа.
        keyboard: JSON-клавиатура (необязательно).

    Returns:
        True, если сообщение ушло (с клавиатурой или без неё).
    """
    if keyboard is None:
        await message.answer(text)
        return True

    try:
        await message.answer(text, keyboard=keyboard)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "vk: VK отверг сообщение с клавиатурой (код=%s): %s — "
            "отправляю без клавиатуры",
            getattr(exc, "code", "?"), exc,
        )

    try:
        await message.answer(text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "vk: сообщение не доставлено даже без клавиатуры (код=%s): %s",
            getattr(exc, "code", "?"), exc,
        )
        return False


def looks_like_group(text: str) -> bool:
    """Похож ли ввод на номер группы.

    Не пускаем в fuzzy-поиск фразы вроде «не знаю»: для них честнее попросить
    номер ещё раз. Регистр и пробелы не важны, но номер обязан быть целиком из
    допустимых символов и содержать цифру (год набора — «25КАД», «26ИМС1»).
    Без проверки на цифру фраза из букв могла нечётко совпасть с чужой группой.
    """
    compact = (text or "").strip().upper().replace(" ", "")
    if not GROUP_PATTERN.fullmatch(compact):
        return False
    return bool(GROUP_HAS_DIGIT.search(compact))


def register_handlers(bot: Bot, conn) -> None:
    """Зарегистрировать команды VK-бота.

    Args:
        bot: экземпляр ``vkbottle.bot.Bot``.
        conn: соединение SQLite (то же, что использует Telegram-бот).
    """
    register_error_logging(bot)

    # --- регистрация группы ---

    @bot.on.message(text=["/start", "start", "Начать", "начать"])
    async def start_handler(message: Message) -> None:
        """Приветствие: показать группу или попросить её указать."""
        group = storage.get_user_group(conn, message.from_id)

        if group:
            await bot.state_dispenser.delete(message.peer_id)
            await send_text(
                message,
                texts.GREETING_WITH_GROUP.format(name="друг", group=group),
                keyboard=keyboards.main_kb(),
            )
            await send_schedule(message, group, date.today())
            return

        # Группы нет — просим ввести и переводим в состояние ожидания.
        await bot.state_dispenser.set(
            message.peer_id, UserState.waiting_group
        )
        await message.answer(texts.ASK_GROUP)

    @bot.on.message(state=UserState.waiting_group, func=is_group_input)
    async def process_group(message: Message) -> None:
        """Принять номер группы, проверить и сохранить.

        Правило ``func=is_group_input`` отсекает команды и нажатия кнопок: они
        уходят обычным хендлерам, а не застревают здесь (см. :func:`is_group_input`).
        """
        raw = (message.text or "").strip()

        if raw.lower() in CANCEL_WORDS:
            await bot.state_dispenser.delete(message.peer_id)
            await message.answer(texts.GROUP_CANCELLED)
            return

        if not storage.available_groups(conn):
            # Кэш расписания пуст: сравнивать ввод не с чем.
            await bot.state_dispenser.delete(message.peer_id)
            await message.answer(texts.GROUP_CACHE_EMPTY)
            return

        if not looks_like_group(raw):
            await message.answer(texts.GROUP_INVALID)
            return

        group = storage.find_exact_group(conn, raw)
        if group is None:
            # Нечёткое совпадение не сохраняем: «25КД» и «25КАД» слишком похожи,
            # и автопринятие записало бы чужую группу. Показываем варианты.
            hints = storage.suggest_groups(conn, raw)
            answer = texts.GROUP_NOT_FOUND.format(query=raw)
            if hints:
                answer += texts.GROUP_SUGGESTIONS.format(
                    hints="\n".join(f"• {name}" for name in hints)
                )
            await message.answer(answer)
            return

        storage.save_user_group(conn, message.from_id, group)
        await bot.state_dispenser.delete(message.peer_id)
        await send_text(
            message,
            texts.GROUP_SAVED.format(group=group),
            keyboard=keyboards.main_kb(),
        )
        await send_schedule(message, group, date.today())
# --- связка с Telegram ---

    @bot.on.message(text=["/link <code>", "/link", "/unlink"])
    async def link_handler(message: Message) -> None:
        """Связать VK-аккаунт с Telegram по одноразовому коду."""
        vk_id = message.from_id
        raw = (message.text or "").strip()

        if raw.lower().startswith("/unlink"):
            if storage.unlink_account(conn, vk_id):
                await message.answer(texts.UNLINK_DONE)
            else:
                await message.answer(texts.UNLINK_NONE)
            return

        existing_tg = storage.get_tg_id_by_vk(conn, vk_id)
        if existing_tg:
            await message.answer(
                texts.LINK_ALREADY_LINKED.format(tg_id=existing_tg)
            )
            return

        code = parse_link_code(raw)
        if not code:
            await message.answer(
                texts.LINK_NEED_CODE.format(bot=storage.TELEGRAM_BOT_USERNAME)
            )
            return

        result = storage.link_account(conn, code, vk_id)
        if not result["ok"]:
            await message.answer(texts.LINK_ERROR.format(error=result["error"]))
            return

        # Если в Telegram уже выбрана группа — переносим её в VK, чтобы
        # студент сразу видел привычное расписание. Группу берём из таблицы
        # users (get_tg_group): get_user_group в storage читает vk_users.
        tg_group = storage.get_tg_group(conn, result["tg_id"])
        if tg_group:
            storage.save_user_group(conn, vk_id, tg_group)

        await message.answer(texts.LINK_SUCCESS)
        if tg_group:
            await send_text(
                message,
                texts.LINK_GROUP_FROM_TG.format(group=tg_group),
                keyboard=keyboards.main_kb(),
            )

    # --- заявка преподавателя ---

    @bot.on.message(text=["/teacher_apply", "teacher_apply"])
    async def teacher_apply_handler(message: Message) -> None:
        """Начать заявку на роль преподавателя (или показать текущий статус).

        Заявка подаётся ЗА связанный Telegram-аккаунт: таблица ``teachers``
        привязана к ``tg_id``, и одобрение в TG должно открывать доступ именно
        тому аккаунту. Без связки честно просим её оформить.
        """
        vk_id = message.from_id
        tg_id = storage.get_linked_tg_id(conn, vk_id)
        if tg_id is None:
            await message.answer(texts.TEACHER_NEED_LINK)
            return

        existing = storage.get_teacher(conn, tg_id)
        if existing:
            status = str(existing["status"])
            full_name = str(existing["full_name"] or "?")
            if status == db.TEACHER_PENDING:
                await message.answer(
                    texts.TEACHER_ALREADY_PENDING.format(fio=full_name)
                )
            elif status == db.TEACHER_APPROVED:
                await message.answer(
                    texts.TEACHER_ALREADY_APPROVED.format(fio=full_name)
                )
            else:
                await message.answer(texts.TEACHER_REJECTED)
            return

        # Заявки нет — просим фамилию и переходим в состояние ожидания.
        await bot.state_dispenser.set(
            message.peer_id, TeacherApplyState.waiting_name
        )
        await message.answer(texts.TEACHER_ASK_NAME)

    @bot.on.message(state=TeacherApplyState.waiting_name,
                    func=is_group_input)
    async def process_teacher_name(message: Message) -> None:
        """Найти ФИО по введённой фамилии и показать совпадения кнопками.

        Правило ``func=is_group_input`` (то же, что у ввода группы) отсекает
        команды и нажатия кнопок меню: под состояние попадал бы ЛЮБОЙ текст, и
        «/teacher_cancel» ушёл бы в поиск по справочнику.
        """
        raw = (message.text or "").strip()

        if raw.lower() in CANCEL_WORDS:
            await bot.state_dispenser.delete(message.peer_id)
            await message.answer(texts.TEACHER_APPLY_CANCELLED)
            return

        # Пустой текст (стикер, фото без подписи): просим фамилию заново.
        # Команды сюда не доходят — их отсекает func=is_group_input.
        if not raw:
            await message.answer(texts.TEACHER_ASK_SURNAME_AGAIN)
            return

        matches = teacher_names.match_names(raw)
        if not matches:
            await message.answer(
                texts.TEACHER_NOT_FOUND.format(query=raw)
            )
            return

        # Список кладём в payload состояния: в callback уходит индекс, а не
        # ФИО — короткий payload надёжнее длинной строки.
        # Список уже обрезан до MAX_CHOICES внутри match_names: показывать
        # «слишком много совпадений» не нужно, дальше уточнять нечем.
        await bot.state_dispenser.set(
            message.peer_id, TeacherApplyState.waiting_name,
            teacher_matches=matches,
        )
        await send_text(
            message, texts.TEACHER_CHOOSE_FIO,
            keyboard=keyboards.names_kb(matches),
        )

    @bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=MessageEvent,
                      payload_contains={keyboards.TEACHER_CB_FIELD:
                                        keyboards.TEACHER_CB_VALUE})
    async def teacher_pick_name(event: MessageEvent) -> None:
        """Нажатие кнопки с ФИО: создать заявку с выбранным ФИО.

        Индекс сверяется со списком из состояния: кнопка могла устареть
        (перезапуск бота, новая подборка) — тогда честно просим заново.
        """
        vk_id = event.user_id
        peer = await bot.state_dispenser.get(event.peer_id)
        matches = (peer.payload.get("teacher_matches") if peer else None) or []

        raw_index = (event.payload or {}).get(keyboards.TEACHER_CB_INDEX)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = -1

        if index < 0 or index >= len(matches):
            await event.show_snackbar(texts.TEACHER_STALE_CHOICE)
            return

        tg_id = storage.get_linked_tg_id(conn, vk_id)
        if tg_id is None:
            # Связку сняли между шагами — то же объяснение, что и в начале.
            await event.show_snackbar(texts.TEACHER_NEED_LINK)
            return

        full_name = str(matches[index])
        result = storage.apply_teacher(conn, tg_id, full_name)
        await bot.state_dispenser.delete(event.peer_id)

        if not result["ok"]:
            await event.send_message(
                texts.TEACHER_APPLY_FAILED.format(error=result["error"])
            )
        else:
            await event.send_message(
                texts.TEACHER_APPLIED.format(fio=full_name)
            )
            # Уведомляем админа в TELEGRAM (админ живёт там). Ссылку на
            # aiogram-бота даёт tg_bridge — aiogram в bot_vk не импортируется.
            await notify_admin_about_teacher_application(
                tg_bridge.get_tg_bot(), conn, tg_id, full_name, "vk",
            )

    @bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=MessageEvent,
                      payload_contains={
                          keyboards.TEACHER_CB_FIELD:
                              keyboards.TEACHER_CB_CANCEL_VALUE})
    async def teacher_cancel_pick(event: MessageEvent) -> None:
        """«🔙 Отмена» на шаге выбора ФИО."""
        await bot.state_dispenser.delete(event.peer_id)
        await event.send_message(texts.TEACHER_APPLY_CANCELLED)

    @bot.on.message(text=["/teacher_status", "teacher_status"])
    async def teacher_status_handler(message: Message) -> None:
        """Показать статус своей заявки."""
        tg_id = storage.get_linked_tg_id(conn, message.from_id)
        teacher = storage.get_teacher(conn, tg_id) if tg_id else None
        if teacher is None:
            await message.answer(texts.TEACHER_STATUS_NONE)
            return

        await message.answer(texts.TEACHER_STATUS.format(
            fio=str(teacher["full_name"] or "?"),
            status=teacher_names.status_rus(str(teacher["status"])),
            applied_at=str(teacher["applied_at"] or "")[:16],
        ))

    @bot.on.message(text=["/teacher_cancel", "teacher_cancel"])
    async def teacher_cancel_handler(message: Message) -> None:
        """Отменить свою заявку, только пока она не рассмотрена.

        Заодно снимаем состояние: команда может прийти в середине шага ввода
        фамилии, и «зависшее» состояние потом перехватывало бы обычный текст.
        """
        await bot.state_dispenser.delete(message.peer_id)

        tg_id = storage.get_linked_tg_id(conn, message.from_id)
        if tg_id is None:
            await message.answer(texts.TEACHER_CANCEL_NONE)
            return

        if storage.cancel_teacher_application(conn, tg_id):
            await message.answer(texts.TEACHER_CANCELLED)
            return

        # Либо заявки нет, либо она уже рассмотрена.
        teacher = storage.get_teacher(conn, tg_id)
        if teacher is None:
            await message.answer(texts.TEACHER_CANCEL_NONE)
        else:
            await message.answer(texts.TEACHER_CANCEL_FORBIDDEN.format(
                status=teacher_names.status_rus(str(teacher["status"])),
            ))

    # --- моя группа ---

    @bot.on.message(text=[keyboards.BTN_MY_GROUP, "Моя группа", "моя группа",
                          "/mygroup", "mygroup"])
    async def my_group_handler(message: Message) -> None:
        """Раздел «📊 Моя группа»: карточка группы со списком студентов.

        Своя учебная группа живёт в Telegram (``students.tg_id``), поэтому
        сначала нужна связка vk_id → tg_id: без неё староста не определится,
        и код приглашения показывать некому. Раскладка ответа повторяет
        TG-экран «Моя группа».
        """
        vk_id = message.from_id
        student = storage.get_student_by_vk(conn, vk_id)

        if student is None:
            # Различаем два случая: совсем нет связки и связка есть, но код
            # приглашения в TG ещё не введён — подсказки разные.
            if storage.get_linked_tg_id(conn, vk_id) is None:
                await message.answer(texts.MY_GROUP_NO_LINK)
            else:
                await message.answer(texts.MY_GROUP_NOT_REGISTERED)
            return

        group_name = str(student["group_name"])
        snapshot = storage.group_snapshot(conn, group_name)
        is_admin = storage.role_of(student) in storage.GROUP_ADMIN_ROLES

        await send_text(
            message,
            view.render_my_group(snapshot, is_admin=is_admin),
            keyboard=keyboards.main_kb(),
        )

    # --- расписание ---

    @bot.on.message(text=[keyboards.BTN_SCHEDULE, "Расписание", "расписание"])
    async def today_handler(message: Message) -> None:
        """Расписание на сегодня (в воскресенье — на понедельник)."""
        group = await _require_group(message)
        if group is None:
            return
        await send_schedule(message, group, date.today())

    # --- профиль ---

    @bot.on.message(text=[keyboards.BTN_PROFILE, "Профиль", "профиль"])
    async def profile_handler(message: Message) -> None:
        """Имя, группа и статус связки с Telegram."""
        vk_id = message.from_id
        group = storage.get_profile_group(conn, vk_id)
        tg_id = storage.get_tg_id_by_vk(conn, vk_id)

        if not group:
            await send_text(
                message,
                texts.PROFILE_NO_GROUP.format(name="друг"),
                keyboard=keyboards.main_kb(),
            )
            return

        lines = [
            "👤 Профиль",
            "",
            "Имя: друг",
            f"Группа: {group}",
            "",
        ]
        if tg_id:
            lines.append(texts.LINK_STATUS_LINKED.format(tg_id=tg_id))
            lines.append(texts.LINK_HINT_UNLINK)
        else:
            lines.append(texts.LINK_STATUS_NONE)
            lines.append(texts.LINK_HINT_OFFER)

        await send_text(
            message, "\n".join(lines), keyboard=keyboards.main_kb(),
        )

    # --- fallback: обязан быть последним ---

    @bot.on.message()
    async def fallback(message: Message) -> None:
        """Подсказка на неизвестную команду."""
        await send_text(message, texts.FALLBACK,
                        keyboard=keyboards.main_kb())

    # --- хелперы ---

    async def _require_group(message: Message) -> str | None:
        """Группа пользователя; если её нет — просим указать и вернуть None."""
        group = storage.get_user_group(conn, message.from_id)
        if group:
            return group

        await message.answer(texts.NO_GROUP_HINT)
        await bot.state_dispenser.set(
            message.peer_id, UserState.waiting_group
        )
        await message.answer(texts.ASK_GROUP)
        return None

    async def send_schedule(message: Message, group: str, target: date) -> None:
        """Отправить расписание на дату с учётом замен.

        В воскресенье показываем ближайший понедельник: расписание на текущий
        выходной бессмысленно, а так пользователь видит полезное.
        """
        if target.isoweekday() == 7:
            target = target + timedelta(days=1)
            await message.answer(texts.WEEKEND)

        lessons = view.lessons_with_substitutions(conn, group, target)
        await send_text(
            message,
            view.render_day(group, target, lessons),
            keyboard=keyboards.schedule_kb(),
        )