"""Парсер расписания занятий из DOCX (24kst.ru).

python-docx намеренно НЕ используется: он не тянет вложенные таблицы с
vMerge. Читаем ``word/document.xml`` из zip напрямую через
``xml.etree.ElementTree``.

Реальная структура документа (проверено на файле 1 семестра 2026/2027,
76 таблиц групп):

- в ``body`` чередуются абзацы ``ГРУППА <имя> <N> курс`` и таблицы:
  каждая таблица относится к ближайшему предшествующему абзацу группы;
- сетка таблицы — 10 колонок::

    [0] № пары | [1] маркер чётности | [2..7] Пн..Сб | [8] маркер | [9] №

  в строке заголовка ``№`` имеет gridSpan=2, между ними шесть названий
  дней; колонки 8–9 — зеркальный дубль (игнорируются);
- каждая пара занимает две физические строки: «Чет» и «нечет»;
  vMerge=restart стоит на чётной строке, vMerge=continue — на нечётной;
  колонка-маркер чётности тоже может сливаться вниз (vMerge), поэтому
  чётность строки берётся из resolved-текста маркера;
- если у нечётной строки собственный текст пуст, а значение тянется
  сверху (vMerge=continue) — пара идёт каждую неделю (week_type='');
- содержимое ячейки дня разбито по абзацам: предмет (возможен перенос
  на несколько абзацев), затем строки «Фамилия /кабинет» (для подгрупп —
  несколько таких строк), разделители ``/`` и ``\\``, «вакансия»,
  кабинеты вида «Песочная 22», «О2-3», «спортзал».

При любой ошибке парсер НЕ падает: log.warning и пустой список —
вызывающий код использует последний кэш (правило 6).
"""

import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import NamedTuple

from bot.parsers.groups import normalize_group_name
from bot.parsers.teachers import TEACHERS, full_fio, is_known_surname

logger = logging.getLogger(__name__)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCUMENT_XML = "word/document.xml"

# Абзац-заголовок перед таблицей группы: «ГРУППА   26 С1  1 курс».
GROUP_RE = re.compile(r"ГРУППА\s+(.+?)\s+\d+\s*курс", re.IGNORECASE)

# Названия дней из шапки таблицы → day_of_week (ISO: понедельник = 1).
DAY_NAMES: dict[str, int] = {
    "понедельник": 1, "вторник": 2, "среда": 3,
    "четверг": 4, "пятница": 5, "суббота": 6,
    "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6,
}

# Значения week_type — ровно как в документе-источнике.
WEEK_TYPE_EVEN = "Чет"
WEEK_TYPE_ODD = "нечет"
WEEK_TYPE_ALWAYS = ""

# Слова, которые никогда не считаются фамилией преподавателя.
NON_TEACHER_WORDS = frozenset({"спортзал", "песочная", "мастерская", "политех"})

VACANCY_RE = re.compile(r"вакансия", re.IGNORECASE)
SURNAME_RE = re.compile(r"^[А-ЯЁ][а-яё]{2,}$")
INITIALS_RE = re.compile(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.")
SEGMENT_SPLIT_RE = re.compile(r"\s*[/\\]\s*")
PARA_RE = re.compile(r"\d+")


class _Cell(NamedTuple):
    """Ячейка таблицы, развёрнутая в сетку колонок."""

    own: tuple[str, ...]       # собственный текст (непустые абзацы)
    vmerge: str | None         # 'restart' | 'cont' | None
    resolved: tuple[str, ...]  # own либо значение, протянутое сверху (vMerge)
    merge_start: int = -1      # индекс строки, где началось слияние (vMerge)


def parse_docx(path: str | Path) -> list[dict]:
    """Распарсить DOCX расписания.

    Args:
        path: путь к файлу .docx.

    Returns:
        Список словарей с ключами: ``group_name``, ``day_of_week``
        (1..6, понедельник = 1), ``para_number`` (int), ``subject``,
        ``teacher``, ``room``, ``week_type`` ('' | 'Чет' | 'нечет').
        При любой ошибке — log.warning и пустой список (без исключений).
    """
    try:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read(DOCUMENT_XML))
        body = root.find(W + "body")
        if body is None:
            raise ValueError("в document.xml нет <w:body>")
        return _parse_body(body)
    except Exception as exc:
        logger.warning(
            "schedule parse failed",
            extra={"path": str(path), "error": repr(exc)},
        )
        return []


def _paragraph_texts(parent: ET.Element) -> tuple[str, ...]:
    """Непустые тексты абзацев внутри элемента (ячейки)."""
    texts = []
    for para in parent.findall(W + "p"):
        text = "".join(node.text or "" for node in para.iter(W + "t")).strip()
        if text:
            texts.append(text)
    return tuple(texts)


