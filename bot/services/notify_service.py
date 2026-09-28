"""Уведомления: напоминания о дедлайнах (шаг 8) и рассылка замен (шаг 10).

Дедупликация — через таблицу ``sent_notifications``: ключ
``(group_name, signature, notify_date)``. У замен ключ — сама группа и
подпись замены, у дедлайнов — псевдо-группа ``__deadline__``, чтобы два
потока уведомлений не смешивались.

Если отправка не удалась (пользователь заблокировал бота), помечаем
``users.is_active = 0``: иначе рассылка будет биться в него каждый проход.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, time, timedelta
from html import escape

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot import db
from bot.config import (
    NOTIFY_CONCURRENCY,
    NOTIFY_SEND_DELAY_SECONDS,
    KRASNOYARSK,
)
from bot.services import cache_service
from bot.services import deadline_service as dl
from bot.services.schedule_service import _sleep, time_range_for_date

logger = logging.getLogger(__name__)

# Псевдо-группа для дедлайновых уведомлений (не путать с реальными группами).
DEADLINE_GROUP = "__deadline__"

# Виды напоминаний о дедлайне: за день до срока и в день срока.
KIND_DAY_BEFORE = "day_before"
KIND_DAY_OF = "day_of"

# Окно рассылки замен: с 15:30 до 23:00 по Красноярску. Днём беспокоить
# не нужно, а после 23:00 сообщение уже не прочитают до утра.
NOTIFY_WINDOW_START = time(15, 30)
NOTIFY_WINDOW_END = time(23, 0)

# Пауза, когда мы вне окна (5 минут, а не 15: нужно точнее поймать 15:30).
OUTSIDE_WINDOW_SLEEP = 300

# Пауза между проходами внутри окна.
NOTIFY_INTERVAL = 15 * 60

# Предел сообщения Telegram; разбиваем по границам карточек.
MESSAGE_LIMIT = 4096

# Иконки — те же, что в расписании (единый язык с шагом 7).
ICON_SUBSTITUTION = "🔁"
ICON_CANCELLED = "❌"
ICON_SELF_STUDY = "📖"

WEEKDAY_NAMES = (
    "Понедельник", "Вторник", "Среда",
    "Четверг", "Пятница", "Суббота", "Воскресенье",
)


def _now_iso() -> str:
    """Текущий момент в ISO-8601 с поясом техникума."""
    return datetime.now(KRASNOYARSK).isoformat(timespec="seconds")


def deadline_message(item: dict) -> str:
    """Текст напоминания о дедлайне.

    Формат зависит от срока: ``🟡 Дедлайн завтра`` / ``🟠 Дедлайн сегодня``.

    Args:
        item: словарь дедлайна с ``days_left`` (0 — сегодня, 1 — завтра).

    Returns:
        HTML-текст сообщения; пустая строка, если срок не сегодня/завтра.
    """
    days = item.get("days_left")
    if days == 0:
        header = "🟠 <b>Дедлайн сегодня</b>"
    elif days == 1:
        header = "🟡 <b>Дедлайн завтра</b>"
    else:
        return ""

    task = escape(item.get("task") or "без названия")
    subject = escape(item.get("subject") or "")
    date_iso = item.get("deadline_date") or ""
    pretty = date_iso
    if date_iso:
        try:
            pretty = datetime.fromisoformat(date_iso).strftime("%d.%m")
        except ValueError:
            pretty = date_iso

    tail = f"<b>{task}</b>"
    if subject:
        tail += f" — {subject}"
    if pretty:
        tail += f", {pretty}"
    return f"{header}\n{tail}"


def _already_sent(conn, signature: str, notify_date: str) -> bool:
    """Отправляли ли уже уведомление о дедлайне в эту дату."""
    return db.is_substitution_sent(conn, DEADLINE_GROUP, signature, notify_date)


def _mark_sent(conn, signature: str, notify_date: str) -> None:
    """Отметить уведомление отправленным (идемпотентно)."""
    db.mark_substitution_sent(
        conn, DEADLINE_GROUP, signature, notify_date, _now_iso()
    )


def _deactivate_user(conn, tg_id: int) -> None:
    """Пометить пользователя неактивным (бот заблокирован)."""
    db.deactivate_user(conn, tg_id)


async def check_deadlines_once(conn, bot, today=None,
                               throttle: bool = True) -> int:
    """Разослать напоминания о дедлайнах на сегодня и завтра.

    Вызывается раз в час из фонового цикла. Дедупликация — через
    ``sent_notifications``, поэтому повторный вызов в тот же день ничего
    не отправит.

    Args:
        conn: соединение SQLite.
        bot: объект Bot для отправки.
        today: база отсчёта (для тестов).
        throttle: выдерживать ли паузу между отправками (в тестах — False).

    Returns:
        Количество успешно отправленных сообщений.
    """
    base = today or datetime.now(KRASNOYARSK).date()
    notify_date = base.isoformat()
    items = dl.remind_window(conn, base)
    if not items:
        return 0

    semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
    sent = 0

    async def _send(item: dict) -> bool:
        nonlocal sent
        kind = KIND_DAY_OF if item.get("days_left") == 0 else KIND_DAY_BEFORE
        signature = f"deadline:{item['id']}:{kind}"
        if _already_sent(conn, signature, notify_date):
            return False

        text = deadline_message(item)
        if not text:
            return False

        async with semaphore:
            try:
                await bot.send_message(chat_id=item["tg_id"], text=text,
                                       parse_mode="HTML")
            except Exception as exc:
                logger.warning(
                    "deadline notify failed",
                    extra={"tg_id": item["tg_id"], "error": repr(exc)},
                )
                _deactivate_user(conn, item["tg_id"])
                return False

        _mark_sent(conn, signature, notify_date)
        sent += 1
        if throttle and NOTIFY_SEND_DELAY_SECONDS:
            await asyncio.sleep(NOTIFY_SEND_DELAY_SECONDS)
        return True

    await asyncio.gather(*(_send(item) for item in items))
    logger.info("deadlines notified", extra={"count": sent, "date": notify_date})
    return sent


async def deadline_notify_loop(conn, bot, interval: int = 3600) -> None:
    """Бесконечный цикл напоминаний о дедлайнах (раз в час).

    Ошибки одного прохода не роняют задачу; ``asyncio.CancelledError``
    пролетает наружу, чтобы цикл останавливался вместе с приложением.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        interval: пауза между проверками, сек.

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    while True:
        try:
            await check_deadlines_once(conn, bot)
        except asyncio.CancelledError:
            logger.info("deadline_notify_loop cancelled")
            raise
        except Exception:
            logger.exception("deadline_notify_loop iteration failed")
        await _sleep(interval)


