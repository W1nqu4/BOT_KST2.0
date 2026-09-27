"""Тесты парсера листа замен из HTML (bot.parsers.substitutions)."""

from pathlib import Path

import pytest

from bot.parsers.groups import normalize_group_name
from bot.parsers.substitutions import (
    EXPECTED_FIELDS,
    parse_html,
    split_rooms,
    _split_teacher_room,
)
from bot.parsers.teachers import full_fio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "sample_substitutions.html"


@pytest.fixture()
def rows() -> list[dict]:
    """Результат разбора фикстуры sample_substitutions.html."""
    return parse_html(FIXTURE)


# --- Разбор ячейки «ЗАМЕНА»: кейсы из ТЗ ---

def test_teacher_after_slash() -> None:
    """«ОД.12 Химия / Витюгова» → предмет + полное ФИО."""
    parsed = _split_teacher_room("ОД.12 Химия / Витюгова")
    assert parsed["subject"] == "ОД.12 Химия"
    assert parsed["teacher"] == "Витюгова Наталья Владимировна"


def test_subject_kept_when_no_old_subject() -> None:
    """«ВПР / Белясина» → предмет «ВПР» не теряется."""
    parsed = _split_teacher_room("ВПР / Белясина")
    assert parsed["subject"] == "ВПР"
    assert parsed["teacher"] == "Белясина Любовь Юрьевна"


def test_two_teachers_joined_by_slash() -> None:
    """«Шеломов/Степанова» → два преподавателя через ' / ' (две подгруппы)."""
    parsed = _split_teacher_room("ОД.01 Русский язык Шеломов/Степанова")
    assert parsed["subject"] == "ОД.01 Русский язык"
    assert parsed["teacher"] == (
        "Шеломов Эдуард Викторович / Степанова Ольга Юрьевна"
    )


def test_service_tail_подгр_removed() -> None:
    """«Наумкина / подгр.» → служебный хвост отброшен."""
    parsed = _split_teacher_room("Семинар / Наумкина / подгр.")
    assert parsed["teacher"] == "Наумкина Эллина Алексеевна"
    assert "подгр" not in parsed["teacher"]


def test_divider_means_cancelled() -> None:
    """Строка из длинных тире → пустой разбор (флаг ставит вызывающий код)."""
    parsed = _split_teacher_room("————————————————-")
    assert parsed == {"subject": "", "teacher": "", "room": ""}


def test_service_tail_vsya_gruppa_removed() -> None:
    """«/вся группа л» не попадает ни в предмет, ни в ФИО."""
    parsed = _split_teacher_room("ОД.08 Информатика /Ващенко /вся группа л")
    assert parsed["subject"] == "ОД.08 Информатика"
    assert parsed["teacher"] == "Ващенко Марина Юрьевна"


def test_surname_glued_to_subject() -> None:
    """Фамилия без слэша отделяется от предмета («…проектирования Белясина»)."""
    parsed = _split_teacher_room(
        "ОП.12 Системы автоматизированного проектирования Белясина / Степень"
    )
    assert parsed["subject"] == "ОП.12 Системы автоматизированного проектирования"
    assert parsed["teacher"] == (
        "Белясина Любовь Юрьевна / Степень Милана Николаевна"
    )


def test_newline_separator_and_trailing_slash() -> None:
    """Перенос строки разделяет предмет и ФИО; лишний слэш на конце не мешает."""
    parsed = _split_teacher_room(
        "МДК.03.01 тема 1.4 Документоведение в строительстве\nВиссарионова /"
    )
    assert parsed["subject"] == "МДК.03.01 тема 1.4 Документоведение в строительстве"
    assert parsed["teacher"] == "Виссарионова Анна Сергеевна"


def test_vacancy_number_collapsed() -> None:
    """«вакансия 3» → «вакансия»."""
    parsed = _split_teacher_room("ОП.03 Рисунок и живопись / вакансия 3")
    assert parsed["teacher"] == "вакансия"


def test_room_glued_to_surname() -> None:
    """«Донзаленко спортзал» → преподаватель и кабинет разделены."""
    parsed = _split_teacher_room("СГ.04 Физическая культура Донзаленко спортзал")
    assert parsed["subject"] == "СГ.04 Физическая культура"
    assert parsed["teacher"] == "Донзаленко Павел Дмитриевич"
    assert parsed["room"] == "спортзал"


def test_subject_with_mdk_number_is_not_room() -> None:
    """Номер МДК не уезжает в кабинет."""
    parsed = _split_teacher_room("МДК 01.01 Начальное архитектурное проектирование")
    assert parsed["subject"] == "МДК 01.01 Начальное архитектурное проектирование"
    assert parsed["room"] == ""