def _cell_props(tc: ET.Element) -> tuple[str | None, int]:
    """Прочитать vMerge ('restart'|'cont'|None) и gridSpan ячейки."""
    vmerge: str | None = None
    span = 1
    tc_pr = tc.find(W + "tcPr")
    if tc_pr is not None:
        vm = tc_pr.find(W + "vMerge")
        if vm is not None:
            vmerge = vm.get(W + "val") or "cont"
        gs = tc_pr.find(W + "gridSpan")
        if gs is not None:
            try:
                span = max(1, int(gs.get(W + "val", "1")))
            except ValueError:
                span = 1
    return vmerge, span



def _build_grid(tbl: ET.Element) -> list[list[_Cell | None]]:
    """Развернуть строки таблицы в единую сетку колонок с учётом vMerge/gridSpan.

    Возвращает список строк; в каждой — ячейки (или None, если колонка в
    этой строке отсутствует). ``resolved`` у vMerge=continue заполняется
    текстом ближайшей restart-ячейки сверху по той же колонке.
    """
    grid: list[list[_Cell | None]] = []
    for ri, tr in enumerate(tbl.findall(W + "tr")):
        row: list[_Cell | None] = []
        for tc in tr.findall(W + "tc"):
            vmerge, span = _cell_props(tc)
            cell = _Cell(own=_paragraph_texts(tc), vmerge=vmerge,
                         resolved=(), merge_start=ri if vmerge == "restart" else -1)
            for _ in range(span):
                row.append(cell)
        grid.append(row)

    # Протянуть значения vMerge=continue сверху вниз по колонкам.
    ncols = max((len(r) for r in grid), default=0)
    for col in range(ncols):
        carried: tuple[str, ...] = ()
        carried_start = -1
        for ri, row in enumerate(grid):
            cell = row[col] if col < len(row) else None
            if cell is None:
                continue
            if cell.vmerge == "cont":
                row[col] = cell._replace(resolved=carried,
                                         merge_start=carried_start)
            else:
                carried = cell.own
                carried_start = ri if cell.vmerge == "restart" else -1
                row[col] = cell._replace(resolved=cell.own)
    return grid


def _find_header(grid: list[list[_Cell | None]]) -> tuple[int, dict[int, int]] | None:
    """Найти строку заголовка с 6 днями недели.

    Дни могут занимать несколько колонок (gridSpan в заголовке), поэтому
    считаем уникальные названия дней и для каждого берём ПЕРВУЮ колонку.

    Returns:
        ``(индекс строки, {колонка: day_of_week})`` или None.
    """
    for ri, row in enumerate(grid):
        day_cols: dict[int, int] = {}
        seen_days: set[int] = set()
        for ci, cell in enumerate(row):
            if cell is None or len(cell.own) != 1:
                continue
            text = cell.own[0].strip().lower()
            if text in DAY_NAMES and DAY_NAMES[text] not in seen_days:
                seen_days.add(DAY_NAMES[text])
                day_cols[ci] = DAY_NAMES[text]
        if len(seen_days) == 6:
            return ri, day_cols
    return None


def _is_odd_marker(cell: _Cell | None) -> bool:
    """Колонка-маркер чётности: единственная строка «чет»/«нечет»."""
    if cell is None or len(cell.own) != 1:
        return False
    return cell.own[0].strip().lower() in {"чет", "чёт", "нечет"}


def _find_odd_column(grid: list[list[_Cell | None]], header_ri: int,
                     day_cols: dict[int, int]) -> int:
    """Колонка-маркер чётности: колонка слева от дней с текстом «чет»/«нечет»."""
    first_day = min(day_cols) if day_cols else 2
    for col in range(first_day):
        markers = sum(1 for row in grid[header_ri + 1:]
                      if col < len(row) and _is_odd_marker(row[col]))
        if markers >= 2:
            return col
    return max(1, first_day - 1)


def _para_number_of(row: list[_Cell | None]) -> int | None:
    """Номер пары из колонки 0 (с учётом vMerge), иначе None."""
    if not row:
        return None
    first = row[0]
    if first is None:
        return None
    for text in (first.resolved or first.own):
        match = PARA_RE.search(text)
        if match:
            return int(match.group(0))
    return None


def _marker_to_week_type(text: str) -> str:
    """Текст маркера чётности → 'Чет' | 'нечет'."""
    low = text.strip().lower()
    if low.startswith("не"):
        return WEEK_TYPE_ODD
    return WEEK_TYPE_EVEN


