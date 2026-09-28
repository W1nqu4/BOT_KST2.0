"""Безопасность: валидация ввода и защита от флуда (шаг 12).

- :func:`sanitize_group` — единая точка проверки имени группы для мест, где
  ввод приходит не через FSM регистрации (например, команды админа).
  Нормализация переиспользует :func:`bot.parsers.groups.normalize_group_name`,
  чтобы не появилось двух разных представлений одной группы.
- :class:`RateLimiter` — ограничение частоты сообщений по ``tg_id``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from bot.parsers.groups import normalize_group_name

logger = logging.getLogger(__name__)

# Допустимый формат группы: буквы/цифры/дефис/слэш, 2..12 символов.
# Дополнительно требуем цифру в начале: номер группы КСТ всегда начинается
# с года («26КАД», «026С»), поэтому «XX» — не группа, хотя формально
# подходит под класс символов.
GROUP_PATTERN = re.compile(r"^[А-ЯЁA-Z0-9\-/]{2,12}$")
GROUP_STARTS_WITH_DIGIT = re.compile(r"^\d")

# Сколько сообщений в минуту разрешено одному пользователю.
DEFAULT_MAX_PER_MINUTE = 20

# Длина окна, секунды.
WINDOW_SECONDS = 60


def sanitize_group(text: str | None) -> str | None:
    """Проверить и нормализовать имя группы.

    Порядок: обрезка пробелов → нормализация (верхний регистр, без пробелов
    и дефисов, ведущая «О» → «0») → проверка формата.

    Args:
        text: ввод пользователя (может быть None).

    Returns:
        Нормализованное имя группы или None, если ввод не подходит.

    Примеры:
        ``«26кад» → «26КАД»``, ``«26 КАД» → «26КАД»``,
        ``«О26КАД» → «026КАД»``, ``«!» → None``, ``«XX» → None``.
    """
    if text is None:
        return None
    normalized = normalize_group_name(text)
    if not GROUP_PATTERN.match(normalized):
        return None
    if not GROUP_STARTS_WITH_DIGIT.match(normalized):
        # «XX» — буквы без года: такого номера группы у техникума нет.
        return None
    return normalized


class RateLimiter:
    """Ограничитель частоты сообщений по ``tg_id`` (скользящее окно).

    Хранит отметки времени последних сообщений: при превышении лимита
    сообщение отбрасывается. Скользящее окно, а не «счётчик в минуту»,
    потому что при счётчике можно отправить 20 сообщений в конце минуты
    и ещё 20 в начале следующей.

    Пример::

        limiter = RateLimiter(max_per_minute=20)
        if not await limiter.allow(tg_id):
            return  # молча игнорируем
    """

    def __init__(self, max_per_minute: int = DEFAULT_MAX_PER_MINUTE) -> None:
        """Создать ограничитель.

        Args:
            max_per_minute: сколько сообщений в минуту разрешено.
        """
        self._max = max_per_minute
        self._lock = asyncio.Lock()
        self._hits: dict[int, deque[float]] = {}

    async def allow(self, tg_id: int) -> bool:
        """Пропустить сообщение пользователя?

        Args:
            tg_id: Telegram id.

        Returns:
            True — можно обрабатывать; False — лимит исчерпан.
        """
        async with self._lock:
            now = time.monotonic()
            queue = self._hits.setdefault(tg_id, deque())
            # Выбрасываем отметки старше окна.
            while queue and now - queue[0] > WINDOW_SECONDS:
                queue.popleft()
            if len(queue) >= self._max:
                return False
            queue.append(now)
            return True

    async def reset(self, tg_id: int | None = None) -> None:
        """Сбросить историю (для тестов и ручной разблокировки).

        Args:
            tg_id: чей счётчик сбросить; None — сбросить всех.
        """
        async with self._lock:
            if tg_id is None:
                self._hits.clear()
            else:
                self._hits.pop(tg_id, None)

    def tracked_users(self) -> int:
        """Сколько пользователей сейчас в окне (для диагностики)."""
        return len(self._hits)
