"""Тексты сообщений посещаемости (этап 1).

Вынесены из ``handlers.py``, чтобы обработчики оставались про логику, а не
про формулировки. Все тексты — с HTML-разметкой (сообщения отправляются с
``parse_mode="HTML"``), поэтому подставляемые данные экранируются вызывающим
кодом через ``html.escape``.
"""
from __future__ import annotations

from bot.attendance.service import role_label
from bot.keyboards.reply import BTN_CANCEL

# --- приветствие ---

GREETING = (
    "👋 Привет! Я бот расписания КСТ.\n\n"
    "Что я умею:\n"
    "📆 Расписание с заменами и чёт/нечет\n"
    "📝 Личные дедлайны с напоминаниями\n"
    "📆 Подписка на календарь прямо в телефоне\n"
    "📊 Посещаемость группы (скоро)\n\n"
    "Начни с «📊 Моя группа» — введи код от старосты.\n"
    "Если ты староста — создай группу."
)

# --- запросы ввода (FSM) ---

ASK_CODE = "Введи код приглашения от старосты (6 цифр):"
ASK_GROUP_NAME = "Введи название группы (например, 25КАД):"
ASK_FULL_NAME = "Введи своё ФИО как в журнале (например, Абрамчик С.Г.):"

ASK_CODE_AGAIN = (
    "Код — это 6 цифр. Пришли его ещё раз, например <code>482931</code>."
)

ASK_FULL_NAME_AGAIN = (
    "Не получилось разобрать ФИО 🤔\n"
    "Напиши фамилию и инициалы, например <b>Абрамчик С.Г.</b>, "
    "или полностью: <b>Абрамчик Светлана Геннадьевна</b>."
)

# --- регистрация ---

NOT_REGISTERED = (
    "🤷 Ты пока не в группе.\n\n"
    "Введи код приглашения от старосты или создай группу, если ты староста."
)

CODE_NOT_FOUND = (
    "❌ Код не найден. Проверь цифры и попробуй ещё раз — "
    "или попроси у старосты новый."
)

ALREADY_IN_GROUP = (
    "🤷 Ты уже состоишь в группе <b>{group}</b>.\n"
    "Один студент — одна группа. Если нужно перейти в другую, "
    "напиши администратору."
)

GROUP_ALREADY_CREATED = (
    "🤔 Группа <b>{group}</b> уже создана.\n"
    "Попроси код приглашения у старосты."
)

GROUP_NOT_FOUND = (
    "🤔 Группы <code>{group}</code> нет в расписании КСТ.\n\n"
    "Проверь название и попробуй ещё раз: например, <b>25КАД</b>."
)

GROUP_SUGGESTIONS_HEADER = "🤔 Группы <code>{group}</code> нет в расписании."

GROUP_SUGGESTIONS_HINT = "Похожие группы — нажми, чтобы выбрать:"


def joined_ok(group: str) -> str:
    """Подтверждение регистрации студента."""
    return (
        f"✅ Ты в группе <b>{group}</b>.\n\n"
        "Теперь доступны «📊 Моя группа» и список группы."
    )


def starosta_created(group: str, code: str) -> str:
    """Карточка старосты после создания группы."""
    return (
        f"✅ Ты староста группы <b>{group}</b>.\n\n"
        f"🔑 Код приглашения: <code>{code}</code>\n\n"
        "Кидай его в чат группы — студенты введут /start → "
        "«Моя группа» → «Ввести код»."
    )


def new_code(code: str) -> str:
    """Новый код приглашения после перегенерации."""
    return (
        "🔄 <b>Код обновлён</b>\n\n"
        f"🔑 Новый код: <code>{code}</code>\n\n"
        "Старый код больше не работает — разошли новый."
    )


# --- карточка группы ---

def group_card(group: str, count: int, role_label: str) -> str:
    """Карточка группы для студента/старосты."""
    role_line = f"🎭 Роль: <b>{role_label}</b>\n" if role_label else ""
    return (
        f"📊 <b>Моя группа</b>\n\n"
        f"🎓 Группа: <b>{group}</b>\n"
        f"{role_line}"
        f"👥 Студентов: <b>{count}</b>"
    )


def group_list(group: str, lines: list[str]) -> str:
    """Список студентов группы (строки уже экранированы и с ролями)."""
    body = "\n".join(lines) if lines else "<i>Пока никого нет.</i>"
    return (
        f"📋 <b>Список группы {group}</b>\n\n"
        f"{body}"
    )


def group_management(group: str, code: str) -> str:
    """Экран управления группой (только староста)."""
    return (
        f"⚙️ <b>Управление группой {group}</b>\n\n"
        f"🔑 Текущий код: <code>{code}</code>"
    )


def deputy_assigned(name: str) -> str:
    """Подтверждение назначения зама."""
    return (
        f"✅ Зам: <b>{name}</b>\n\n"
        "Он сможет отмечать посещаемость вместе с тобой."
    )


DEPUTY_NOBODY = (
    "🤔 Некого назначить: в группе пока нет студентов кроме тебя."
)

MANAGE_DENIED = "⛔ Управлять группой может только староста."
NOT_YOUR_GROUP = "⛔ Это не твоя группа."
STUB_ALERT = "🚧 Скоро — этап 2"
MENU_HOME = "🏠 <b>Главное меню</b> — используй кнопки ниже."


# --- админ-панель (этап 1: роль администратора) ---

# Ответ посторонним: не подтверждаем существование команды.
ADMIN_NOT_FOUND = "🤔 Команда не найдена. Посмотри /help — там всё, что умеет бот."

