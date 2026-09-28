"""Трекинг последнего экрана пользователя (шаг 11).

Нужен для обратной связи: когда студент пишет «ничего не работает», админ
должен видеть, где именно он был. Экран — короткая строка-идентификатор
(``schedule:today``, ``deadlines:list`` и т.п.).

Данные лежат в FSM-хранилище рядом с состояниями: отдельная таблица ради
одного ключа была бы избыточной, а при перезапуске бота потеря экрана
не критична.

Особенность aiogram: ``state.update_data`` требует активного FSM-контекста,
поэтому если его нет (например, обработчик не принимает ``state``), вызов
безопасно игнорируется.
"""
from __future__ import annotations

import logging
from datetime import date

from aiogram.fsm.context import FSMContext

logger = logging.getLogger(__name__)

# Ключ в FSM-данных.
SCREEN_KEY = "last_screen"

# Ключ в FSM-данных: дата, показанная на экране расписания. Нужна, чтобы
# ◀️/▶️ листали дни от показанного дня, а не от «сегодня»: иначе ▶️
# навсегда застревает на завтрашнем дне, а ◀️ уводит в воскресенье.
DATE_KEY = "screen_date"

# Значение, когда экран ещё не записан.
SCREEN_UNKNOWN = "unknown"

# Идентификаторы экранов (короткие и стабильные — попадают в сообщение админу).
SCREEN_SCHEDULE_TODAY = "schedule:today"
SCREEN_SCHEDULE_DAY = "schedule:day"
SCREEN_DEADLINES_LIST = "deadlines:list"
SCREEN_DEADLINES_ADD = "deadlines:add"
SCREEN_CALENDAR_MAIN = "calendar:main"
SCREEN_PROFILE = "profile"


async def set_last_screen(state: FSMContext | None, screen: str) -> None:
    """Запомнить текущий экран пользователя.

    Args:
        state: FSM-контекст (может быть None — тогда ничего не делаем).
        screen: идентификатор экрана.
    """
    if state is None:
        return
    try:
        await state.update_data(**{SCREEN_KEY: screen})
    except Exception:
        # Отсутствие FSM-хранилища не должно ломать сам обработчик.
        logger.debug("could not store last screen",
                     extra={"screen": screen}, exc_info=True)


async def get_last_screen(state: FSMContext | None) -> str:
    """Последний экран пользователя (``unknown``, если не записан)."""
    if state is None:
        return SCREEN_UNKNOWN
    try:
        data = await state.get_data()
    except Exception:
        return SCREEN_UNKNOWN
    return str(data.get(SCREEN_KEY) or SCREEN_UNKNOWN)


async def set_screen_date(state: FSMContext | None, d: date) -> None:
    """Запомнить дату, показанную на экране расписания.

    Args:
        state: FSM-контекст (может быть None — тогда ничего не делаем).
        d: дата, которую видит пользователь.
    """
    if state is None:
        return
    try:
        await state.update_data(**{DATE_KEY: d.isoformat()})
    except Exception:
        logger.debug("could not store screen date",
                     extra={"screen_date": d.isoformat()}, exc_info=True)


async def get_screen_date(state: FSMContext | None) -> date | None:
    """Дата экрана расписания; None — если не записана или испорчена."""
    if state is None:
        return None
    try:
        data = await state.get_data()
    except Exception:
        return None
    raw = data.get(DATE_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        logger.debug("could not parse screen date", extra={"raw": str(raw)})
        return None


def screen_label(screen: str) -> str:
    """Человекочитаемая подпись экрана для админа."""
    labels = {
        SCREEN_SCHEDULE_TODAY: "расписание: сегодня",
        SCREEN_SCHEDULE_DAY: "расписание: день",
        SCREEN_DEADLINES_LIST: "дедлайны: список",
        SCREEN_DEADLINES_ADD: "дедлайны: добавление",
        SCREEN_CALENDAR_MAIN: "календарь: ссылки",
        SCREEN_PROFILE: "профиль",
    }
    return labels.get(screen, screen or SCREEN_UNKNOWN)