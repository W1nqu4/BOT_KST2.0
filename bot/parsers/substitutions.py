"""Парсер листа замен из HTML (24kst.ru).

Реальная структура страницы (проверено на live-странице от 28.09.2026):

- дата стоит в шапке: ``<strong><u>на  28  СЕНТЯБРЯ 2026 (ПОНЕДЕЛЬНИК)</u></strong>``
  (внутри — неразрывные пробелы ``\\xa0``), год берётся из той же строки;
- таблица замен — 5 колонок::

    Группа | Пара | Предмет по расписанию | ЗАМЕНА | Аудитория

  её ищем по заголовку «ЗАМЕНА» (а не по позиции — на странице есть ещё
  таблица телефонов доверия и таблица времени звонков);
- ``Пара`` — целое 1..5; строка без номера пары (служебная) пропускается;
- разделитель «замены нет» — длинное тире: ``——————`` (U+2014), иногда с
  ASCII-дефисом на конце. Только из тире (или пусто) ⇒ ``is_cancelled``;
- содержимое ячейки «ЗАМЕНА» бывает устроено так:

    «ОД.07 Математика /Кудрявцева»           — слэш перед фамилией;
    «ОД.01 Русский язык /Рукосуева/»         — лишний слэш на конце;
    «ОП.12 … проектирования Белясина / Степень» — фамилия приклеена к предмету;
    «МДК01.01 … изделия⏎Евдокимова /»        — фамилия на новой строке;
    «СГ.02 … Головань/ подгруппа»            — служебный хвост;
    «ОД.08 Информатика /Ващенко /вся группа л» — служебный хвост;
    «МДК 01.01 … ⏎Вараск / Наумкина»         — две подгруппы: два преподавателя;
    «————————————————»                        — замены нет.

- переносы строк в HTML — настоящие разделители: ``<br>`` и ``<p>`` внутри
  ``<td>`` (в кабинетах так записывают второй кабинет: «406А/⏎405А»).

Формат кабинетов (проверено на обоих источниках):

- «307А», «П-4», «спортзал» — как есть;
- «Песочная 22, 210/221» ⇒ два кабинета «Песочная 22, 210» и «Песочная 22, 221»;
- «Песочная 22, 208, 230» ⇒ «Песочная 22, 208» и «Песочная 22, 230»;
- «314Б/⏎306Б» ⇒ «314Б / 306Б» (два кабинета подгрупп);
- «404/1Б» — неоднозначный случай: по правилу «/» без пробелов разделяет,
  поэтому выходят два кабинета («404» и «1Б»), как и в DOCX, где то же место
  записано через запятую («404, 1Б»). Возможно, это «корпус 1Б, кабинет 404».
  Вопрос вынесен владельцу проекта — см. README.

При любой ошибке парсер НЕ падает: log.warning и пустой список — вызывающий
код использует последний кэш (правило 6).
"""

import logging
import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import Tag

from bot.parsers.groups import normalize_group_name
from bot.parsers.teachers import TEACHERS, full_fio, is_known_surname

logger = logging.getLogger(__name__)

# Русские месяцы в родительном падеже (как в шапке листа замен).
MONTHS: dict[str, int] = {
    "ЯНВАРЯ": 1, "ФЕВРАЛЯ": 2, "МАРТА": 3, "АПРЕЛЯ": 4,
    "МАЯ": 5, "ИЮНЯ": 6, "ИЮЛЯ": 7, "АВГУСТА": 8,
    "СЕНТЯБРЯ": 9, "ОКТЯБРЯ": 10, "НОЯБРЯ": 11, "ДЕКАБРЯ": 12,
}

# «на 28 СЕНТЯБРЯ 2026» (пробелы могут быть неразрывными).
DATE_RE = re.compile(r"на\s+(\d{1,2})\s+([А-ЯЁ]+)\s+(\d{4})", re.IGNORECASE)

# «замены нет»: строка только из длинных тире, дефисов и пробелов.
DIVIDER_RE = re.compile(r"^[—–−\-\s]+$")

# Служебные хвосты, которые не являются ни предметом, ни фамилией.
# «Самостоятельная работа» здесь НЕ срезается: она фиксируется флагом
# is_self_study (см. SELF_STUDY_RE), а текст предмета остаётся полным.
SERVICE_TAIL_RE = re.compile(
    r"(?:вся\s+группа(?:\s+л)?|\d+\s*подгрупп[аы]?|подгрупп[аы]?|подгр\.?)",
    re.IGNORECASE,
)

