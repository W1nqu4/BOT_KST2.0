"""Тексты голосования за посещаемость.

Все тексты — HTML (``parse_mode="HTML"``), поэтому ФИО и предметы
экранируются здесь же: вызывающему коду не нужно об этом помнить.
"""
from __future__ import annotations

from datetime import date
from html import escape

from bot.utils.text import plural_ru

# Подписи кнопок.
BTN_VOTE = "📣 Запустить голосование"
BTN_VOTE_START = "📣 Голосование"
BTN_CLOSE = "🔒 Закрыть досрочно"

# Заголовок и подсказки.
VOTE_TITLE = "🗳 <b>Голосование за посещаемость</b>"
VOTE_CLOSED_TITLE = "🗳 <b>Голосование закрыто</b>"
VOTE_HINT = "Нажми на фамилию, чтобы подтвердить, что студент был:"

# Ответы (alert).
ALERT_VOTED = "✅ Голос учтён"
ALERT_ALREADY_VOTED = "Ты уже подтвердил этого студента"
ALERT_VOTE_CLOSED = "Голосование уже закрыто"
ALERT_NOT_IN_GROUP = "Ты не в группе этой пары"
ALERT_ATTESTED = "✅ Зачтён по голосованию"

# Сообщения старосте.
NO_MISSING = "🤔 Все отметились, голосовать не за кого."
NO_CHAT = (
    "🤔 У группы нет привязанного чата — голосование запустить негде.\n\n"
    "<i>Попроси администратора привязать чат командой /setup.</i>"
)
NO_LESSON = "🤔 У группы нет такой пары в этот день."
VOTE_EXISTS = "🤔 По этой паре уже есть голосование."
VOTE_FORMAT = (
    "Формат: <code>/vote дата пара</code>\n"
    "Например: <code>/vote сегодня 3</code> или "
    "<code>/vote 29.09 2</code>"
)
VOTE_DENIED = "⛔ Голосование запускает староста или зам."

# Подпись для пустого списка кандидатов.
NO_CANDIDATES = "<i>Не за кого голосовать — все отметились.</i>"


def vote_title(day: date, para: int, subject: str) -> str:
    """Шапка сообщения: «3 пара · 30.09 · История»."""
    head = f"{para} пара · {day.strftime('%d.%m')}"
    if subject:
        head += f" · {escape(subject)}"
    return head


def threshold_line(threshold: int, total: int) -> str:
    """Строка «Порог зачёта: 3 голоса из 5»."""
    word = plural_ru(threshold, "голос", "голоса", "голосов")
    return f"Порог зачёта: <b>{threshold}</b> {word} из {total}"


def render_vote_poll(group: str, day: date, para: int, subject: str,
                     candidates: list[dict], votes: dict[int, int],
                     threshold: int, attested: set[int],
                     total: int | None = None,
                     closed: bool = False) -> str:
    """Сообщение голосования со счётчиками голосов.

    Счётчики живут в тексте, а не только в кнопках: в чате видно динамику,
    даже если Telegram не перерисовал клавиатуру (или её свернули).

    Args:
        group: группа.
        day: дата пары.
        para: номер пары.
        subject: название предмета.
        candidates: ``[{'tg_id', 'full_name'}]`` — за кого можно голосовать.
        votes: ``{tg_id: количество голосов}``.
        threshold: порог зачёта.
        attested: ``tg_id`` уже зачтённых.
        total: размер группы (для строки порога); None — по числу кандидатов.
        closed: закрытое ли голосование.

    Returns:
        HTML-текст сообщения.
    """
    lines = [VOTE_CLOSED_TITLE if closed else VOTE_TITLE,
             vote_title(day, para, subject),
             f"🎓 {escape(group)}", ""]

    if not candidates:
        lines.append(NO_CANDIDATES)
    else:
        lines.append(VOTE_HINT)
        lines.append("")
        for person in candidates:
            tg_id = int(person["tg_id"])
            count = int(votes.get(tg_id, 0))
            marks = f"{count} {plural_ru(count, 'голос', 'голоса', 'голосов')}"
            check = " ✓" if tg_id in attested else ""
            lines.append(
                f"{escape(str(person['full_name']))} — {marks}{check}"
            )

    lines.append("")
    size = total if total is not None else len(candidates)
    lines.append(threshold_line(threshold, size))
    return "\n".join(lines)


def render_vote_final(group: str, day: date, para: int, subject: str,
                      attested: list[dict],
                      not_attested: list[dict]) -> str:
    """Итог закрытого голосования.

    Args:
        group: группа.
        day: дата пары.
        para: номер пары.
        subject: предмет.
        attested: зачтённые по голосованию.
        not_attested: не набравшие порог.

    Returns:
        HTML-текст сообщения.
    """
    lines = [VOTE_CLOSED_TITLE, vote_title(day, para, subject),
             f"🎓 {escape(group)}", ""]

    lines.append("✅ <b>Зачтены:</b>")
    if attested:
        lines.extend(f"  • {escape(str(person['full_name']))}"
                     for person in attested)
    else:
        lines.append("  <i>никого</i>")

    lines.extend(["", "⚠️ <b>Не набрали голосов:</b>"])
    if not_attested:
        lines.extend(f"  • {escape(str(person['full_name']))}"
                     for person in not_attested)
    else:
        lines.append("  <i>все набрали</i>")

    lines.extend(["", "Староста, проверь вручную: /mark"])
    return "\n".join(lines)


def attested_notice(full_name: str, count: int) -> str:
    """Объявление в чат: «✅ Иванов И.И. зачтён по голосованию (3 голоса)»."""
    word = plural_ru(count, "голос", "голоса", "голосов")
    return (f"✅ {escape(full_name)} зачтён по голосованию "
            f"({count} {word})")


def pick_para_prompt(day: date) -> str:
    """Заголовок выбора пары для голосования."""
    return (f"🗳 <b>Запуск голосования</b>\n"
            f"📅 {day.strftime('%d.%m.%Y')}\n\nВыбери пару:")


NO_LESSONS_TODAY = "🤔 У группы сегодня нет пар по расписанию."