def _row_block_info(
    data_rows: list[list[_Cell | None]], odd_col: int
) -> list[tuple[int | None, str, int]]:
    """Для каждой строки данных: (номер пары, маркер чётности, id блока).

    Блок чётности открывается строкой, у которой маркер имеет собственный
    текст (в т.ч. vMerge=restart); строки с vMerge=continue в маркере
    относятся к предыдущему блоку. id блока нужен, чтобы отличить слияние
    ячейки внутри одного блока (пара конкретной чётности) от слияния через
    границу блоков (еженедельная пара).
    """
    infos: list[tuple[int | None, str, int]] = []
    block_id = -1
    last_marker = WEEK_TYPE_EVEN
    for row in data_rows:
        para = _para_number_of(row)
        marker_cell = row[odd_col] if odd_col < len(row) else None
        own = marker_cell.own if marker_cell is not None else ()
        if own:
            block_id += 1
            last_marker = _marker_to_week_type(own[0])
        infos.append((para, last_marker, block_id))
    return infos

def _segment_is_teacher_start(segment: str) -> bool:
    """Сегмент похож на НАЧАЛО «хвоста»: фамилия из справочника / вакансия / инициалы."""
    if VACANCY_RE.search(segment):
        return True
    if INITIALS_RE.search(segment):
        return True
    for surname in TEACHERS:
        if re.search(rf"\b{surname}\b", segment):
            return True
    return False


def _classify_tail_segment(segment: str) -> str:
    """Классифицировать сегмент хвоста: 'teacher' | 'room' | 'vacancy'."""
    if VACANCY_RE.search(segment):
        return "vacancy"
    if INITIALS_RE.search(segment):
        return "teacher"
    for surname in TEACHERS:
        if re.search(rf"\b{surname}\b", segment):
            return "teacher"
    first_word = segment.split(maxsplit=1)[0].lower() if segment else ""
    if first_word in NON_TEACHER_WORDS:
        return "room"
    if re.search(r"\d", segment):
        return "room"
    if SURNAME_RE.match(segment):
        return "teacher"
    return "room"


def _warn_unknown_surname(token: str, context: dict | None) -> None:
    """WARNING о фамилии, которой нет в справочнике (решение владельца).

    Алерт администратору намеренно НЕ отправляется — слишком шумно, достаточно
    лога. Фамилия используется как есть, без нормализации.

    Args:
        token: найденный кандидат в фамилии.
        context: ``{'group': ..., 'day': ..., 'para': ...}`` для сообщения.
    """
    ctx = context or {}
    group = ctx.get("group")
    day = ctx.get("day")
    para = ctx.get("para")
    logger.warning(
        f"неизвестная фамилия {token!r}, контекст: "
        f"группа {group}, день {day}, пара {para}",
        extra={"surname": token, "group": group, "day": day, "para": para},
    )


def parse_day_cell(paragraphs: Sequence[str],
                   context: dict | None = None) -> dict:
    """Разобрать ячейку дня недели на предмет / преподавателя / кабинет.

    Формат ячейки (по абзацам, проверено на реальном документе):
    сначала название предмета (может переноситься на несколько абзацев),
    затем «хвост» — строки вида «Фамилия /кабинет» (для подгрупп несколько
    пар фамилия+кабинет), разделители ``/`` и ``\\``, «вакансия», кабинеты
    «Песочная 22», «О2-3», «спортзал». Граница предмета — первый сегмент,
    содержащий фамилию из :data:`TEACHERS`, «вакансию» или инициалы.

    Args:
        paragraphs: непустые тексты абзацев ячейки.

    Returns:
        ``{'subject': str, 'teacher': str, 'room': str}`` — teacher/room
        через запятую при нескольких подгруппах; ФИО нормализуется через
        :func:`full_fio`.
    """
    # 1. Все абзацы → поток сегментов (сплит по '/' и '\\').
    segments: list[str] = []
    for para in paragraphs:
        for seg in SEGMENT_SPLIT_RE.split(para):
            seg = seg.strip(" ,;")
            if seg:
                segments.append(seg)

    # 2. Граница: первый «преподавательский» сегмент.
    tail_start = next(
        (i for i, seg in enumerate(segments) if _segment_is_teacher_start(seg)),
        len(segments),
    )
    subject = " ".join(segments[:tail_start]).strip()

    # 3. Хвост: фамилии (нормализуем через full_fio) и кабинеты, дедупликация.
    teachers: list[str] = []
    rooms: list[str] = []
    for seg in segments[tail_start:]:
        kind = _classify_tail_segment(seg)
        if kind == "vacancy":
            if "вакансия" not in teachers:
                teachers.append("вакансия")
            continue
        if kind == "teacher":
            token = seg
            match = INITIALS_RE.search(seg)
            if match:
                token = seg[: match.end()].strip()
            else:
                for surname in TEACHERS:
                    found = re.search(rf"\b{surname}\b", seg)
                    if found:
                        token = found.group(0)
                        break
                else:
                    m = SURNAME_RE.match(seg)
                    if m:
                        token = m.group(0)
                        # Фамилия не из справочника: пропускаем как есть, но
                        # пишем WARNING с контекстом (решение владельца).
                        if not is_known_surname(token):
                            _warn_unknown_surname(token, context)
            token = full_fio(token)
            if token not in teachers:
                teachers.append(token)
        else:
            if seg not in rooms:
                rooms.append(seg)

    return {
        "subject": subject,
        "teacher": ", ".join(teachers),
        "room": ", ".join(rooms),
    }