def test_self_study_text_kept_in_subject() -> None:
    """«Самостоятельная работа» остаётся в тексте — флаг ставится отдельно."""
    parsed = _split_teacher_room("ОП.05 Основы BIM-моделирования Самостоятельная работа")
    assert "Самостоятельная работа" in parsed["subject"]
# --- Кабинеты ---

def test_split_rooms_address_with_two_numbers() -> None:
    """«Песочная 22, 208, 230» → два полных адреса."""
    assert split_rooms("Песочная 22, 208, 230") == [
        "Песочная 22, 208", "Песочная 22, 230",
    ]


def test_split_rooms_address_slash_keeps_prefix() -> None:
    """«Песочная 22, 210/221» → префикс адреса переносится на второй номер."""
    assert split_rooms("Песочная 22, 210/221") == [
        "Песочная 22, 210", "Песочная 22, 221",
    ]


def test_split_rooms_address_without_space() -> None:
    """«Песочная22, 110» и «Песочная, 22, 110» → один кабинет."""
    assert split_rooms("Песочная22, 110") == ["Песочная 22, 110"]
    assert split_rooms("Песочная, 22, 110") == ["Песочная 22, 110"]


def test_split_rooms_slash_without_spaces() -> None:
    """«401Б/308Б» → два кабинета (в DOCX то же место через запятую)."""
    assert split_rooms("401Б/308Б") == ["401Б", "308Б"]


def test_split_rooms_corpus_slash_is_one_room() -> None:
    """«404/1Б» → ОДИН кабинет («404, корпус 1Б»). Решение владельца."""
    assert split_rooms("404/1Б") == ["404/1Б"]
    assert split_rooms("404/1Б/ 308Б") == ["404/1Б", "308Б"]


def test_split_rooms_address_slash_still_splits() -> None:
    """Адрес с двумя номерами по-прежнему даёт два кабинета."""
    assert split_rooms("Песочная 22, 210/221") == [
        "Песочная 22, 210", "Песочная 22, 221",
    ]


def test_split_rooms_plain_and_service() -> None:
    """Обычные кабинеты и служебные слова не меняются."""
    assert split_rooms("307А") == ["307А"]
    assert split_rooms("П-4") == ["П-4"]
    assert split_rooms("спортзал") == ["спортзал"]
    assert split_rooms("") == []


# --- Нормализация имён групп ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("26 С1", "26С1"),
    ("26С1", "26С1"),
    ("О26КАД", "026КАД"),
    ("026 КАД", "026КАД"),
    ("26МОСДР-1", "26МОСДР1"),
    ("25 КАД", "25КАД"),
    ("   ", ""),
])
def test_normalize_group_name(raw: str, expected: str) -> None:
    assert normalize_group_name(raw) == expected


def test_normalize_group_name_is_idempotent() -> None:
    """Повторная нормализация не меняет результат."""
    for raw in ("26 С1", "О26КАД", "26МОСДР-1"):
        once = normalize_group_name(raw)
        assert normalize_group_name(once) == once


# --- Полный разбор фикстуры ---

def test_fixture_parses_all_rows(rows: list[dict]) -> None:
    """10 строк данных: служебная строка без пары пропущена."""
    assert len(rows) == 10
    for row in rows:
        assert set(EXPECTED_FIELDS) <= set(row)
        assert row["date_iso"] == "2026-09-22"


def test_fixture_header_only_date_from_caption(rows: list[dict]) -> None:
    """Дата берётся из шапки, а не из таблицы; год — из той же строки."""
    assert {r["date_iso"] for r in rows} == {"2026-09-22"}


def test_fixture_skips_row_without_para(rows: list[dict]) -> None:
    """Строка «26ИМС1» без номера пары в результат не попадает."""
    assert all(r["group"] != "26ИМС1" for r in rows)
    assert all(r["para"] >= 1 for r in rows)


def test_fixture_group_normalized(rows: list[dict]) -> None:
    """«026 С» → «026С», «25 КАД» → «25КАД»."""
    groups = {r["group"] for r in rows}
    assert "026С" in groups
    assert "25КАД" in groups


def test_fixture_cancelled_row(rows: list[dict]) -> None:
    """Строка с тире — отмена: пустые ФИО и кабинет."""
    cancelled = [r for r in rows if r["is_cancelled"]]
    assert len(cancelled) == 1
    row = cancelled[0]
    assert row["group"] == "26МЭГ"
    assert row["old_subject"] == "ОД.07 Математика"
    assert row["new_subject"] == ""
    assert row["teacher"] == ""
    assert row["room"] == ""