# «вакансия 3» → «вакансия» (номер вакансии не несёт смысла).
VACANCY_RE = re.compile(r"ваканси[яй](?:\s+\d+)?", re.IGNORECASE)

# Служебные слова-кабинеты (не фамилии).
ROOM_WORDS = frozenset({"спортзал", "мастерская", "политех", "бассейн"})

# Фамилия: с заглавной буквы, дальше строчные («Витюгова»).
SURNAME_RE = re.compile(r"^[А-ЯЁ][а-яё]{2,}$")

# Инициалы: «Соломатина К.А.», «К.А.».
INITIALS_RE = re.compile(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.")

# Текст содержит признак самостоятельной работы.
SELF_STUDY_RE = re.compile(r"самостоятельн", re.IGNORECASE)

# Разделители сегментов внутри ячейки: слэш, обратный слэш, перенос строки.
SEGMENT_SPLIT_RE = re.compile(r"\s*[/\\\r\n]+\s*")

# Кабинет-адрес: «Песочная 22», «Песочная, 22, 110», «Песочная22, 110».
ADDRESS_ROOM_RE = re.compile(r"^(Песочная)\s*,?\s*(\d+)\s*,?\s*(.+)$", re.IGNORECASE)

# «404/1Б» — ОДИН кабинет («кабинет 404, корпус 1Б»). Решение владельца.
#
# Отличие от «401Б/308Б» (это ДВА кабинета) — в первой части нет буквы корпуса,
# там голый номер. Поэтому в шаблоне первая часть строго `\d+` без буквы:
# готовый шаблон владельца `\d+[А-Яа-я]?/\d+[А-Яа-я]+` матчил бы и
# «401Б/308Б», «314Б/306Б», «406А/405А», а это подтверждённо два кабинета
# (в DOCX те же места записаны через запятую: «401Б, 308Б» — 4 раза,
# «314Б, 306Б» — 6 раз, «406А, 405А» — 10 раз).
CORPUS_SLASH_ROOM_RE = re.compile(
    r"(?<![\dА-Яа-я])(\d{1,4})/(\d{1,4}[А-Яа-я]{1,3})(?![\dА-Яа-я])"
)

# Временный маркер спрятанного кабинета «кабинет/корпус» (без слэша).
CORPUS_PLACEHOLDER_RE = re.compile(r"ROOM(\d+)")

# Заголовки таблицы замен → имена колонок.
HEADER_ALIASES: dict[str, str] = {
    "группа": "group",
    "пара": "para",
    "предмет по расписанию": "old",
    "замена": "new",
    "аудитория": "room",
}

# Поля результата parse_html (см. docstring функции).
EXPECTED_FIELDS = (
    "group", "date_iso", "para", "old_subject", "new_subject",
    "teacher", "room", "is_cancelled", "is_self_study",
)


def parse_html(path: str | Path) -> list[dict]:
    """Распарсить лист замен из HTML-файла.

    Args:
        path: путь к файлу .html со страницей «Лист замен».

    Returns:
        Список словарей с ключами :data:`EXPECTED_FIELDS`:
        ``group`` (нормализованное имя), ``date_iso`` (YYYY-MM-DD),
        ``para`` (int), ``old_subject``, ``new_subject``, ``teacher``,
        ``room``, ``is_cancelled``, ``is_self_study``.

        При любой ошибке (нет файла, нет даты, нет таблицы) —
        ``log.warning`` и пустой список: вызывающий код берёт последний
        успешный кэш (правило 6).
    """
    try:
        html = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("substitutions read failed",
                       extra={"path": str(path), "error": repr(exc)})
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
        date_iso = _extract_date(soup)
        if not date_iso:
            logger.warning("substitutions: date not found in header",
                           extra={"path": str(path)})
            return []
        table = _find_replace_table(soup)
        if table is None:
            logger.warning("substitutions: replace table not found",
                           extra={"path": str(path)})
            return []
        columns = _table_columns(table)
        if columns is None:
            logger.warning("substitutions: unexpected table header",
                           extra={"path": str(path)})
            return []
    except Exception as exc:
        logger.warning("substitutions parse failed",
                       extra={"path": str(path), "error": repr(exc)})
        return []

    rows = _parse_rows(table, columns, date_iso)
    logger.info("substitutions parsed",
                extra={"date": date_iso, "count": len(rows)})
    return rows


def _extract_date(soup: BeautifulSoup) -> str:
    """Дата из шапки «на  28  СЕНТЯБРЯ 2026 (ПОНЕДЕЛЬНИК)» → ``2026-09-28``.

    Год берётся из той же строки. Пустая строка — если дата не найдена или
    месяц/день некорректны.
    """
    text = _normalize_space(soup.get_text(" "))
    match = DATE_RE.search(text)
    if match is None:
        return ""
    day, month_name, year = match.groups()
    month = MONTHS.get(month_name.upper())
    if month is None:
        return ""
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return ""


def _find_replace_table(soup: BeautifulSoup) -> Tag | None:
    """Найти таблицу замен по заголовку «ЗАМЕНА».

    На странице есть и другие таблицы (телефоны доверия, время звонков),
    поэтому ищем именно по заголовку колонки, а не по позиции.
    """
    for table in soup.find_all("table"):
        texts = [c.get_text(" ", strip=True).upper()
                 for c in table.find_all(["td", "th"])]
        if "ЗАМЕНА" in texts[:12]:
            return table
    return None


def _table_columns(table: Tag) -> dict[str, int] | None:
    """Сопоставить заголовки колонок с их индексами.

    Returns:
        ``{'group': 0, 'para': 1, 'old': 2, 'new': 3, 'room': 4}`` или None,
        если обязательные колонки не найдены.
    """
    rows = table.find_all("tr")
    if not rows:
        return None
    columns: dict[str, int] = {}
    for index, cell in enumerate(rows[0].find_all(["td", "th"])):
        title = _normalize_space(cell.get_text(" ", strip=True)).lower()
        name = HEADER_ALIASES.get(title)
        if name is not None and name not in columns:
            columns[name] = index
    required = {"group", "para", "old", "new"}
    if not required <= set(columns):
        return None
    return columns
def _parse_rows(table: Tag, columns: dict[str, int],
                date_iso: str) -> list[dict]:
    """Разобрать строки таблицы замен (кроме заголовка)."""
    rows: list[dict] = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= columns["new"]:
            continue
        group = normalize_group_name(_cell_text(cells[columns["group"]]))
        if not group:
            continue
        para = _parse_para(_cell_text(cells[columns["para"]]))
        if para is None:
            continue  # служебная строка без номера пары

        old_subject = _normalize_space(
            _cell_text(cells[columns["old"]]).replace("\n", " "))
        new_raw = _cell_text(cells[columns["new"]])
        room_raw = (_cell_text(cells[columns["room"]])
                    if "room" in columns and len(cells) > columns["room"]
                    else "")

        parsed = _split_teacher_room(
            new_raw,
            context={"group": group, "date": date_iso, "para": para},
        )

        # Кабинет: из «ЗАМЕНА» (если там был) плюс отдельная колонка.
        rooms: list[str] = []
        for candidate in (parsed["room"], room_raw):
            for room in split_rooms(candidate):
                if room not in rooms:
                    rooms.append(room)

        is_cancelled = _is_divider(new_raw)
        rows.append({
            "group": group,
            "date_iso": date_iso,
            "para": para,
            "old_subject": old_subject,
            "new_subject": parsed["subject"],
            "teacher": parsed["teacher"],
            "room": " / ".join(rooms),
            "is_cancelled": is_cancelled,
            "is_self_study": bool(
                SELF_STUDY_RE.search(new_raw) and not is_cancelled
            ),
        })
    return rows


def _cell_text(cell: Tag) -> str:
    """Текст ячейки с сохранением переносов строк.

    В HTML перенос внутри ``<td>`` записан тегами ``<br>`` и ``<p>`` — это
    настоящие разделители (второй кабинет, фамилия на новой строке), поэтому
    их превращаем в ``\\n``, а неразрывные пробелы — в обычные.
    """
    for br in cell.find_all("br"):
        br.replace_with("\n")
    for para in cell.find_all("p"):
        para.append("\n")
    text = cell.get_text("", strip=False).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _normalize_space(text: str) -> str:
    """Свернуть любые пробельные последовательности (в т.ч. NBSP) в один пробел."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _parse_para(text: str) -> int | None:
    """Номер пары из ячейки «Пара»; None — если это не число 1..9."""
    match = re.search(r"\d+", text)
    if match is None:
        return None
    para = int(match.group(0))
    return para if 1 <= para <= 9 else None


def _is_divider(text: str) -> bool:
    """Ячейка «ЗАМЕНА» — разделитель «замены нет»?

    True для пустой строки и для строки из длинных тире (в т.ч. с ASCII-дефисом
    на конце: «———————————————————-»).
    """
    stripped = text.strip()
    return not stripped or DIVIDER_RE.match(stripped) is not None
def _warn_unknown_surname(token: str, context: dict | None) -> None:
    """WARNING о фамилии, которой нет в справочнике (решение владельца).

    Алерт администратору намеренно НЕ отправляется — слишком шумно, достаточно
    лога. Фамилия при этом используется как есть, без нормализации.

    Args:
        token: найденный кандидат в фамилии.
        context: ``{'group': ..., 'date': ..., 'para': ...}`` для сообщения.
    """
    extra: dict[str, object] = {"surname": token}
    if context:
        extra["group"] = context.get("group")
        extra["date"] = context.get("date")
        extra["para"] = context.get("para")
    logger.warning(
        f"неизвестная фамилия {token!r}, контекст: "
        f"группа {extra.get('group')}, день {extra.get('date')}, "
        f"пара {extra.get('para')}",
        extra=extra,
    )


def _split_teacher_room(text: str, context: dict | None = None) -> dict:
    """Разобрать ячейку «ЗАМЕНА» на предмет / преподавателей / кабинеты.

    Формат ячейки (проверено на live-странице):

    - предмет и хвост разделены слэшем, обратным слэшем или переносом строки;
    - фамилия может быть приклеена к предмету пробелом («…проектирования Белясина»);
    - два преподавателя — две подгруппы («Вараск / Наумкина») ⇒ « / »;
    - служебные хвосты («вся группа», «подгруппа», «подгр.») отбрасываются;
    - «вакансия 3» ⇒ «вакансия»;
    - преподаватель нормализуется через :func:`full_fio`.

    Args:
        text: сырой текст ячейки «ЗАМЕНА».

    Returns:
        ``{'subject': str, 'teacher': str, 'room': str}``. Для разделителя
        «замены нет» все три поля пустые (флаг ``is_cancelled`` ставит
        вызывающий код через :func:`_is_divider`).
    """
    if _is_divider(text):
        return {"subject": "", "teacher": "", "room": ""}

    flat = _normalize_space(text)

    # 1. Служебные хвосты убираем ДО разбиения: «/вся группа л» — не часть.
    flat = SERVICE_TAIL_RE.sub(" ", flat)
    flat = VACANCY_RE.sub("вакансия", flat)
    flat = _normalize_space(flat)

    # 2. Разбиение по слэшам/переносам на сегменты (порядок сохраняем).
    #    Точку НЕ срезаем: она часть инициалов («Соломатина К.А.»), иначе
    #    INITIALS_RE перестаёт распознавать фамилию с инициалами.
    segments = [s.strip(" ,;") for s in SEGMENT_SPLIT_RE.split(flat)]
    segments = [s for s in segments if s]

    # 3. Классифицируем сегменты: teacher / room / текст предмета.
    #    Фамилия может быть приклеена к предмету пробелом без слэша
    #    («ОД.07 Математика Вовчек»), поэтому teacher-сегмент дополнительно
    #    расщепляем на предмет + преподавателей.
    subject_parts: list[str] = []
    teachers: list[str] = []
    rooms: list[str] = []
    for segment in segments:
        kind = _classify_segment(segment)
        if kind == "room":
            rooms.extend(split_rooms(segment))
        elif kind == "teacher":
            head, found, found_rooms = _split_at_first_surname(segment, context)
            if head:
                subject_parts.append(head)
            for token in found:
                if token not in teachers:
                    teachers.append(token)
            for room in found_rooms:
                if room not in rooms:
                    rooms.append(room)
        else:
            subject_parts.append(segment)

    subject = _normalize_space(" ".join(subject_parts))

    # 4. Кабинет, приклеенный к предмету («СГ.04 … культура спортзал»).
    subject, tail_rooms = _split_trailing_rooms(subject)
    for room in tail_rooms:
        if room not in rooms:
            rooms.append(room)

    return {
        "subject": subject,
        "teacher": " / ".join(teachers),
        "room": " / ".join(rooms),
    }


def _split_at_first_surname(segment: str,
                            context: dict | None = None) -> tuple[str, list[str], list[str]]:
    """Разделить сегмент на текст предмета, фамилии и кабинеты.

    Нужно потому, что в источнике фамилия часто приклеена к предмету пробелом
    без слэша: «ОД.07 Математика Вовчек», «СГ.01 История России /Лютов».

    Args:
        segment: сегмент ячейки «ЗАМЕНА».
        context: ``{'group': ..., 'date': ..., 'para': ...}`` — для WARNING
            о фамилии, которой нет в справочнике.

    Returns:
        ``(текст_до_первой_фамилии, [ФИО, ...], [кабинет, ...])``.
    """
    if "вакансия" in segment.lower():
        head = VACANCY_RE.split(segment)[0].strip(" ,;.")
        return head, ["вакансия"], []

    match = INITIALS_RE.search(segment)
    if match is not None:
        # Инициалы: фамилия стоит прямо перед ними («Соломатина К.А.»).
        before = segment[: match.start()].rstrip()
        surname = before.split()[-1] if before.split() else ""
        # Слово перед инициалами — гарантированно фамилия: если её нет в
        # справочнике, пишем WARNING с контекстом (решение владельца).
        if surname and not is_known_surname(surname):
            _warn_unknown_surname(surname, context)
        head = before[: len(before) - len(surname)].strip(" ,;.")
        head, head_rooms = _split_trailing_rooms(head)
        token = full_fio(f"{surname} {match.group(0)}".strip())
        return head, [token], head_rooms

    best: tuple[int, int, str] | None = None  # (start, end, фамилия)
    for surname in TEACHERS:
        found = re.search(rf"\b{surname}\b", segment)
        if found is None:
            continue
        # Из двух вхождений выбираем самое раннее; при равенстве — более длинное.
        if best is None or (found.start(), -len(surname)) < (best[0], -len(best[2])):
            best = (found.start(), found.end(), surname)

    if best is None:
        return segment, [], []

    start, end, surname = best
    head = segment[:start].strip(" ,;.")
    tail = segment[end:].strip(" ,;.")

    extra: list[str] = []
    for other in re.findall(r"[А-ЯЁ][а-яё]{2,}", tail):
        if other in TEACHERS and other != surname:
            extra.append(full_fio(other))
    head, head_rooms = _split_trailing_rooms(head)
    _, tail_rooms = _split_trailing_rooms(tail)
    return head, [full_fio(surname), *extra], [*head_rooms, *tail_rooms]


def _classify_segment(segment: str) -> str:
    """Классифицировать сегмент: 'teacher' | 'room' | 'subject'.

    Приоритет: «вакансия» и фамилия из справочника → преподаватель; короткий
    сегмент с цифрой или служебное слово → кабинет; иначе текст предмета.
    Порядок важен: «Песочная 22» содержит цифру, но фамилий там нет, а
    «МДК 01.01 …» — длинный текст предмета (см. :func:`_looks_like_room`).
    """
    low = segment.lower()
    if "вакансия" in low:
        return "teacher"
    if INITIALS_RE.search(segment):
        return "teacher"
    for surname in TEACHERS:
        if re.search(rf"\b{surname}\b", segment):
            return "teacher"
    if _looks_like_room(segment):
        return "room"
    return "subject"


def _looks_like_room(segment: str) -> bool:
    """Сегмент похож на кабинет: «307А», «П-4», «404/1Б», «спортзал».

    Кабинет — либо служебное слово («спортзал»), либо адрес («Песочная 22, 210»),
    либо КОРОТКИЕ токены-номера («307А», «О2-3», «221»). Проверка по токенам, а
    не по «есть цифра»: иначе «ОД.07 Математика» и «МДК 01.01 …» — предметы с
    номерами — попадали бы в аудиторию.
    """
    if segment.lower() in ROOM_WORDS:
        return True
    if ADDRESS_ROOM_RE.match(segment) is not None:
        return True
    tokens = [t for t in re.split(r"[,\s]+", segment) if t]
    return bool(tokens) and all(_is_room_token(t) for t in tokens)


def split_rooms(text: str) -> list[str]:
    """Разобрать текст кабинетов в список отдельных кабинетов.

    Правила (проверены на реальных данных обоих источников):

    - «Песочная 22, 210/221» ⇒ ``['Песочная 22, 210', 'Песочная 22, 221']``;
    - «Песочная 22, 208, 230» ⇒ ``['Песочная 22, 208', 'Песочная 22, 230']``;
    - «Песочная, 22, 110» и «Песочная22, 110» ⇒ ``['Песочная 22, 110']``;
    - «406А/405А», «406А/ 405А», «314Б 306Б» ⇒ по кабинету на каждый номер;
    - «404/1Б» ⇒ ``['404/1Б']`` — ОДИН кабинет («404, корпус 1Б»), решение
      владельца проекта; первая часть без буквы корпуса (см.
      :data:`CORPUS_SLASH_ROOM_RE`);
    - «307А», «П-4», «спортзал» ⇒ как есть.

    Args:
        text: текст кабинета (одна ячейка или сегмент).

    Returns:
        Список кабинетов в порядке появления, без повторов.
    """
    flat = _normalize_space(text)
    if not flat:
        return []

    # «404/1Б» — один кабинет: прячем пару от разбиения по слэшам.
    flat, corpora = join_corpus_rooms(flat)

    rooms: list[str] = []
    address_prefix = ""  # «Песочная 22» — переносится на голые номера после «/».
    for raw_piece in SEGMENT_SPLIT_RE.split(flat):
        piece = raw_piece.strip(" ,;.")
        if not piece:
            continue
        # Возврат спрятанного кабинета «кабинет/корпус».
        corpus = CORPUS_PLACEHOLDER_RE.fullmatch(piece)
        if corpus is not None:
            room = corpora[int(corpus.group(1))]
            if room not in rooms:
                rooms.append(room)
            continue
        match = ADDRESS_ROOM_RE.match(piece)
        if match is not None:
            address_prefix = f"{match.group(1).capitalize()} {match.group(2)}"
            for room in _rooms_from_piece(piece):
                if room not in rooms:
                    rooms.append(room)
            continue
        # «Песочная 22, 210/221» → после слэша остался голый номер «221»:
        # дополняем его адресом из предыдущего куска.
        if address_prefix and _is_room_token(piece):
            room = f"{address_prefix}, {piece}"
            if room not in rooms:
                rooms.append(room)
            continue
        for room in _rooms_from_piece(piece):
            if room not in rooms:
                rooms.append(room)
    return rooms


def _rooms_from_piece(piece: str) -> list[str]:
    """Разбить один кусок текста (без слэшей) на кабинеты по запятым/пробелам."""
    match = ADDRESS_ROOM_RE.match(piece)
    if match is not None:
        # «Песочная 22, 210, 221» — адрес один, кабинетов может быть несколько.
        prefix = f"{match.group(1).capitalize()} {match.group(2)}"
        return [f"{prefix}, {num}"
                for num in re.split(r"[,\s]+", match.group(3)) if num]

    # «404Б 306Б» (без запятой) — два коротких номера подряд.
    tokens = re.split(r"[,\s]+", piece)
    tokens = [t for t in tokens if t]
    if len(tokens) > 1 and all(_is_room_token(t) for t in tokens):
        return tokens
    return [piece]


def join_corpus_rooms(text: str) -> tuple[str, list[str]]:
    """Склеить «кабинет/корпус» в один кабинет: «404/1Б» ⇒ «404/1Б».

    Решение владельца проекта: «404/1Б» — это ОДИН кабинет («кабинет 404,
    корпус 1Б»), а не два. Правило применяется ДО разбиения по слэшам, иначе
    слэш разрежет пару на «404» и «1Б».

    Args:
        text: текст кабинета.

    Returns:
        ``(текст с заменёнными парами, [склеенные кабинеты])``. Пары заменяются
        на placeholder-номер, чтобы слэш не участвовал в разбиении, а сами
        кабинеты возвращаются отдельно для подстановки на место placeholder.
    """
    rooms: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        room = f"{match.group(1)}/{match.group(2)}"
        rooms.append(room)
        return f" ROOM{len(rooms) - 1} "

    return CORPUS_SLASH_ROOM_RE.sub(_replace, text), rooms


def _is_room_token(token: str) -> bool:
    """Токен похож на номер кабинета: «307А», «1Б», «П-4», «221»."""
    return re.fullmatch(r"(?:[А-ЯЁа-яё]?\d+[А-ЯЁа-яё]{0,2}|\d+[А-ЯЁа-яё]?|П-\d+)", token) is not None


def _split_trailing_rooms(subject: str) -> tuple[str, list[str]]:
    """Отделить кабинет, приклеенный к тексту предмета без слэша.

    «СГ.04 Физическая культура спортзал» → предмет + ``['спортзал']``.
    Срабатывает только на служебные слова-кабинеты, чтобы «ОД.09 Физическая
    культура» осталась предметом целиком.

    Returns:
        ``(предмет, кабинеты)``.
    """
    words = subject.split()
    if words and words[-1].lower() in ROOM_WORDS:
        return " ".join(words[:-1]), [words[-1]]
    return subject, []