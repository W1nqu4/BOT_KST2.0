"""Тесты парсера расписания из DOCX (bot.parsers.schedule)."""

import zipfile
from pathlib import Path

import pytest

from bot.parsers.schedule import parse_day_cell, parse_docx
from bot.parsers.teachers import full_fio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = PROJECT_ROOT / "data" / "sample_schedule.docx"

EXPECTED_FIELDS = {
    "group_name", "day_of_week", "para_number",
    "subject", "teacher", "room", "week_type",
}
ALLOWED_WEEK_TYPES = {"", "Чет", "нечет"}

DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")


# --- Сборка синтетического DOCX ---

def _p(text: str) -> str:
    """Абзац с одним текстовым run."""
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _tc(paragraphs: list[str], vmerge: str | None = None, span: int | None = None) -> str:
    """Ячейка таблицы: gridSpan ИЛИ vMerge ('restart'/'cont')."""
    props = ""
    if span:
        props = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>'
    elif vmerge == "restart":
        props = '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
    elif vmerge == "cont":
        props = "<w:tcPr><w:vMerge/></w:tcPr>"
    content = "".join(_p(t) for t in paragraphs) or "<w:p/>"
    return f"<w:tc>{props}{content}</w:tc>"


def _tr(*cells: str) -> str:
    """Строка таблицы."""
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def _header_row() -> str:
    """Шапка: №(span2) | 6 дней | №(span2) — как в реальном документе."""
    return _tr(_tc(["№"], span=2), *[_tc([d]) for d in DAYS], _tc(["№"], span=2))


def _row(num_cell: str, marker: list[str], monday_cell: str) -> str:
    """Строка данных: № | маркер | Пн | остальные дни пустые | маркер | №."""
    empty_cont = _tc([], vmerge="cont")
    return _tr(num_cell, _tc(marker), monday_cell,
               *[empty_cont for _ in range(5)], _tc(marker), num_cell)


def _make_docx(tmp_path: Path, body_xml: str, name: str = "schedule.docx") -> Path:
    """Собрать минимальный DOCX (zip с word/document.xml) из XML тела."""
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body_xml}</w:body></w:document>'
    )
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc)
    return path


@pytest.fixture()
def synthetic_docx(tmp_path: Path) -> Path:
    """DOCX одной группы 26С1 с тремя сценариями vMerge в понедельнике.

    Пара 1 — еженедельная (restart на «Чет», continue на «нечет»).
    Пара 2 — разные предметы по чётности (обе ячейки independent).
    Пара 3 — только нечётная неделя (нечет-ячейка restart со своим текстом).
    """
    math = ["ОД.07 Математика", "Грехова /308А"]
    lit = ["ОД.02 Литература", "Зубарева/ 213Б"]
    bio = ["ОД.13 Биология", "Мюллер / 215Б"]
    chem = ["ОД.12 Химия", "Витюгова / 313А"]

    rows = [
        _header_row(),
        # Пара 1: Математика каждую неделю.
        _row(_tc(["1"], vmerge="restart"), ["Чет"],
             _tc(math, vmerge="restart")),
        _row(_tc([], vmerge="cont"), ["нечет"],
             _tc([], vmerge="cont")),
        # Пара 2: Литература (Чет) / Биология (нечет).
        _row(_tc(["2"], vmerge="restart"), ["Чет"], _tc(lit)),
        _row(_tc([], vmerge="cont"), ["нечет"], _tc(bio)),
        # Пара 3: Химия только в нечет.
        _row(_tc(["3"], vmerge="restart"), ["Чет"],
             _tc([], vmerge="restart")),
        _row(_tc([], vmerge="cont"), ["нечет"],
             _tc(chem, vmerge="restart")),
    ]
    body = _p("1 семестр 2026 -2027") + _p("ГРУППА   26 С1  1 курс") + \
        "<w:tbl>" + "".join(rows) + "</w:tbl>"
    return _make_docx(tmp_path, body)



# --- Тесты на синтетическом файле ---

def test_synthetic_parses_all_three_scenarios(synthetic_docx: Path) -> None:
    lessons = parse_docx(synthetic_docx)
    assert len(lessons) == 4  # 1 еженедельная + 2 по чётности + 1 нечет-only

    by_week = {(i["para_number"], i["week_type"]): i for i in lessons}

    weekly = by_week[(1, "")]
    assert weekly["subject"] == "ОД.07 Математика"
    assert weekly["teacher"] == full_fio("Грехова")
    assert weekly["room"] == "308А"
    assert weekly["day_of_week"] == 1

    assert by_week[(2, "Чет")]["subject"] == "ОД.02 Литература"
    assert by_week[(2, "нечет")]["subject"] == "ОД.13 Биология"
    assert by_week[(3, "нечет")]["subject"] == "ОД.12 Химия"
    assert (3, "Чет") not in by_week


def test_synthetic_group_name_normalized(synthetic_docx: Path) -> None:
    lessons = parse_docx(synthetic_docx)
    assert lessons
    assert all(i["group_name"] == "26С1" for i in lessons)


