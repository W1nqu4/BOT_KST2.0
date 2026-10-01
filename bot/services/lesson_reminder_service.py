"""Умные напоминания: за 5 минут до пары и прогульщикам раз в неделю.

Две фоновые задачи:

A. :func:`lesson_reminder_loop` — раз в минуту проверяет, не начинается ли
   пара через 5 минут, и пишет каждому студенту группы в личку. Дедупликация
   через ``sent_notifications``: повторный проход в ту же минуту (или
   перезапуск бота) второго сообщения не даст.

B. :func:`weekly_truant_loop` — раз в неделю (воскресенье, 19:00) пишет тем,
   у кого за последние 7 дней накопилось 3 и больше прогулов.

Почему дедуп-ключ включает ``tg_id``: в ``sent_notifications`` первичный ключ
``(group_name, signature, notify_date)``, то есть строка помечает «уведомление
ушло» для ГРУППЫ. Если бы подпись была ``lesson:{дата}:{пара}``, после первого
же студента отметка закрыла бы рассылку для всей группы. Поэтому в подписи
есть получатель: ``lesson:{дата}:{пара}:{tg_id}``.

Замены здесь не накладываются (как и в /teacher): напоминание показывает
плановую пару из кэша. Наложение замен — отдельная тема, у неё своя рассылка
(:mod:`bot.services.notify_service`), и трогать её нельзя.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from html import escape

from aiogram.exceptions import TelegramForbiddenError

from bot import db
from bot.config import (
    BELL_TIMES_SATURDAY,
    BELL_TIMES_WEEKDAY,
    KRASNOYARSK,
)
from bot.services.schedule_service import (
    _sleep,
    apply_substitutions,
    get_lessons_for_day,
)
from bot.utils.text import plural_ru

logger = logging.getLogger(__name__)

# Период проверки напоминаний: раз в минуту (пара начинается в конкретную
# минуту, и промах в 60 секунд означал бы пропущенное напоминание).
LESSON_TICK_SECONDS = 60

# Период проверки недельной рассылки. Широкий час (весь 19:00) плюс метка в
# ``meta`` — расписание уйдёт один раз за воскресенье, даже если внутри часа
# проходов было несколько.
WEEKLY_TICK_SECONDS = 15 * 60

# Час недельной рассылки (воскресенье, по Красноярску).
WEEKLY_TRUANT_HOUR = 19
WEEKLY_TRUANT_WEEKDAY = 6           # воскресенье (Monday = 0)

# За сколько дней назад считаем прогулы.
TRUANT_WINDOW_DAYS = 7

# Со скольки прогулов беспокоим студента.
TRUANT_THRESHOLD = 3

# Статусы, которые считаются прогулом (late — тоже пропуск начала пары).
TRUANT_STATUSES = ("absent", "late")

# Ключ в ``meta``: дата последней недельной рассылки.
META_LAST_WEEKLY_TRUANT = "last_weekly_truant_date"

# Сравниваем минуту целиком, а не секунды. Так надёжнее: период цикла —
# 60 секунд плюс миллисекунды обработки, то есть каждый тик попадает в свою
# минуту, и минута «начало минус 5» посещается ровно один раз. Окно по
# секундам (±30) при таком периоде могло бы не поймать тик вовсе, если фаза
# сместится к концу минуты.


def _bells_for(weekday: int) -> dict[int, tuple[str, str]]:
    """Звонки для дня недели (суббота отличается).

    Args:
        weekday: 0..6, понедельник = 0 (как в :meth:`datetime.date.weekday`).

    Returns:
        Словарь «номер пары → (начало, конец)».
    """
    return BELL_TIMES_SATURDAY if weekday == 5 else BELL_TIMES_WEEKDAY


def _minutes_of_day(raw: str) -> int:
    """Перевести ``'HH:MM'`` в минуты от начала суток."""
    hours, minutes = raw.split(":")
    return int(hours) * 60 + int(minutes)


def upcoming_para(now: datetime, minutes_before: int = 5) -> int | None:
    """Номер пары, если её начало ровно через ``minutes_before`` минут.

    Учитывает звонки нужного дня: в субботу третья и четвёртая пары идут
    раньше будничных, пятой нет вовсе.

    Допуск — минута в обе стороны (цикл просыпается не ровно в :00.000, а
    внутри минуты), то есть при ``minutes_before=5`` напоминание уходит в
    минуту «начало минус 5».

    Args:
        now: текущий момент (по Красноярску).
        minutes_before: за сколько минут до начала пары напоминать.

    Returns:
        Номер пары (1..5) или None, если прямо сейчас напоминать не о чем.
    """
    target = now.hour * 60 + now.minute

    for para, (start, _end) in _bells_for(now.weekday()).items():
        if abs(_minutes_of_day(start) - minutes_before - target) <= 1:
            return para
    return None


def time_range_for(para: int, weekday: int) -> str:
    """Интервал пары по звонкам дня (``'09:00-10:35'``).

    Args:
        para: номер пары.
        weekday: 0..6, понедельник = 0.

    Returns:
        Интервал или пустая строка, если такой пары в этот день нет.
    """
    bells = _bells_for(weekday).get(para)
    if bells is None:
        return ""
    return f"{bells[0]}-{bells[1]}"


def reminder_signature(date_iso: str, para: int, tg_id: int) -> str:
    """Подпись напоминания для ``sent_notifications``.

    Включает получателя: ключ таблицы помечает группу, и без ``tg_id`` первое
    же сообщение закрыло бы рассылку для всей группы.
    """
    return f"lesson:{date_iso}:{para}:{tg_id}"


def lesson_reminder_text(para: int, lesson: dict, weekday: int) -> str:
    """Текст напоминания за 5 минут до пары."""
    subject = escape(str(lesson.get("subject") or "—"))
    room = escape(str(lesson.get("room") or "—"))
    return (
        f"🔔 <b>Через 5 минут — {para} пара</b>\n\n"
        f"📚 {subject}\n"
        f"🚪 {room}\n"
        f"⏰ {time_range_for(para, weekday)}"
    )
def truant_text(count: int) -> str:
    """Текст предупреждения прогульщику.

    Форма слова согласуется с числом («3 прогула», но «5 прогулов»):
    шаблон «{count} прогулов» давал бы «3 прогулов» — по-русски неверно, а
    счёт начинается именно с трёх.
    """
    word = plural_ru(count, "прогул", "прогула", "прогулов")
    return (
        f"⚠️ <b>У тебя {count} {word} за неделю</b>\n\n"
        "Заходи на пары, иначе могут не аттестовать.\n\n"
        "Посмотреть посещаемость: /my_attendance"
    )


async def _send_personal(conn, bot, tg_id: int, text: str,
                         log_message: str) -> bool:
    """Отправить личное сообщение, обработав блокировку бота.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        tg_id: получатель.
        text: текст сообщения (HTML).
        log_message: текст WARNING при прочих ошибках.

    Returns:
        True, если сообщение ушло.
    """
    try:
        await bot.send_message(tg_id, text, parse_mode="HTML")
        return True
    except TelegramForbiddenError:
        # Бот заблокирован: помечаем неактивным, чтобы не биться каждый проход.
        db.deactivate_user(conn, tg_id)
        logger.info("user deactivated (blocked bot)", extra={"tg_id": tg_id})
        return False
    except Exception:
        logger.warning(log_message, extra={"tg_id": tg_id})
        return False
async def check_reminders(conn, bot, now: datetime | None = None,
                          minutes_before: int = 5) -> int:
    """Разослать напоминания о паре, до которой ``minutes_before`` минут.

    Занятие берётся из расписания С УЧЁТОМ ЗАМЕН
    (:func:`bot.services.schedule_service.apply_substitutions`), поэтому:

    - отменённая пара (``is_cancelled``) напоминания не вызывает — идти некуда;
    - самостоятельная работа (``is_self_study``) тоже пропускается: это не
      пара с преподавателем;
    - предмет, кабинет и время в тексте — из замены, если она была.

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        now: текущий момент (для тестов); иначе — момент по Красноярску.
        minutes_before: за сколько минут до начала пары напоминать.

    Returns:
        Сколько сообщений отправлено.
    """
    moment = now or datetime.now(KRASNOYARSK)
    today = moment.date()
    date_iso = today.isoformat()

    para = upcoming_para(moment, minutes_before)
    if para is None:
        return 0

    sent = 0
    for student in db.get_all_students_with_group(conn):
        tg_id = int(student["tg_id"])
        group = str(student["group_name"])

        signature = reminder_signature(date_iso, para, tg_id)
        if db.is_substitution_sent(conn, group, signature, date_iso):
            continue

        lessons = get_lessons_for_day(conn, group, today)
        # Накладываем лист замен: без этого напоминание приходило бы и на
        # отменённую пару, а предмет/кабинет/время могли измениться заменой.
        lessons = apply_substitutions(conn, lessons, group, today)
        lesson = next(
            (item for item in lessons if int(item["para_number"]) == para),
            None,
        )
        if lesson is None:
            continue

        # Отменённую пару не напоминаем: идти некуда.
        if lesson.get("is_cancelled"):
            logger.info("lesson reminder skipped (cancelled)",
                        extra={"group": group, "date": date_iso, "para": para})
            continue

        # Самостоятельная работа — не пара с преподавателем, будильник по ней
        # только мешает. Вернуть напоминания для неё: убрать эту проверку.
        if lesson.get("is_self_study"):
            logger.info("lesson reminder skipped (self study)",
                        extra={"group": group, "date": date_iso, "para": para})
            continue

        text = lesson_reminder_text(para, lesson, moment.weekday())
        if await _send_personal(conn, bot, tg_id, text,
                                "lesson reminder failed"):
            # Метку ставим только после успешной отправки: иначе сбой
            # Telegram навсегда «съел» бы напоминание студенту.
            db.mark_substitution_sent(conn, group, signature, date_iso)
            sent += 1

    if sent:
        logger.info("lesson reminders sent",
                    extra={"date": date_iso, "para": para, "count": sent})
    return sent


async def send_truant_reminders(conn, bot, today: date | None = None) -> int:
    """Предупредить студентов с ``TRUANT_THRESHOLD`` прогулами за 7 дней.

    В сообщение добавлен блок «под угрозой неаттестации»: прогулы важны не
    сами по себе, а тем, что из-за них не аттестуют. Блок считается за
    текущий месяц (тот же период, что и на экране «Моя посещаемость»).

    Args:
        conn: соединение SQLite.
        bot: объект Bot.
        today: последний день окна (для тестов); иначе — сегодня.

    Returns:
        Сколько сообщений отправлено.
    """
    last_day = today or datetime.now(KRASNOYARSK).date()
    week_ago = (last_day - timedelta(days=TRUANT_WINDOW_DAYS)).isoformat()
    today_iso = last_day.isoformat()

    placeholders = ", ".join("?" for _ in TRUANT_STATUSES)
    rows = conn.execute(
        "SELECT tg_id, COUNT(*) AS cnt FROM attendance"
        f" WHERE status IN ({placeholders})"
        "   AND date_iso BETWEEN ? AND ?"
        " GROUP BY tg_id HAVING COUNT(*) >= ?",
        (*TRUANT_STATUSES, week_ago, today_iso, TRUANT_THRESHOLD),
    ).fetchall()

    sent = 0
    for row in rows:
        tg_id = int(row["tg_id"])
        count = int(row["cnt"])
        text = truant_text(count) + _attestation_part(conn, tg_id, last_day)
        if await _send_personal(conn, bot, tg_id, text,
                                "truant reminder failed"):
            sent += 1

    if sent:
        logger.info("truant reminders sent",
                    extra={"date": today_iso, "count": sent})
    return sent


def _attestation_part(conn, tg_id: int, today: date) -> str:
    """Блок «под угрозой неаттестации» для личного предупреждения.

    Пустая строка, если студент не в группе или все предметы аттестованы:
    без проблем незачем пугать.

    Args:
        conn: соединение SQLite.
        tg_id: студент.
        today: дата для расчёта периода (для тестов).

    Returns:
        HTML-блок (начинается с перевода строки) или пустая строка.
    """
    from bot.attendance import attendance_texts as atext
    from bot.attendance import attestation_service as atts
    from bot.attendance import db as att_db

    student = att_db.get_student(conn, tg_id)
    if student is None:
        return ""

    group = str(student["group_name"])
    period_start, period_end = atts.period_for_month(today, offset_months=0)
    summary = atts.get_attestation_summary(conn, tg_id, group, period_start,
                                           period_end)
    return atext.render_truant_attestation(summary["at_risk"])
async def lesson_reminder_loop(conn, bot, now_provider=None) -> None:
    """Бесконечный цикл напоминаний о паре (раз в минуту).

    Ошибка прохода не роняет задачу: логируется, и цикл продолжается.
    ``asyncio.CancelledError`` пролетает наружу — иначе задачу нельзя
    корректно остановить при завершении приложения.

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
            await check_reminders(conn, bot, moment_provider())
        except asyncio.CancelledError:
            logger.info("lesson_reminder_loop cancelled")
            raise
        except Exception:
            logger.exception("lesson_reminder failed")
        await _sleep(LESSON_TICK_SECONDS)


async def weekly_truant_loop(conn, bot, now_provider=None) -> None:
    """Бесконечный цикл недельной рассылки прогульщикам.

    Работает в воскресенье в час :data:`WEEKLY_TRUANT_HOUR` (по Красноярску).
    Проверка идёт каждые 15 минут, а дата последней рассылки хранится в
    ``meta`` — за одно воскресенье рассылка уходит ровно один раз, даже если
    внутри часа проходов было несколько или бот перезапустился.

    Поведение при ошибках и отмене — как в :func:`lesson_reminder_loop`.

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
            now = moment_provider()
            today_iso = now.date().isoformat()
            if (now.weekday() == WEEKLY_TRUANT_WEEKDAY
                    and now.hour == WEEKLY_TRUANT_HOUR):
                if db.get_meta(conn, META_LAST_WEEKLY_TRUANT) != today_iso:
                    await send_truant_reminders(conn, bot, now.date())
                    db.set_meta(conn, META_LAST_WEEKLY_TRUANT, today_iso)
        except asyncio.CancelledError:
            logger.info("weekly_truant_loop cancelled")
            raise
        except Exception:
            logger.exception("weekly_truant failed")
        await _sleep(WEEKLY_TICK_SECONDS)