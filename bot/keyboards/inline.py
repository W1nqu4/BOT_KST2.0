"""Inline-клавиатуры: навигация по расписанию и выбор группы.

Callback-данные держим короткими и стабильными: они попадают в кнопки,
которые уже отправлены пользователю, поэтому менять формат нельзя без
поддержки старых вариантов.
"""

from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Callback-данные навигации по дням недели.
CB_NAV_PREFIX = "sched:nav:"      # sched:nav:0 .. sched:nav:5
CB_TODAY = "sched:today"
CB_PICK_DAY = "sched:pickday"
CB_CHANGE_GROUP = "menu:changegroup"

# Тексты дней недели (1..7, понедельник = 1).
DAY_NAMES = {
    1: "Понедельник", 2: "Вторник", 3: "Среда",
    4: "Четверг", 5: "Пятница", 6: "Суббота", 7: "Воскресенье",
}


def day_name(weekday: int) -> str:
    """Название дня недели по ISO-номеру (1..7)."""
    return DAY_NAMES.get(weekday, "")


def week_nav_kb(current: date, direction: int) -> InlineKeyboardMarkup:
    """Навигация по дням недели: [◀️] [🔄 Сегодня] [▶️] и выбор дня.

    Args:
        current: дата, которая показана сейчас (для смещения).
        direction: не используется в данных кнопок (смещение считается
            в обработчике от текущей даты), оставлен для читаемости вызова.

    Returns:
        InlineKeyboardMarkup с навигацией.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️", callback_data=f"{CB_NAV_PREFIX}-1"),
            InlineKeyboardButton(text="🔄 Сегодня", callback_data=CB_TODAY),
            InlineKeyboardButton(text="▶️", callback_data=f"{CB_NAV_PREFIX}+1"),
        ],
        [
            InlineKeyboardButton(text="📆 Выбрать день", callback_data=CB_PICK_DAY),
        ],
    ])


def pick_day_kb(today: date) -> InlineKeyboardMarkup:
    """Выбор дня недели: по кнопке на каждый день (Пн..Сб).

    Returns:
        InlineKeyboardMarkup с шестью днями в двух рядах.
    """
    buttons = [
        InlineKeyboardButton(text=day_name(i), callback_data=f"{CB_NAV_PREFIX}{i}")
        for i in range(1, 7)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:3],
        buttons[3:],
        [InlineKeyboardButton(text="🔄 Сегодня", callback_data=CB_TODAY)],
    ])


def group_suggestions_kb(groups: list[str]) -> InlineKeyboardMarkup:
    """Кнопки с похожими группами (когда введённой группы нет в расписании).

    Args:
        groups: до 3 ближайших названий.

    Returns:
        InlineKeyboardMarkup; пустой список даёт клавиатуру без рядов.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"group:pick:{name}")]
        for name in groups
    ])