# ==========================================================================
# Рассылка замен (шаг 10)
# ==========================================================================

def substitution_signature(sub: dict) -> str:
    """Стабильная подпись замены для дедупликации.

    Собирается из всех значимых полей, поэтому при изменении любого из них
    (предмет, преподаватель, кабинет) подпись меняется, и студент получит
    новое уведомление — это правильно: замена действительно другая.

    Args:
        sub: словарь замены из ``substitutions_cache``.

    Returns:
        md5-хеш в hex.
    """
    raw = "|".join([
        str(sub.get("group") or sub.get("group_name") or ""),
        str(sub.get("date_iso") or ""),
        str(sub.get("para") or ""),
        str(sub.get("old_subject") or ""),
        str(sub.get("new_subject") or ""),
        str(sub.get("teacher") or ""),
        str(sub.get("room") or ""),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def next_school_day(from_date: date) -> date:
    """Следующий учебный день после указанной даты.

    Воскресенье — выходной, его пропускаем: в субботу занятия бывают, поэтому
    она остаётся учебным днём. Если сегодня воскресенье, следующий день —
    понедельник.

    Примеры: пт → сб, сб → пн, вс → пн, чт → пт.

    Args:
        from_date: дата, от которой считаем.

    Returns:
        Следующий учебный день.
    """
    candidate = from_date + timedelta(days=1)
    if candidate.weekday() == 6:        # воскресенье
        candidate += timedelta(days=1)  # понедельник
    return candidate


def weekday_name(d: date) -> str:
    """Название дня недели по-русски."""
    return WEEKDAY_NAMES[d.weekday()]


def substitution_icon(sub: dict) -> str:
    """Иконка замены: ❌ отмена → 📖 самостоятельная → 🔁 замена."""
    if sub.get("is_cancelled"):
        return ICON_CANCELLED
    if sub.get("is_self_study"):
        return ICON_SELF_STUDY
    return ICON_SUBSTITUTION


def _sub_date(sub: dict, target: date | None = None) -> date | None:
    """Дата замены: явный target или ``date_iso`` из словаря.

    Нужна, чтобы время пары считалось по звонкам нужного дня (в субботу
    звонки отличаются от будней).
    """
    if target is not None:
        return target
    raw = str(sub.get("date_iso") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def render_substitution_card(sub: dict,
                             target: date | None = None) -> str:
    """Карточка одной замены.

    Формат: иконка с номером пары, зачёркнутый прежний предмет, новый
    предмет, преподаватель, кабинет и время.

    Args:
        sub: словарь замены.
        target: дата замены (если не передана — берётся ``date_iso``):
            по ней определяется, субботние звонки или будничные.

    Returns:
        HTML-текст карточки (без завершающих переводов строк).
    """
    para = int(sub.get("para") or 0)
    old_subject = escape(str(sub.get("old_subject") or ""))
    new_subject = escape(str(sub.get("new_subject") or ""))
    teacher = escape(str(sub.get("teacher") or ""))
    room = escape(str(sub.get("room") or ""))

    lines = [f"{substitution_icon(sub)} <b>{para} пара</b>"]
    if old_subject:
        lines.append(f"<s>{old_subject}</s>")
    if new_subject:
        lines.append(f"<b>{new_subject}</b>")
    if teacher:
        lines.append(f"👤 {teacher}")
    if room:
        lines.append(f"🚪 {room}")
    time_range = time_range_for_date(para, _sub_date(sub, target))
    if time_range:
        lines.append(f"⏰ {time_range}")
    if sub.get("is_cancelled"):
        lines.append("<i>Пара отменена</i>")
    elif sub.get("is_self_study"):
        lines.append("<i>Самостоятельная работа</i>")
    return "\n".join(lines)


def split_blocks(header: str, blocks: list[str], footer: str,
                 limit: int = MESSAGE_LIMIT) -> list[str]:
    """Разбить сообщение на части по границам карточек.

    Telegram ограничивает сообщение 4096 символами; резать карточку посередине
    нельзя (сломается разметка), поэтому набираем карточки, пока влезают.

    Args:
        header: шапка сообщения.
        blocks: карточки (каждая — отдельный html-блок).
        footer: подпись в конце.
        limit: предельная длина одного сообщения.

    Returns:
        Список сообщений; пустой, если карточек нет.
    """
    if not blocks:
        return []

    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > limit and current != header:
            messages.append(current)
            current = f"{header}\n\n{block}"
        else:
            current = candidate

    # Футер добавляем, если влезает; иначе — отдельным сообщением.
    with_footer = f"{current}\n\n{footer}"
    if len(with_footer) <= limit:
        messages.append(with_footer)
    else:
        messages.append(current)
        messages.append(footer)
    return messages


def render_substitution_notification(group: str, subs: list[dict],
                                     target: date) -> list[str]:
    """Сообщения о новых заменах на дату.

    Args:
        group: имя группы.
        subs: новые (ещё не отправленные) замены.
        target: дата, к которой относятся замены.

    Returns:
        Список сообщений (одно или несколько), каждое — готовый HTML.
    """
    if not subs:
        return []

    header = (
        "🔔 <b>Замены на завтра</b>\n"
        f"<i>{escape(group)} · {escape(weekday_name(target))}, "
        f"{target.strftime('%d.%m')}</i>"
    )
    blocks = [render_substitution_card(sub, target) for sub in subs]
    footer = "<i>Загляни в «Расписание» — там всё уже с учётом этих замен.</i>"
    return split_blocks(header, blocks, footer)
async def _send_to_user(conn, bot, tg_id: int, texts: list[str]) -> bool:
    """Отправить пользователю сообщения о заменах.

    Обрабатывает две ожидаемые ошибки Telegram:

    - ``TelegramForbiddenError`` — пользователь заблокировал бота: помечаем
      неактивным, чтобы рассылка больше в него не билась;
    - ``TelegramRetryAfter`` — флуд-контроль: ждём указанное время и делаем
      одну повторную попытку.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        tg_id: получатель.
        texts: список сообщений.

    Returns:
        True, если все сообщения ушли.
    """
    async def _deliver() -> None:
        for text in texts:
            await bot.send_message(tg_id, text, parse_mode="HTML")

    try:
        await _deliver()
        return True
    except TelegramForbiddenError:
        db.deactivate_user(conn, tg_id)
        logger.info("user deactivated (blocked bot)", extra={"tg_id": tg_id})
        return False
    except TelegramRetryAfter as exc:
        pause = int(getattr(exc, "retry_after", 1)) + 1
        logger.warning("flood control, retrying",
                       extra={"tg_id": tg_id, "retry_after": pause})
        await asyncio.sleep(pause)
        try:
            await _deliver()
            return True
        except Exception:
            logger.warning("retry failed", extra={"tg_id": tg_id})
            return False
    except Exception as exc:
        logger.warning("send failed",
                       extra={"tg_id": tg_id, "error": repr(exc)})
        return False


async def _process_substitutions(conn, bot, target: date,
                                 throttle: bool = True) -> int:
    """Разослать замены на дату группам с активными подписчиками.

    Алгоритм:

    1. берём группы, у которых есть активные пользователи;
    2. для каждой группы читаем замены на дату и отбрасываем уже отправленные
       (по подписи в ``sent_notifications``);
    3. если есть новые — рассылаем их пользователям группы и помечаем
       отправленными.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        target: дата замен.
        throttle: выдерживать ли антифлуд-паузу (в тестах — False).

    Returns:
        Количество успешно отправленных сообщений.
    """
    groups = db.get_notify_groups(conn)
    if not groups:
        return 0

    semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
    date_iso = target.isoformat()
    sent = 0

    for group in groups:
        subs = cache_service.get_substitutions_for_group_date(
            conn, group, date_iso
        )
        if not subs:
            continue

        new_subs = [
            sub for sub in subs
            if not db.is_substitution_sent(
                conn, group, substitution_signature(sub), date_iso
            )
        ]
        if not new_subs:
            continue

        texts = render_substitution_notification(group, new_subs, target)
        if not texts:
            continue

        user_ids = db.get_users_by_group(conn, group)
        for tg_id in user_ids:
            # Уважаем настройку пользователя: он мог отключить уведомления.
            if not db.get_notifications_enabled(conn, tg_id):
                continue
            async with semaphore:
                if await _send_to_user(conn, bot, tg_id, texts):
                    sent += 1
                if throttle and NOTIFY_SEND_DELAY_SECONDS:
                    await asyncio.sleep(NOTIFY_SEND_DELAY_SECONDS)

        # Помечаем отправленным даже если у группы не оказалось подписчиков:
        # иначе при первой же регистрации студент получит старые замены.
        for sub in new_subs:
            db.mark_substitution_sent(
                conn, group, substitution_signature(sub), date_iso
            )

    logger.info("substitutions notified",
                extra={"count": sent, "date": date_iso, "groups": len(groups)})
    return sent


def in_notify_window(moment: datetime) -> bool:
    """Попадает ли момент в окно рассылки (15:30–23:00 местного времени)."""
    return NOTIFY_WINDOW_START <= moment.time() <= NOTIFY_WINDOW_END


async def notify_substitutions_loop(conn, bot, now_provider=None) -> None:
    """Бесконечный цикл рассылки замен.

    Работает только в окне 15:30–23:00: днём замен ещё может не быть, а
    ночью уведомление неуместно. Вне окна спит 5 минут (чтобы точнее поймать
    начало), внутри — 15 минут.

    Ошибки одного прохода не роняют задачу; ``asyncio.CancelledError``
    пролетает наружу для корректной остановки приложения.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now_provider: функция «текущий момент» (для тестов).

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    moment_provider = now_provider or (lambda: datetime.now(KRASNOYARSK))

    while True:
        try:
            moment = moment_provider()
            if not in_notify_window(moment):
                await _sleep(OUTSIDE_WINDOW_SLEEP)
                continue

            target = next_school_day(moment.date())
            await _process_substitutions(conn, bot, target)
        except asyncio.CancelledError:
            logger.info("notify_substitutions_loop cancelled")
            raise
        except Exception:
            logger.exception("notify_substitutions_loop failed")

        await _sleep(NOTIFY_INTERVAL)
