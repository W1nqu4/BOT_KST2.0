"""Тесты очистки истории замен (bot.services.history_service).

Проверяются границы учебного года (30 июня) и цикл очистки. Важная деталь:
:func:`end_of_academic_year` описывает границу учебного года, но как
граница УДАЛЕНИЯ она не годится (с сентября по декабрь она в будущем —
история стиралась бы целиком). Удаление идёт по
:func:`previous_academic_year_end` — концу последнего завершившегося года.

Сеть и Telegram не используются; ``_sleep`` подменяется, чтобы цикл не
ждал сутки.
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from bot.db import get_connection
from bot.migrations import apply_migrations
from bot.services import history_service as hs

GROUP = "26КАД"

SUB_ROW = {
    "para": 2, "old_subject": "ОД.03 История",
    "new_subject": "ОД.07 Математика", "teacher": "Т", "room": "307А",
    "is_cancelled": False, "is_self_study": False,
}


async def _no_sleep(seconds: float) -> None:
    """Заглушка паузы: цикл не должен ждать сутки."""
    return None


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями."""
    c = get_connection(tmp_path / "history.db")
    apply_migrations(c)
    yield c
    c.close()


def _seed(conn, date_iso: str, para: int = 2) -> None:
    """Записать замену в историю на дату."""
    from bot import db

    db.save_substitution_history(conn, GROUP, date_iso,
                                 [dict(SUB_ROW, para=para)])


# --- end_of_academic_year ---

@pytest.mark.parametrize(("raw", "expected"), [
    (date(2026, 9, 1), date(2027, 6, 30)),    # сентябрь → след. год
    (date(2026, 12, 31), date(2027, 6, 30)),  # декабрь → след. год
    (date(2027, 1, 15), date(2027, 6, 30)),   # январь → этот год
    (date(2026, 6, 15), date(2026, 6, 30)),   # июнь → этот год
    (date(2026, 7, 1), date(2026, 6, 30)),    # июль: граница уже прошла
    (date(2026, 8, 15), date(2026, 6, 30)),   # август: граница уже прошла
    (date(2026, 6, 30), date(2026, 6, 30)),   # сам день границы
    (date(2026, 2, 28), date(2026, 6, 30)),
])
def test_end_of_academic_year(raw: date, expected: date) -> None:
    """Граница учебного года: 30 июня с учётом месяца."""
    assert hs.end_of_academic_year(raw) == expected


def test_end_of_academic_year_is_june_30() -> None:
    """Граница всегда 30 июня (день и месяц фиксированы)."""
    for month in range(1, 13):
        result = hs.end_of_academic_year(date(2026, month, 1))
        assert (result.month, result.day) == (6, 30)


# --- previous_academic_year_end: граница УДАЛЕНИЯ ---

@pytest.mark.parametrize(("raw", "expected"), [
    (date(2026, 9, 28), date(2026, 6, 30)),   # сентябрь: прошёл июнь этого года
    (date(2026, 12, 31), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 6, 30)),    # каникулы: тот же июнь
    (date(2026, 8, 31), date(2026, 6, 30)),
    (date(2027, 1, 15), date(2026, 6, 30)),   # январь: прошлогодний июнь
    (date(2026, 6, 15), date(2025, 6, 30)),   # июнь: текущий год ещё идёт
    (date(2026, 2, 28), date(2025, 6, 30)),
])
def test_previous_academic_year_end(raw: date, expected: date) -> None:
    """Граница удаления всегда в прошлом — иначе стиралась бы текущая история."""
    result = hs.previous_academic_year_end(raw)
    assert result == expected
    assert result < raw, "граница удаления не может быть в будущем"


def test_previous_academic_year_end_keeps_current_year() -> None:
    """Сентябрьские записи текущего учебного года не попадают под границу."""
    cutoff = hs.previous_academic_year_end(date(2026, 9, 28))
    assert date(2026, 9, 1) >= cutoff, "записи нового года должны остаться"
    assert date(2025, 12, 31) < cutoff, "прошлый год должен удаляться"


# --- cleanup-цикл ---

async def test_cleanup_loop_removes_old_records(conn, monkeypatch) -> None:
    """Цикл удаляет прошлогодние записи, сохраняя текущий учебный год."""
    _seed(conn, "2025-09-01", para=1)     # прошлый учебный год — удалится
    _seed(conn, "2025-12-31", para=2)     # прошлый учебный год — удалится
    _seed(conn, "2026-09-28", para=3)     # текущий учебный год — останется
    assert conn.execute(
        "SELECT COUNT(*) FROM substitution_history"
    ).fetchone()[0] == 3

    calls: list[int] = []

    async def stopping_sleep(seconds: float) -> None:
        calls.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(hs, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await hs.history_cleanup_loop(conn, today_provider=lambda: date(2026, 9, 28))

    assert calls, "цикл должен дойти до паузы"
    assert conn.execute(
        "SELECT COUNT(*) FROM substitution_history"
    ).fetchone()[0] == 1, "история текущего года должна остаться"
    assert conn.execute(
        "SELECT date_iso FROM substitution_history"
    ).fetchone()[0] == "2026-09-28"


async def test_cleanup_loop_keeps_current_year_on_july(conn,
                                                       monkeypatch) -> None:
    """В июле граница — 30 июня этого года: записи июня нового года целы."""
    _seed(conn, "2025-09-01", para=1)     # прошлый год — удалится
    _seed(conn, "2026-06-29", para=2)     # до границы — удалится
    _seed(conn, "2026-06-30", para=3)     # сама граница — останется

    async def stopping_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(hs, "_sleep", stopping_sleep)

    with pytest.raises(asyncio.CancelledError):
        await hs.history_cleanup_loop(conn, today_provider=lambda: date(2026, 7, 1))

    dates = [r[0] for r in conn.execute(
        "SELECT date_iso FROM substitution_history ORDER BY date_iso"
    )]
    # Записи 2025-09-01 нет, граница 2026-06-30 включена.
    assert "2025-09-01" not in dates
    assert dates == ["2026-06-30"]


async def test_cleanup_loop_survives_errors(conn, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: логируется и делается следующий проход.

    Подменяем ``hs.db`` целиком (а не атрибут внутри общего модуля
    ``bot.db``): ``monkeypatch.setattr(hs.db, ...)`` менял бы глобальный
    модуль и мог оставить подмену после теста, из-за чего цикл уходил
    в бесконечность вместо отмены.
    """
    calls: list[int] = []

    class FakeDb:
        """Заглушка модуля db: первый проход падает, второй отменяется."""

        @staticmethod
        def cleanup_substitution_history(connection, until_date_iso):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("БД занята")
            raise asyncio.CancelledError

    monkeypatch.setattr(hs, "db", FakeDb)
    monkeypatch.setattr(hs, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await hs.history_cleanup_loop(conn, today_provider=lambda: date(2026, 9, 28))

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_cleanup_loop_uses_interval(conn, monkeypatch) -> None:
    """Пауза между проходами — сутки."""
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(hs, "_sleep", recording_sleep)

    with pytest.raises(asyncio.CancelledError):
        await hs.history_cleanup_loop(conn, today_provider=lambda: date(2026, 9, 28))

    assert slept == [hs.HISTORY_CLEANUP_INTERVAL]
    assert hs.HISTORY_CLEANUP_INTERVAL == 24 * 3600


def test_history_cleanup_is_registered_as_background_task() -> None:
    """Очистка истории подключена в main как фоновая задача."""
    import inspect

    import bot.main as main_module

    source = inspect.getsource(main_module.build_background_tasks)
    assert "history_cleanup_loop" in source