def test_fixture_self_study_flag(rows: list[dict]) -> None:
    """«Самостоятельная работа» → is_self_study=True."""
    self_study = [r for r in rows if r["is_self_study"]]
    assert len(self_study) == 1
    assert self_study[0]["group"] == "026С"


def test_fixture_not_cancelled_rows_have_new_subject(rows: list[dict]) -> None:
    """У каждой не-отменённой строки есть текст замены; преподаватель —
    кроме строк самостоятельной работы (там его в источнике нет)."""
    for row in rows:
        if row["is_cancelled"]:
            continue
        assert row["new_subject"], row
        if not row["is_self_study"]:
            assert row["teacher"], row


def test_unknown_surname_kept_and_logged(caplog) -> None:
    """Фамилия не из справочника: пропускается как есть + WARNING с контекстом."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="bot.parsers.substitutions"):
        parsed = _split_teacher_room(
            "Семинар / Иванов К.А.",
            context={"group": "26КАД", "date": "2026-09-28", "para": 3},
        )
    assert parsed["teacher"] == "Иванов К.А."
    messages = [r.getMessage() for r in caplog.records]
    assert any("неизвестная фамилия" in m for m in messages)
    assert any("26КАД" in m and "2026-09-28" in m and "3" in m for m in messages)


def test_placeholder_fio_warns_once_per_session(caplog) -> None:
    """Пометка «(ФИО уточняется)» → WARNING ровно один раз за сессию."""
    import logging as _logging

    from bot.parsers import teachers

    teachers._WARNED_PLACEHOLDERS.clear()
    with caplog.at_level(_logging.WARNING, logger="bot.parsers.teachers"):
        for _ in range(3):
            value = full_fio("Вишнякова")
    assert "уточняется" in value  # значение отдаётся как есть, без выдумывания
    warnings = [r for r in caplog.records if "not filled yet" in r.getMessage()]
    assert len(warnings) == 1, f"ожидали один WARNING, получили {len(warnings)}"


def test_known_surname_no_unknown_warning(caplog) -> None:
    """Известная фамилия не провоцирует WARNING о неизвестной."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="bot.parsers.substitutions"):
        _split_teacher_room("Семинар / Соломатина К.А.")
    assert not any("неизвестная фамилия" in r.getMessage() for r in caplog.records)


def test_initials_with_dot_not_stripped() -> None:
    """Точка в инициалах не срезается — иначе не распознаётся фамилия."""
    parsed = _split_teacher_room("Семинар / Соломатина К.А.")
    assert parsed["teacher"] == "Соломатина Ксения Александровна"
    assert parsed["subject"] == "Семинар"


def test_fixture_teacher_and_room_from_both_cells(rows: list[dict]) -> None:
    """Кабинеты объединяются из ячейки «ЗАМЕНА» и колонки «Аудитория».

    В фикстуре у 26Р (пара 2) аудитория «404/1Б/⏎308Б»: «404/1Б» — один
    кабинет («404, корпус 1Б»), «308Б» — второй.
    """
    row = next(r for r in rows if r["group"] == "26Р" and r["para"] == 2)
    assert row["room"] == "404/1Б / 308Б"


def test_fixture_no_crash_on_noisy_tables() -> None:
    """Лишние таблицы (телефоны доверия) игнорируются по заголовку."""
    assert parse_html(FIXTURE), "таблица замен найдена несмотря на шум"


# --- Устойчивость к мусору: [] без исключений ---

def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_html(tmp_path / "no_such.html") == []


def test_garbage_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.html"
    bad.write_bytes(b"<html><body>no table here</body></html>")
    assert parse_html(bad) == []


def test_html_without_date_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "no_date.html"
    path.write_text(
        "<html><body><table><tr><td>Группа</td><td>Пара</td>"
        "<td>Предмет по расписанию</td><td>ЗАМЕНА</td><td>Аудитория</td></tr>"
        "<tr><td>26КАД</td><td>1</td><td>ОД.07</td><td>ОД.07 / Кудрявцева</td>"
        "<td>307А</td></tr></table></body></html>",
        encoding="utf-8",
    )
    assert parse_html(path) == []


def test_unknown_month_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad_month.html"
    path.write_text(
        "<html><body><p><strong><u>на 22 СМЕСЯЦА 2026</u></strong></p>"
        "<table><tr><td>Группа</td><td>Пара</td><td>Предмет по расписанию</td>"
        "<td>ЗАМЕНА</td><td>Аудитория</td></tr>"
        "<tr><td>26КАД</td><td>1</td><td>ОД.07</td><td>ОД.07 / Кудрявцева</td>"
        "<td>307А</td></tr></table></body></html>",
        encoding="utf-8",
    )
    assert parse_html(path) == []