"""Inline-плитки голосования за посещаемость.

Callback-данные:

- ``vote:confirm:{date}:{para}:{target_tg_id}`` — подтвердить студента;
- ``vote:close:{date}:{para}`` — закрыть голосование (староста);
- ``vote:start`` — выбрать пару для запуска (староста);
- ``vote:pick:{date}:{para}`` — выбранная пара.

Группа в callback не входит: голосование адресуется парой и датой, а группу
обработчик берёт у студента (``att_db.get_student``). Так строка короче
(лимит 64 байта) и нельзя проголосовать в чужой группе, подделав данные.
"""
from __future__ import annotations

from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.attendance import vote_texts as vtext
from bot.utils.text import plural_ru

# Callback-данные.
CB_CONFIRM_PREFIX = "vote:confirm:"   # vote:confirm:2026-09-30:3:7002
CB_CLOSE_PREFIX = "vote:close:"       # vote:close:2026-09-30:3
CB_START = "vote:start"
CB_PICK_PREFIX = "vote:pick:"         # vote:pick:2026-09-30:3

# Ограничения Telegram: callback_data — до 64 байт, текст кнопки — до 64
# символов. Обрезаем ФИО, чтобы длинные имена не ломали разметку.
MAX_CALLBACK_BYTES = 64
MAX_BUTTON_TEXT = 60


def confirm_callback(date_iso: str, para: int, target_tg_id: int) -> str:
    """Собрать callback голоса, проверив длину.

    Raises:
        ValueError: если строка не влезает в лимит Telegram — лучше упасть
            на тесте, чем молча получить неработающую кнопку.
    """
    payload = f"{CB_CONFIRM_PREFIX}{date_iso}:{para}:{target_tg_id}"
    if len(payload.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback too long: {payload!r}")
    return payload


def vote_kb(group: str, date_iso: str, para: int,
            candidates: list[dict], votes: dict[int, int] | None = None,
            attested: set[int] | None = None) -> InlineKeyboardMarkup:
    """Кнопки голосования: фамилия со счётчиком, затем «Закрыть досрочно».

    Счётчик дублируется в подписи кнопки (как в макете ТЗ) и в тексте
    сообщения. Это не избыточно: клавиатуру в клиенте можно свернуть, а
    текст со счётчиками остаётся видимым.

    ФИО обрезается так, чтобы подпись со счётчиком уложилась в лимит
    Telegram: у длинной фамилии хвост отбрасывается, а счётчик сохраняется
    (он важнее для голосования).

    Args:
        group: группа (для совместимости вызова; в callback не попадает).
        date_iso: дата пары.
        para: номер пары.
        candidates: ``[{'tg_id', 'full_name'}]``.
        votes: ``{tg_id: количество голосов}``; None — счётчиков нет.
        attested: ``tg_id`` уже зачтённых.

    Returns:
        InlineKeyboardMarkup с кнопками кандидатов и закрытием.
    """
    counts = votes or {}
    done = attested or set()

    rows = []
    for person in candidates:
        tg_id = int(person["tg_id"])
        count = int(counts.get(tg_id, 0))
        word = plural_ru(count, "голос", "голоса", "голосов")
        suffix = f" — {count} {word}" + (" ✓" if tg_id in done else "")
        # «✅ » — 2 символа, суффикс со счётчиком, запас на обрезку.
        name_limit = max(4, MAX_BUTTON_TEXT - len("✅ ") - len(suffix))
        name = str(person["full_name"])[:name_limit]
        rows.append([InlineKeyboardButton(
            text=f"✅ {name}{suffix}",
            callback_data=confirm_callback(date_iso, para, tg_id),
        )])

    rows.append([InlineKeyboardButton(
        text=vtext.BTN_CLOSE,
        callback_data=f"{CB_CLOSE_PREFIX}{date_iso}:{para}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_vote_kb(lessons: list[dict], day: date) -> InlineKeyboardMarkup:
    """Список пар дня для запуска голосования (староста).

    Args:
        lessons: пары дня (``para_number``, ``subject``).
        day: дата пар.

    Returns:
        InlineKeyboardMarkup: кнопка на пару + «🏠 Меню».
    """
    from bot.attendance import keyboards as att_kb

    date_iso = day.isoformat()
    rows = [
        [InlineKeyboardButton(
            text=f"{int(lesson['para_number'])} пара · "
                 f"{str(lesson['subject'])[:36]}",
            callback_data=f"{CB_PICK_PREFIX}{date_iso}:"
                          f"{int(lesson['para_number'])}",
        )]
        for lesson in lessons
    ]
    rows.append([InlineKeyboardButton(text="🏠 Меню",
                                      callback_data=att_kb.CB_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def close_only_kb(date_iso: str, para: int) -> InlineKeyboardMarkup:
    """Только кнопка закрытия (для повторного запуска без кандидатов)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=vtext.BTN_CLOSE,
            callback_data=f"{CB_CLOSE_PREFIX}{date_iso}:{para}",
        )],
    ])


def vote_button_label(count: int, attested: bool) -> str:
    """Подпись кнопки-счётчика: «3 голоса ✓» (используется в тестах)."""
    word = plural_ru(count, "голос", "голоса", "голосов")
    return f"{count} {word}" + (" ✓" if attested else "")