def _parse_table(tbl: ET.Element, group_name: str) -> list[dict]:
    """Разобрать одну таблицу группы; при проблемах — log.warning и [].

    Модель: цепочка vMerge (restart → continue...) — это одна визуальная
    ячейка, занятие эмитится ОДИН раз. week_type определяется по тому,
    какие блоки чётности («Чет»/«нечет») пересекает ячейка:

    - restart-строка и continue-строки находятся в разных блоках,
      own-текст continue-строк пуст → пара каждую неделю ('');
    - цепочка целиком внутри одного блока → week_type этого блока;
    - у continue-строки есть СОБСТВЕННЫЙ текст → это самостоятельное
      занятие своей строки (встречается в реальных документах).
    """
    grid = _build_grid(tbl)
    header = _find_header(grid)
    if header is None:
        logger.warning(
            "schedule table skipped: header not found",
            extra={"group": group_name},
        )
        return []
    header_ri, day_cols = header
    odd_col = _find_odd_column(grid, header_ri, day_cols)

    data_rows = grid[header_ri + 1:]
    infos = _row_block_info(data_rows, odd_col)

    lessons: list[dict] = []
    for row_idx, (row, (para_number, row_marker, block_id)) in enumerate(
            zip(data_rows, infos)):
        if para_number is None:
            continue  # служебная строка без номера пары
        for col, day in day_cols.items():
            cell = row[col] if col < len(row) else None
            if cell is None:
                continue
            if cell.own:
                paragraphs = cell.own
                if cell.vmerge == "restart" and cell.merge_start >= 0:
                    # Начало слияния: проверим, тянется ли оно в другой блок.
                    week_type = _merged_week_type(
                        data_rows, infos, col, row_idx, block_id,
                    )
                else:
                    week_type = row_marker
            elif cell.vmerge == "cont":
                # Пустой own — строка «живёт» за счёт ячейки сверху;
                # занятие уже эмитировано (или будет) из restart-строки.
                continue
            else:
                continue  # пустая ячейка
            parsed = parse_day_cell(
                paragraphs,
                context={
                    "group": group_name,
                    "day": day,
                    "para": para_number,
                },
            )
            if not parsed["subject"] and not parsed["teacher"]:
                continue
            lessons.append({
                "group_name": group_name,
                "day_of_week": day,
                "para_number": para_number,
                "subject": parsed["subject"],
                "teacher": parsed["teacher"],
                "room": parsed["room"],
                "week_type": week_type,
            })
    return lessons


def _merged_week_type(
    data_rows: list[list[_Cell | None]],
    infos: list[tuple[int | None, str, int]],
    col: int,
    row_idx: int,
    block_id: int,
) -> str:
    """week_type для restart-ячейки: '' если слияние пересекает блоки чётности.

    Смотрит вниз по колонке, пока идут vMerge=continue с пустым own.
    Если среди них есть строка другого блока чётности — пара еженедельная.
    """
    week_type = infos[row_idx][1]
    r = row_idx + 1
    while r < len(data_rows):
        cell = data_rows[r][col] if col < len(data_rows[r]) else None
        if cell is None or cell.vmerge != "cont":
            break
        if cell.own:
            break  # самостоятельное занятие следующей строки, не наше слияние
        if infos[r][2] != block_id:
            return WEEK_TYPE_ALWAYS
        r += 1
    return week_type


def _parse_body(body: ET.Element) -> list[dict]:
    """Пройти по body: абзац «ГРУППА X N КУРС» → следующая таблица этой группы."""
    lessons: list[dict] = []
    current_group: str | None = None
    for el in body:
        if el.tag == W + "p":
            text = "".join(node.text or "" for node in el.iter(W + "t")).strip()
            match = GROUP_RE.search(text)
            if match:
                # Общая нормализация: «26 С1» → «26С1», «О26КАД» → «026КАД».
                current_group = normalize_group_name(match.group(1))
        elif el.tag == W + "tbl":
            if current_group is None:
                logger.warning("schedule table skipped: no group heading before it")
                continue
            lessons.extend(_parse_table(el, current_group))
    return lessons

