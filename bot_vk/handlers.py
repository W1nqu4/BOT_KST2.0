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

from vkbottle import BaseStateGroup
from vkbottle.bot import Bot, Message

from bot_vk import keyboards, storage, texts, view

logger = logging.getLogger(__name__)

# Слова, которыми пользователь отменяет ввод группы.
CANCEL_WORDS = {"отмена", "cancel", "стоп", "назад", "не надо"}

# Сколько дней показывать в «неделе» (как в Telegram).
WEEK_DAYS = 6

# Кнопки меню: по ним состояние ввода группы не должно перехватывать нажатие.
MENU_TEXTS = {
    keyboards.BTN_TODAY, "Сегодня", "сегодня",
    keyboards.BTN_WEEK, "Неделя", "неделя",
    keyboards.BTN_PROFILE, "Профиль", "профиль",
    keyboards.BTN_DEADLINES, "Дедлайны", "дедлайны",
    "/start", "start", "начать", "/link", "/unlink",
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

    # --- расписание ---

    @bot.on.message(text=["📆 Сегодня", "Сегодня", "сегодня"])
    async def today_handler(message: Message) -> None:
        """Расписание на сегодня (в воскресенье — на понедельник)."""
        group = await _require_group(message)
        if group is None:
            return
        await send_schedule(message, group, date.today())

    @bot.on.message(text=["📅 Неделя", "Неделя", "неделя"])
    async def week_handler(message: Message) -> None:
        """Расписание на 6 дней подряд, начиная с сегодняшнего."""
        group = await _require_group(message)
        if group is None:
            return
        await send_week(message, group, date.today())

    # --- профиль ---

    @bot.on.message(text=["👤 Профиль", "Профиль", "профиль"])
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

    async def send_week(message: Message, group: str, start: date) -> None:
        """Отправить расписание на WEEK_DAYS дней (режется по лимиту VK)."""
        for chunk in view.render_week(conn, group, start, days=WEEK_DAYS):
            await send_text(message, chunk, keyboard=keyboards.schedule_kb())