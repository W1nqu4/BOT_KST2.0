"""Свободный ввод группы: «25кад» в чате без кнопок и кода старосты.

Отдельный роутер, подключаемый ПОСЛЕДНИМ. Причина: в aiogram фильтр
``F.text`` совпадает с любым текстовым сообщением, и на первом же сработавшем
хендлере распространение останавливается — даже если тот сделал ``return``.
Поэтому «перехватчик текста» обязан стоять в конце цепочки, иначе он отберёт
сообщения у дедлайнов, обратной связи и других FSM-шагов.

Здесь только тонкая обёртка условий: сам разбор и сохранение группы живут в
:func:`bot.handlers.start.save_schedule_group`, чтобы логика не разъезжалась
с FSM-вводом.
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import db

logger = logging.getLogger(__name__)

router = Router(name="group_input")

# Регексп «похоже на номер группы» для СВОБОДНОГО ввода.
#
# Отличается от GROUP_PATTERN регистрации расписания (там пользователь уже в
# режиме ввода группы, и лишний текст безвреден). Здесь перехватывается любой
# текст в чате, поэтому правило строже: обязательна ЦИФРА.
#
# Почему так: паттерн из ТЗ (``^[А-ЯЁA-Z0-9\\-/]{2,12}$``) пропускал обычные
# слова — «привет», «спасибо», «да» — и каждое из них сохранялось бы как имя
# группы с ответом «такой группы нет в расписании». Все группы КСТ содержат
# цифру (25КАД, 26МЭГ, 26С1, 25-КАД, 26/1), так что требование цифры ничего
# нужного не отсекает, а мусор убирает.
GROUP_LIKE = re.compile(r"^[А-ЯЁA-Z0-9\-/]{2,12}$")
_HAS_DIGIT = re.compile(r"\d")


def looks_like_group(text: str) -> bool:
    """Похож ли свободный текст на номер группы.

    Пробелы внутри номера допускаются («25 кад»): студенты пишут и так, а
    нормализация всё равно уберёт их перед сохранением.

    Args:
        text: то, что написал пользователь.

    Returns:
        True, если это может быть номер группы.
    """
    compact = "".join(str(text or "").split()).upper()
    if not compact or not _HAS_DIGIT.search(compact):
        return False
    return bool(GROUP_LIKE.match(compact))


@router.message(F.text)
async def free_group_input(message: Message, state: FSMContext, conn) -> None:
    """Понять номер группы, написанный просто в чат.

    Срабатывает только когда:

    - ввод похож на номер группы (:func:`looks_like_group`: формат 2..12
      символов из букв/цифр/дефиса/слэша И хотя бы одна цифра);
    - у пользователя ещё нет группы для расписания (иначе текст может быть
      адресован другому сценарию — например, ответу в обратной связи);
    - нет активного FSM-шага (ввод группы в FSM обрабатывает
      :func:`bot.handlers.start.process_group` — дублировать нельзя).

    Во всех остальных случаях молча выходим: сообщение получат обработчики
    ниже по цепочке.
    """
    text = (message.text or "").strip()
    tg_id = message.from_user.id if message.from_user else 0

    if not looks_like_group(text):
        return
    if db.get_user_group(conn, tg_id):
        return
    if await state.get_state() is not None:
        return

    from bot.handlers.start import save_schedule_group

    logger.info("group from free input", extra={"tg_id": tg_id})
    await save_schedule_group(message, conn, text, from_schedule=True)