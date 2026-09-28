"""История замен: очистка по границе учебного года (шаг 3).

Таблица ``substitution_history`` копит замены за учебный год. Учебный год
заканчивается 30 июня: всё, что раньше, при следующем проходе удаляется,
чтобы таблица не росла бесконечно.

Вынесено отдельно от ``notify_service`` (по решению владельца): рассылка
уведомлений и обслуживание истории — разные задачи, и смешивать их в одном
модуле значило бы усложнить обе.

UI истории пока нет — это заготовка под будущую фичу.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from bot import db
from bot.services.schedule_service import _sleep

logger = logging.getLogger(__name__)

# Период прохода очистки: раз в сутки. Данные меняются медленно, чаще незачем.
HISTORY_CLEANUP_INTERVAL = 24 * 3600

# День окончания учебного года (30 июня).
ACADEMIC_YEAR_END_MONTH = 6
ACADEMIC_YEAR_END_DAY = 30

# Месяц начала учебного года: с сентября считается новый учебный год.
ACADEMIC_YEAR_START_MONTH = 9


def end_of_academic_year(d: date) -> date:
    """Дата окончания учебного года, в который попадает день ``d``.

    Учебный год идёт с 1 сентября по 30 июня. Логика:

    - сентябрь–декабрь → конец года наступает в СЛЕДУЮЩЕМ календарном году
      (1 сентября 2026 → 30 июня 2027);
    - январь–август → конец года в ТЕКУЩЕМ календарном году: январь–июнь
      впереди (15 января 2027 → 30 июня 2027), а июль–август — каникулы,
      граница уже прошла (1 июля 2027 → 30 июня 2027).

    Args:
        d: дата, для которой считаем границу.

    Returns:
        30 июня соответствующего учебного года.
    """
    if d.month >= ACADEMIC_YEAR_START_MONTH:
        return date(d.year + 1, ACADEMIC_YEAR_END_MONTH, ACADEMIC_YEAR_END_DAY)
    return date(d.year, ACADEMIC_YEAR_END_MONTH, ACADEMIC_YEAR_END_DAY)


def previous_academic_year_end(d: date) -> date:
    """Конец последнего УЖЕ ЗАВЕРШИВШЕГОСЯ учебного года (30 июня).

    Эта функция — то, что нужно как граница удаления
    (``date_iso < cutoff``): она всегда возвращает дату в прошлом.

    Почему не :func:`end_of_academic_year`: с сентября по декабрь та функция
    возвращает **будущий** июнь следующего года. Как граница удаления это
    означало бы «удалить всё раньше следующего июня», то есть стереть всю
    историю текущего учебного года при первом же проходе очистки —
    «память замен» обнулялась бы каждые сутки.

    Логика границы:

    - сентябрь–декабрь: прошёл 30 июня ЭТОГО года → старый год удаляется,
      записи нового (с 1 сентября) остаются;
    - июль–август: прошёл 30 июня ЭТОГО года → закончившийся год удаляется
      (он уже прошёл — каникулы);
    - январь–июнь: прошёл 30 июня ПРОШЛОГО года → текущий учебный год цел.

    Args:
        d: дата, для которой считаем границу.

    Returns:
        30 июня последнего завершившегося учебного года.
    """
    if d.month >= ACADEMIC_YEAR_END_MONTH + 1:
        # Июль–декабрь: 30 июня этого года уже прошло.
        return date(d.year, ACADEMIC_YEAR_END_MONTH, ACADEMIC_YEAR_END_DAY)
    # Январь–июнь: последний завершившийся год кончился в прошлом году.
    return date(d.year - 1, ACADEMIC_YEAR_END_MONTH, ACADEMIC_YEAR_END_DAY)


async def history_cleanup_loop(conn, today_provider=None) -> None:
    """Бесконечный цикл очистки истории замен (раз в сутки).

    Удаляет записи раньше границы текущего учебного года. Ошибка прохода не
    роняет задачу: логируется и цикл продолжается. ``asyncio.CancelledError``
    пролетает наружу — иначе задачу нельзя корректно остановить.

    Args:
        conn: соединение SQLite.
        today_provider: функция «сегодня» (для тестов).

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    moment_provider = today_provider or date.today
    while True:
        try:
            cutoff = previous_academic_year_end(moment_provider())
            removed = db.cleanup_substitution_history(conn, cutoff.isoformat())
            if removed:
                logger.info("history cleanup",
                            extra={"removed": removed, "until": cutoff.isoformat()})
        except asyncio.CancelledError:
            logger.info("history_cleanup_loop cancelled")
            raise
        except Exception:
            logger.exception("history cleanup failed")
        await _sleep(HISTORY_CLEANUP_INTERVAL)