ASK_ADMIN_GROUP = "Введи название группы (например, 25КАД):"
ASK_ADMIN_DELETE_GROUP = (
    "🗑 <b>Удаление группы</b>\n\n"
    "Введи название группы. Удалятся и группа, и все её студенты."
)
ASK_ADMIN_BROADCAST = (
    "📢 <b>Рассылка</b>\n\n"
    "Пришли текст сообщения. HTML разрешён (<code>b</code>, "
    "<code>i</code>, <code>code</code>), но без ссылок.\n\n"
    f"Отменить — кнопкой «{BTN_CANCEL}»."
)

ADMIN_FIND_NOT_FOUND = "🤔 Группы <code>{group}</code> нет среди созданных."
ADMIN_DELETE_NOT_FOUND = "🤔 Группы <code>{group}</code> нет среди созданных."
ADMIN_BROADCAST_EMPTY = "🤔 В группах пока нет студентов — рассылать некому."
ADMIN_BROADCAST_CANCELLED = "↩️ Рассылка отменена."


def admin_panel(stats: dict) -> str:
    """Текст админ-панели со счётчиками."""
    return (
        "👑 <b>Админ-панель</b>\n\n"
        f"📊 Групп: <b>{stats['groups']}</b>\n"
        f"👥 Студентов: <b>{stats['students']}</b>\n"
        f"🔑 Активных кодов: <b>{stats['active_codes']}</b>"
    )


def admin_group_card(item: dict) -> str:
    """Карточка группы для админа (данные уже экранированы вызывающим)."""
    lines = [
        f"🔍 <b>Группа {item['group_name']}</b>",
        "",
        f"👥 Студентов: <b>{item['students_count']}</b>",
    ]
    if item.get("starosta_name"):
        lines.append(f"⭐ Староста: <b>{item['starosta_name']}</b>")
    if item.get("invite_code"):
        lines.append(f"🔑 Код: <code>{item['invite_code']}</code>")
    if item.get("created_at"):
        lines.append(f"🕐 Создана: {item['created_at']}")
    return "\n".join(lines)


def admin_delete_confirm(group_name: str, students: int) -> str:
    """Предпросмотр удаления группы."""
    return (
        f"🗑 <b>Удалить группу {group_name}?</b>\n\n"
        f"Вместе с ней удалятся <b>{students}</b> студ. "
        "Это действие необратимо."
    )


def admin_delete_done(group_name: str, students: int) -> str:
    """Отчёт об удалении группы."""
    return (
        f"✅ Группа <b>{group_name}</b> удалена.\n"
        f"Удалено студентов: <b>{students}</b>."
    )


def admin_broadcast_preview(text: str, recipients: int) -> str:
    """Предпросмотр рассылки со счётчиком получателей."""
    return (
        "📢 <b>Предпросмотр рассылки</b>\n\n"
        "———\n"
        f"{text}\n"
        "———\n\n"
        f"Получателей: <b>{recipients}</b>"
    )


def admin_broadcast_report(result: dict) -> str:
    """Отчёт о рассылке."""
    return (
        "📢 <b>Рассылка завершена</b>\n\n"
        f"✅ Отправлено: <b>{result['sent']}</b>\n"
        f"❌ Ошибок: <b>{result['failed']}</b>\n"
        f"👥 Всего: <b>{result['total']}</b>"
    )


def starosta_assigned(group: str, name: str) -> str:
    """Подтверждение назначения старосты админом."""
    return (
        f"✅ Староста группы <b>{group}</b>: <b>{name}</b>\n\n"
        "Прежний староста стал обычным студентом."
    )


def admin_groups_list(groups: list[dict], limit: int = 20) -> tuple[str, int]:
    """Список групп для админа.

    Args:
        groups: сводки из ``admin_service.list_all_groups_stats``.
        limit: сколько групп показать.

    Returns:
        ``(текст, сколько_скрыто)``.
    """
    from html import escape

    shown = groups[:limit]
    hidden = max(0, len(groups) - len(shown))

    lines = [f"📋 <b>Группы ({len(groups)})</b>", ""]
    if not shown:
        lines.append("<i>Пока ни одной группы.</i>")

    for item in shown:
        row = f"• <b>{escape(str(item['group_name']))}</b>"
        count = int(item.get("students_count") or 0)
        row += (f" — {count} студ." if count else " — 0 студентов")
        starosta = str(item.get("starosta_name") or "")
        if starosta:
            row += f", староста {escape(starosta)}"
        code = str(item.get("invite_code") or "")
        if code:
            row += f", код <code>{escape(code)}</code>"
        lines.append(row)

    if hidden:
        lines.extend(["", f"<i>и ещё {hidden}</i>"])
    return "\n".join(lines), hidden


def admin_students_list(students: list[dict],
                        limit: int = 50) -> tuple[str, int]:
    """Список студентов, сгруппированный по группам.

    Args:
        students: результат ``admin_service.list_all_students``.
        limit: сколько строк показать.

    Returns:
        ``(текст, сколько_скрыто)``.
    """
    from html import escape

    shown = students[:limit]
    hidden = max(0, len(students) - len(shown))

    lines = [f"👥 <b>Студенты ({len(students)})</b>", ""]
    if not shown:
        lines.append("<i>Пока никого.</i>")

    current_group = None
    for student in shown:
        group_name = str(student["group_name"])
        if group_name != current_group:
            if current_group is not None:
                lines.append("")
            lines.append(f"<b>{escape(group_name)}</b>")
            current_group = group_name
        role = role_label(str(student.get("role") or ""))
        row = (f"   {escape(str(student['full_name']))} · "
               f"<code>{student['tg_id']}</code>")
        if role:
            row += f" — {role}"
        lines.append(row)

    if hidden:
        lines.extend(["", f"<i>и ещё {hidden}</i>"])
    return "\n".join(lines), hidden