def test_synthetic_no_exceptions_and_field_types(synthetic_docx: Path) -> None:
    for i in parse_docx(synthetic_docx):
        assert EXPECTED_FIELDS <= set(i)
        assert i["week_type"] in ALLOWED_WEEK_TYPES
        assert isinstance(i["para_number"], int)


# --- Тесты на реальном файле (data/sample_schedule.docx) ---

@pytest.mark.skipif(not SAMPLE.exists(), reason="нет data/sample_schedule.docx")
def test_real_sample_parses_without_errors() -> None:
    lessons = parse_docx(SAMPLE)
    assert lessons, "реальный документ распарсен непусто"
    for i in lessons:
        assert EXPECTED_FIELDS <= set(i)
        assert i["week_type"] in ALLOWED_WEEK_TYPES
        assert 1 <= i["day_of_week"] <= 6
        assert i["para_number"] >= 1
        assert isinstance(i["group_name"], str) and i["group_name"]
        assert i["subject"] or i["teacher"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="нет data/sample_schedule.docx")
def test_real_sample_covers_many_groups() -> None:
    lessons = parse_docx(SAMPLE)
    groups = {i["group_name"] for i in lessons}
    assert len(groups) >= 50, f"ожидали много групп, получили {len(groups)}"
    assert "26КАД" in groups


@pytest.mark.skipif(not SAMPLE.exists(), reason="нет data/sample_schedule.docx")
def test_real_sample_week_type_distribution() -> None:
    lessons = parse_docx(SAMPLE)
    types = {i["week_type"] for i in lessons}
    assert types <= ALLOWED_WEEK_TYPES
    # В реальном документе встречаются все три варианта.
    assert types == ALLOWED_WEEK_TYPES


# --- Юнит-тесты разбора ячейки дня ---

def test_parse_day_cell_simple() -> None:
    parsed = parse_day_cell(["ОД.07 Математика", "Грехова /308А"])
    assert parsed == {
        "subject": "ОД.07 Математика",
        "teacher": full_fio("Грехова"),
        "room": "308А",
    }


def test_parse_day_cell_multiline_subject_and_subgroups() -> None:
    parsed = parse_day_cell(
        ["ОП.02 Инженерная", "графика", "Ныркова/ 412Б", "Яцук / 306Б"]
    )
    assert parsed["subject"] == "ОП.02 Инженерная графика"
    assert parsed["teacher"] == f"{full_fio('Ныркова')}, {full_fio('Яцук')}"
    assert parsed["room"] == "412Б, 306Б"


def test_parse_day_cell_vacancy_and_address_room() -> None:
    parsed = parse_day_cell(
        ["МДК 02.01", "Строительное черчение",
         "Вакансия 3 / вакансия 1", "Песочная 22"]
    )
    assert parsed["subject"] == "МДК 02.01 Строительное черчение"
    assert parsed["teacher"] == "вакансия"
    assert parsed["room"] == "Песочная 22"


def test_parse_day_cell_backslash_separator_and_sport_hall() -> None:
    parsed = parse_day_cell(["ОД.09 Физическая культура", "Кузнецов\\спортзал"])
    assert parsed["subject"] == "ОД.09 Физическая культура"
    assert parsed["teacher"] == full_fio("Кузнецов")
    assert parsed["room"] == "спортзал"


def test_parse_day_cell_initials_normalized_via_full_fio() -> None:
    """Фамилия с инициалами нормализуется в полное ФИО из справочника."""
    parsed = parse_day_cell(["Семинар", "Соломатина К.А."])
    assert parsed["teacher"] == "Соломатина Ксения Александровна"
    assert full_fio("Соломатина") == "Соломатина Ксения Александровна"
    assert full_fio("Соломатина К.А.") == "Соломатина Ксения Александровна"


def test_parse_day_cell_subject_with_digit_not_cut() -> None:
    """«МДК 02.02» — цифры в предмете не должны уходить в кабинет."""
    parsed = parse_day_cell(
        ["МДК 02.02", "Объемно-пространственная композиция",
         "Баранова / Васильянская", "Песочная 22, 231"]
    )
    assert parsed["subject"] == "МДК 02.02 Объемно-пространственная композиция"
    assert parsed["teacher"] == f"{full_fio('Баранова')}, {full_fio('Васильянская')}"
    assert parsed["room"] == "Песочная 22, 231"


# --- Устойчивость к мусору: [] без исключений ---

def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_docx(tmp_path / "no_such_file.docx") == []


def test_garbage_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"this is not a zip file at all")
    assert parse_docx(bad) == []


def test_zip_without_document_xml_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/styles.xml", "<w:styles/>")
    assert parse_docx(path) == []


def test_docx_without_tables_returns_empty(tmp_path: Path) -> None:
    path = _make_docx(tmp_path, _p("ГРУППА 26КАД 1 курс"))
    assert parse_docx(path) == []

