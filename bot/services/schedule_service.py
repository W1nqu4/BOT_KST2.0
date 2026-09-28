"""Сервис расписания: чётность, выборка занятий, наложение замен (шаг 6).

Чётность — единственный источник правды :func:`week_type_for_date`:
**чётность ЧИСЛА МЕСЯЦА, не недели. День недели и месяц не влияют.**
Функция используется расписанием, .ics-подпиской (шаг 9) и уведомлениями
(шаг 10), поэтому дублировать формулу в других модулях запрещено.

Значения ``week_type`` — ровно как в источнике (DOCX) и в кэше:
``''`` (пара каждую неделю), ``'Чет'``, ``'нечет'``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from bot.config import (
    BELL_TIMES,
    SCHEDULE_REFRESH_SECONDS,
    SUBSTITUTIONS_REFRESH_SECONDS,
)
from bot.services import cache_service

logger = logging.getLogger(__name__)

# Значения чётности — те же строки, что пишет парсер расписания.
WEEK_TYPE_EVEN = "Чет"
WEEK_TYPE_ODD = "нечет"
WEEK_TYPE_ALWAYS = ""

# «📚 Предметы»: сколько пар показывать и на какой горизонт искать.
SUBJECT_LESSONS_LIMIT = 10
SUBJECT_HORIZON_DAYS = 60


def week_type_for_date(d: date) -> str:
    """Чётность для даты: ``'Чет'`` или ``'нечет'``.

    **Чётность ЧИСЛА МЕСЯЦА, не недели. День недели и месяц не влияют.**
    Пример: 28 сентября (понедельник) — ``'Чет'``, 21 сентября (понедельник) —
    ``'нечет'``; 01 октября — ``'нечет'`` (смена месяца отсчёт не сбрасывает).

    Args:
        d: дата.

    Returns:
        ``'Чет'`` если число месяца чётное, иначе ``'нечет'``.
    """
    return WEEK_TYPE_EVEN if d.day % 2 == 0 else WEEK_TYPE_ODD


def time_range_for_para(para_number: int) -> str:
    """Время пары по звонкам: ``'09:00-10:35'``; пустая строка, если пары нет.

    Args:
        para_number: номер пары (1..5).

    Returns:
        Интервал вида ``'HH:MM-HH:MM'`` или ``''`` для неизвестного номера.
    """
    bells = BELL_TIMES.get(para_number)
    if bells is None:
        return ""
    return f"{bells[0]}-{bells[1]}"


def _matches_week_type(lesson_week_type: str, target: str) -> bool:
    """Подходит ли занятие для недели ``target``.

    - ``''`` (в источнике пусто) — пара идёт каждую неделю, оставляем всегда;
    - совпадение с целевой чётностью — оставляем;
    - иначе — пропускаем.

    Args:
        lesson_week_type: значение из кэша (``''`` | ``'Чет'`` | ``'нечет'``).
        target: результат :func:`week_type_for_date`.

    Returns:
        True, если занятие показывается в эту неделю.
    """
    if lesson_week_type == WEEK_TYPE_ALWAYS:
        return True
    return lesson_week_type == target


def get_lessons_for_day(conn, group: str, d: date) -> list[dict]:
    """Занятия группы на дату с учётом чётности.

    Args:
        conn: соединение SQLite.
        group: имя группы (нормализуется при регистрации пользователя).
        d: дата.

    Returns:
        Список словарей с полями ``para_number`` (int), ``subject``,
        ``teacher``, ``room``, ``week_type``, ``time_range`` — отсортирован
        по ``para_number``. Пустой список, если занятий нет.
    """
    rows = cache_service.get_schedule_for_group_day(conn, group, d.isoweekday())
    target = week_type_for_date(d)

    lessons: list[dict] = []
    for row in rows:
        if not _matches_week_type(row["week_type"], target):
            continue
        para = int(row["para_number"])
        lessons.append({
            "para_number": para,
            "subject": row["subject"],
            "teacher": row["teacher"],
            "room": row["room"],
            "week_type": row["week_type"],
            "time_range": time_range_for_para(para),
        })

    # Сортировка по номеру пары как по числу, а не по строке.
    lessons.sort(key=lambda item: item["para_number"])
    return lessons
def apply_substitutions(conn, lessons: list[dict], group: str,
                        d: date) -> list[dict]:
    """Наложить лист замен на занятия дня.

    Правила:

    - замена на существующую пару ⇒ заменяются ``subject`` / ``teacher`` /
      ``room``, выставляется ``is_substitution=True``;
    - ``is_cancelled`` ⇒ у занятия ``is_cancelled=True`` (пара отменена);
    - ``is_self_study`` ⇒ у занятия ``is_self_study=True`` (самостоятельная
      работа);
    - замена на пару, которой нет в плане ⇒ добавляется отдельной записью
      с ``planned_subject=''``;
    - входной список **не мутируется**: возвращается новый.

    Args:
        conn: соединение SQLite.
        lessons: результат :func:`get_lessons_for_day`.
        group: имя группы.
        d: дата.

    Returns:
        Новый список занятий, отсортированный по ``para_number``. В каждой
        записи есть ``is_substitution`` (bool); для добавленных замен —
        ``planned_subject`` (``''`` у новых, исходный предмет у существующих).
    """
    # Глубокая копия словарей: входной список не должен меняться.
    result: list[dict] = []
    for lesson in lessons:
        item = dict(lesson)
        item.setdefault("is_substitution", False)
        item.setdefault("is_cancelled", False)
        item.setdefault("is_self_study", False)
        item.setdefault("planned_subject", item.get("subject", ""))
        result.append(item)

    substitutions = cache_service.get_substitutions_for_group_date(
        conn, group, d.isoformat()
    )
    if not substitutions:
        result.sort(key=lambda item: item["para_number"])
        return result

    by_para = {item["para_number"]: item for item in result}
    for sub in substitutions:
        para = int(sub["para"])
        cancelled = bool(sub["is_cancelled"])
        self_study = bool(sub["is_self_study"])
        lesson = by_para.get(para)

        if lesson is None:
            # Замена на пару, которой нет в плане: показываем отдельной строкой.
            result.append({
                "para_number": para,
                "subject": sub["new_subject"],
                "teacher": sub["teacher"],
                "room": sub["room"],
                "week_type": week_type_for_date(d),
                "time_range": time_range_for_para(para),
                "is_substitution": True,
                "is_cancelled": cancelled,
                "is_self_study": self_study,
                "planned_subject": "",  # пары не было в расписании
            })
            by_para[para] = result[-1]
            continue

        lesson["is_substitution"] = True
        if cancelled:
            lesson["is_cancelled"] = True
            # Отмену показываем текстом, а не пустотой.
            lesson["subject"] = sub["new_subject"] or lesson["subject"]
            continue
        if sub["new_subject"]:
            lesson["subject"] = sub["new_subject"]
        if sub["teacher"]:
            lesson["teacher"] = sub["teacher"]
        if sub["room"]:
            lesson["room"] = sub["room"]
        if self_study:
            lesson["is_self_study"] = True

    result.sort(key=lambda item: item["para_number"])
    return result


def get_subjects_for_group(conn, group: str) -> list[str]:
    """Уникальные предметы группы из кэша расписания.

    Args:
        conn: соединение SQLite.
        group: имя группы («26КАД»).

    Returns:
        Отсортированный список непустых названий предметов; пустой список,
        если расписание ещё не загружено.
    """
    rows = conn.execute(
        "SELECT DISTINCT subject FROM schedule_cache"
        " WHERE group_name = ? AND subject <> ''"
        " ORDER BY subject",
        (group,),
    ).fetchall()
    return [str(row["subject"]) for row in rows]


def get_nearest_lessons_for_subject(conn, group: str, subject: str,
                                    limit: int = SUBJECT_LESSONS_LIMIT,
                                    horizon_days: int = SUBJECT_HORIZON_DAYS,
                                    start: date | None = None) -> list[dict]:
    """Ближайшие пары группы по предмету, начиная с ``start`` (горизонт 60 дней).

    Каждый день прогоняется через :func:`get_lessons_for_day` (учёт чётности)
    и :func:`apply_substitutions` (учёт замен); берутся только пары с нужным
    предметом. Отменённые пары пропускаются — показывать их как занятие было
    бы неверно.

    Args:
        conn: соединение SQLite.
        group: имя группы.
        subject: точное название предмета из :func:`get_subjects_for_group`.
        limit: сколько пар вернуть максимум (по умолчанию 10).
        horizon_days: горизонт поиска в днях (включительно).
        start: дата начала; по умолчанию — сегодня.

    Returns:
        Список словарей ``{date, para_number, subject, teacher, room,
        time_range, week_type}``, отсортированный по дате, затем по номеру
        пары. Пустой список, если пар нет.
    """
    day = start or date.today()
    found: list[dict] = []
    for offset in range(horizon_days + 1):
        current = day + timedelta(days=offset)
        lessons = apply_substitutions(
            conn, get_lessons_for_day(conn, group, current), group, current
        )
        for lesson in lessons:
            if lesson.get("is_cancelled"):
                continue
            if lesson["subject"] != subject:
                continue
            found.append({
                "date": current,
                "para_number": lesson["para_number"],
                "subject": lesson["subject"],
                "teacher": lesson["teacher"],
                "room": lesson["room"],
                "time_range": lesson["time_range"],
                "week_type": lesson["week_type"],
            })
            if len(found) >= limit:
                found.sort(key=lambda item: (item["date"], item["para_number"]))
                return found
    found.sort(key=lambda item: (item["date"], item["para_number"]))
    return found


async def _sleep(seconds: float) -> None:
    """Пауза между проходами цикла.

    Вынесена в отдельную корутину, чтобы тесты могли подменить **только её**:
    патч ``asyncio.sleep`` целиком сломал бы сам event loop (им пользуется
    pytest-asyncio), и тест циклов зависал бы навсегда.

    Args:
        seconds: длительность паузы.
    """
    await asyncio.sleep(seconds)


async def refresh_schedule_loop(conn) -> None:
    """Бесконечный цикл обновления расписания (раз в 6 часов).

    Ошибки одного прохода не роняют задачу: логируются через
    ``log.exception`` и цикл продолжается. ``asyncio.CancelledError``
    **пролетает наружу** — иначе задачу нельзя корректно остановить
    при завершении приложения (шаг 12).

    Args:
        conn: соединение SQLite.

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    while True:
        try:
            await cache_service.refresh_schedule(conn)
        except asyncio.CancelledError:
            logger.info("refresh_schedule_loop cancelled")
            raise
        except Exception:
            logger.exception("refresh_schedule_loop iteration failed")
        await _sleep(SCHEDULE_REFRESH_SECONDS)


async def refresh_substitutions_loop(conn) -> None:
    """Бесконечный цикл обновления листа замен (раз в 15 минут).

    Поведение и обработка отмены — как в :func:`refresh_schedule_loop`.

    Args:
        conn: соединение SQLite.

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    while True:
        try:
            await cache_service.refresh_substitutions(conn)
        except asyncio.CancelledError:
            logger.info("refresh_substitutions_loop cancelled")
            raise
        except Exception:
            logger.exception("refresh_substitutions_loop iteration failed")
        await _sleep(SUBSTITUTIONS_REFRESH_SECONDS)
    return f"{bells[0]}-{bells[1]}"
