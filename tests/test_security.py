"""Тесты безопасности: валидация группы и ограничение частоты (шаг 12)."""

import asyncio
import time

import pytest

from bot.utils.security import (
    DEFAULT_MAX_PER_MINUTE,
    WINDOW_SECONDS,
    RateLimiter,
    sanitize_group,
)


# --- sanitize_group ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("26кад", "26КАД"),          # регистр
    ("26 КАД", "26КАД"),         # пробел внутри
    ("  26КАД  ", "26КАД"),      # пробелы вокруг
    ("26КАД", "26КАД"),          # уже нормально
    ("О26КАД", "026КАД"),        # ведущая «О» → «0»
    ("026 КАД", "026КАД"),       # пробел + ноль
    ("26МОСДР-1", "26МОСДР1"),   # дефис удаляется
    ("26С1", "26С1"),
])
def test_sanitize_group_valid(raw: str, expected: str) -> None:
    assert sanitize_group(raw) == expected


@pytest.mark.parametrize("raw", [
    "XX",        # буквы без года — не номер группы
    "!",
    "",
    "   ",
    "26КАД!",
    "26КАДДОПЕКСТРА",   # 14 символов — длиннее лимита 12
    "2",                # слишком коротко
    None,
])
def test_sanitize_group_invalid(raw) -> None:
    assert sanitize_group(raw) is None


def test_sanitize_group_idempotent() -> None:
    """Повторная нормализация не меняет результат."""
    once = sanitize_group("26 кад")
    assert sanitize_group(once) == once


# --- RateLimiter ---

async def test_rate_limiter_allows_up_to_limit() -> None:
    """20 сообщений в минуту проходят, 21-е — отказ."""
    limiter = RateLimiter(max_per_minute=20)
    allowed = [await limiter.allow(1) for _ in range(19)]
    assert all(allowed), "первые 19 должны пройти"
    assert await limiter.allow(1) is True, "20-е проходит"
    assert await limiter.allow(1) is False, "21-е отклонено"


async def test_rate_limiter_is_per_user() -> None:
    """Лимит одного пользователя не влияет на другого."""
    limiter = RateLimiter(max_per_minute=2)
    assert await limiter.allow(1) is True
    assert await limiter.allow(1) is True
    assert await limiter.allow(1) is False

    assert await limiter.allow(2) is True, "другой пользователь не под лимитом"


async def test_rate_limiter_window_resets(monkeypatch) -> None:
    """Через 60 секунд окно сбрасывается и сообщения снова проходят."""
    limiter = RateLimiter(max_per_minute=2)
    await limiter.allow(1)
    await limiter.allow(1)
    assert await limiter.allow(1) is False

    # «Прокручиваем» время на 61 секунду вперёд.
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        time, "monotonic", lambda: real_monotonic() + WINDOW_SECONDS + 1
    )
    assert await limiter.allow(1) is True, "после окна лимит сброшен"


async def test_rate_limiter_concurrent_safe() -> None:
    """Одновременные запросы не превышают лимит (защита под замком)."""
    limiter = RateLimiter(max_per_minute=5)
    results = await asyncio.gather(*(limiter.allow(1) for _ in range(20)))
    assert sum(results) == 5, "ровно 5 из 20 прошли"


async def test_rate_limiter_reset() -> None:
    """reset снимает лимит у конкретного пользователя или у всех."""
    limiter = RateLimiter(max_per_minute=1)
    await limiter.allow(1)
    await limiter.allow(2)
    assert await limiter.allow(1) is False

    await limiter.reset(1)
    assert await limiter.allow(1) is True, "после reset пользователь снова может"

    await limiter.reset()
    assert limiter.tracked_users() == 0


def test_default_limit_is_twenty() -> None:
    assert DEFAULT_MAX_PER_MINUTE == 20
    assert WINDOW_SECONDS == 60