def dashboard_kb() -> InlineKeyboardMarkup:
    """Кнопки под дашбордом: перейти к расписанию или сменить группу."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data=CB_TODAY),
            InlineKeyboardButton(text="📆 Расписание", callback_data=CB_PICK_DAY),
        ],
        [
            InlineKeyboardButton(text="🔄 Сменить группу", callback_data=CB_CHANGE_GROUP),
        ],
    ])


# --- Календарь и дедлайны (шаг 8) ---

# Названия месяцев в именительном падеже (для шапки календаря).
MONTH_NAMES = (
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

# Заголовки дней недели: сетка начинается с понедельника.
WEEKDAY_HEADERS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

# Заглушка для дней вне текущего месяца.
CALENDAR_EMPTY = "·"


def month_title(year: int, month: int) -> str:
    """«Сентябрь 2026» для шапки календаря."""
    name = MONTH_NAMES[month] if 1 <= month <= 12 else ""
    return f"{name} {year}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Сдвинуть месяц на ``delta`` (с переходом через год)."""
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def build_calendar_kb(year: int, month: int,
                      prefix: str = "dl") -> InlineKeyboardMarkup:
    """Календарь месяца: сетка 7 столбцов, Пн..Вс.

    Особенности:

    - номер месяца в шапке («Сентябрь 2026»);
    - дни месяца → ``{prefix}:cal:pick:{ISO}``;
    - сегодня выделен скобками (``[15]``);
    - дни вне месяца — ``·`` с callback ``{prefix}:cal:ignore``;
    - кнопка «✏️ Вручную» для ввода даты текстом.

    Args:
        year: год.
        month: месяц (1..12).
        prefix: префикс callback-данных.

    Returns:
        InlineKeyboardMarkup сетки календаря.
    """
    import calendar as _calendar
    from datetime import date as _date

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=month_title(year, month),
                              callback_data=f"{prefix}:cal:ignore")],
        [InlineKeyboardButton(text=day, callback_data=f"{prefix}:cal:ignore")
         for day in WEEKDAY_HEADERS],
    ]

    today = _date.today()
    # monthcalendar: недели с понедельника, 0 — день вне месяца.
    for week in _calendar.monthcalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(
                    text=CALENDAR_EMPTY, callback_data=f"{prefix}:cal:ignore",
                ))
                continue
            label = str(day)
            if (year, month, day) == (today.year, today.month, today.day):
                label = f"[{day}]"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"{prefix}:cal:pick:{_date(year, month, day).isoformat()}",
            ))
        rows.append(row)

    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    rows.append([
        InlineKeyboardButton(text="◀️",
                             callback_data=f"{prefix}:cal:nav:{prev_year}-{prev_month:02d}"),
        InlineKeyboardButton(text="✏️ Вручную",
                             callback_data=f"{prefix}:cal:manual"),
        InlineKeyboardButton(text="▶️",
                             callback_data=f"{prefix}:cal:nav:{next_year}-{next_month:02d}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def deadline_list_kb(items: list[dict]) -> InlineKeyboardMarkup:
    """Список дедлайнов: кнопка удаления на каждый + «Добавить» и «Меню».

    Args:
        items: активные дедлайны (словари с ``id`` и ``task``).

    Returns:
        InlineKeyboardMarkup: ряды удаления, затем [➕ Добавить] [🏠 Меню].
    """
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        label = (item.get("task") or "без названия")[:28]
        rows.append([InlineKeyboardButton(
            text=f"❌ {label}", callback_data=f"dl:del:{item['id']}",
        )])
    rows.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="dl:add"),
        InlineKeyboardButton(text="🏠 Меню", callback_data=CB_TODAY),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deadline_type_kb() -> InlineKeyboardMarkup:
    """Меню выбора привязки дедлайна."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📚 По предмету", callback_data="dl:type:subject"),
            InlineKeyboardButton(text="🧑‍🏫 По преподавателю", callback_data="dl:type:teacher"),
        ],
        [
            InlineKeyboardButton(text="✏️ Произвольный", callback_data="dl:type:custom"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="dl:list"),
        ],
    ])


def deadline_confirm_kb(deadline_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой созданного дедлайна."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"dl:del:{deadline_id}"),
            InlineKeyboardButton(text="🏠 Готово", callback_data="dl:list"),
        ],
    ])


def deadline_delete_confirm_kb(deadline_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить",
                                 callback_data=f"dl:del_confirmed:{deadline_id}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data="dl:list"),
        ],
    ])


def pick_from_list_kb(items: list[tuple[int, str]],
                      prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора из списка: ``[(индекс, подпись), ...]``.

    Args:
        items: пары (индекс, подпись) — индекс попадает в callback.
        prefix: ``dl:subj`` или ``dl:teach``.

    Returns:
        InlineKeyboardMarkup с кнопками выбора и «Назад».
    """
    rows = [
        [InlineKeyboardButton(text=label[:60], callback_data=f"{prefix}:{index}")]
        for index, label in items
    ]
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="dl:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def date_source_kb() -> InlineKeyboardMarkup:
    """Выбор способа указания даты."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Календарь", callback_data="dl:date:calendar"),
            InlineKeyboardButton(text="✏️ Вручную", callback_data="dl:date:manual"),
        ],
        [
            InlineKeyboardButton(text="⏭ Без даты", callback_data="dl:date:none"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="dl:list"),
        ],
    ])
