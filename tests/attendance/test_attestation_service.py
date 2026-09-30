"""Тесты аттестации по посещаемости (Часть 4).

Проверяется чистая логика :mod:`bot.attendance.attestation_service` на
in-memory БД: порог, статусы, периоды, fallback предмета.
"""

from datetime import date
from pathlib import Path

import pytest

from bot.attendance import attestation_service as atts
from bot.attendance import service
from bot.config import MIN_ATTESTATION_LESSONS
from bot.db import get_connection, transaction
from bot.migrations import apply_migrations

GROUP = "25КАД"
OTHER_GROUP = "26КАД"
STAROSTA = 2001
STUDENT = 2002
OTHER_STUDENT = 2003

# День внутри месяца (не понедельник!): период текущего месяца считается
# «1 число → этот день», поэтому он должен накрывать все отметки тестов
# (они лежат между 1 и 8 октября).
PERIOD_DAY = date(2026, 10, 20)
SUBJECTS = {1: "История", 2: "Литература", 3: "Физика"}


def _add_lessons(c, group: str) -> None:
    """Расписание группы: три предмета в понедельник."""
    for para, subject in SUBJECTS.items():
        c.execute(
            "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
            " subject, teacher, room, week_type, updated_at)"
            " VALUES (?, 1, ?, ?, 'Тест Т.Т.', '', '', 'x')",
            (group, para, subject),
        )


def _mark(c, group: str, tg_id: int, day_iso: str, para: int, status: str,
          subject: str | None = None) -> None:
    """Вставить отметку (с предметом или без — для проверки fallback)."""
    c.execute(
        "INSERT INTO attendance (group_name, date_iso, para, tg_id, full_name,"
        " status, marked_by, marked_at, method, subject)"
        " VALUES (?, ?, ?, ?, 'Иванов И.И.', ?, ?, 'x', 'self', ?)",
        (group, day_iso, para, tg_id, status, tg_id, subject),
    )


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с группой, старостой и студентом."""
    c = get_connection(tmp_path / "attestation.db")
    apply_migrations(c)
    with transaction(c):
        _add_lessons(c, GROUP)
        _add_lessons(c, OTHER_GROUP)
        code = service.create_group(c, GROUP, STAROSTA, "Абрамчик С.Г.")
        service.join_group(c, STUDENT, code, "Иванов И.И.")
        service.join_group(c, OTHER_STUDENT, code, "Петров П.П.")
    yield c
    c.close()


def _summary(conn, tg_id: int = STUDENT, day: date = PERIOD_DAY) -> dict:
    """Сводка аттестации студента за месяц дня ``day``."""
    start, end = atts.period_for_month(day, 0)
    return atts.get_attestation_summary(conn, tg_id, GROUP, start, end)


# --- get_group_subjects ---

def test_get_group_subjects_unique_sorted(conn) -> None:
    """Предметы группы возвращаются уникальными и отсортированными."""
    with transaction(conn):
        # Дубль предмета в другой день — в списке он должен быть один раз.
        conn.execute(
            "INSERT INTO schedule_cache (group_name, day_of_week, para_number,"
            " subject, teacher, room, week_type, updated_at)"
            " VALUES (?, 3, 1, 'История', 'Т.Т.', '', '', 'x')", (GROUP,)
        )

    subjects = atts.get_group_subjects(conn, GROUP)
    assert subjects == ["История", "Литература", "Физика"]
    assert len(subjects) == len(set(subjects))


def test_get_group_subjects_unknown_group(conn) -> None:
    """У неизвестной группы предметов нет."""
    assert atts.get_group_subjects(conn, "НетТакой") == []


# --- get_attestation_summary: порог ---

def test_three_of_five_is_attested(conn) -> None:
    """3 зачтённые пары из 5 — предмет аттестован."""
    with transaction(conn):
        for day in ("2026-10-01", "2026-10-02", "2026-10-06"):
            _mark(conn, GROUP, STUDENT, day, 1, "present", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-07", 1, "absent", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-08", 1, "absent", "История")

    item = next(i for i in _summary(conn)["items"] if i["subject"] == "История")
    assert item["attended"] == 3
    assert item["total_lessons"] == 5
    assert item["is_attested"] is True
    assert item["need_more"] == 0


def test_two_of_five_not_attested(conn) -> None:
    """2 пары — не аттестован, нужно ещё 1."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-02", 1, "present", "История")

    item = next(i for i in _summary(conn)["items"] if i["subject"] == "История")
    assert item["is_attested"] is False
    assert item["need_more"] == 1


def test_zero_lessons_needs_threshold(conn) -> None:
    """0 пар — нужно ровно MIN_ATTESTATION_LESSONS."""
    item = next(i for i in _summary(conn)["items"] if i["subject"] == "Физика")
    assert item["attended"] == 0
    assert item["is_attested"] is False
    assert item["need_more"] == MIN_ATTESTATION_LESSONS == 3
# --- статусы ---

def test_late_counted(conn) -> None:
    """Опоздание идёт в зачёт аттестации."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-02", 1, "late", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-06", 1, "late", "История")

    item = next(i for i in _summary(conn)["items"] if i["subject"] == "История")
    assert item["attended"] == 3
    assert item["is_attested"] is True


def test_excused_not_counted(conn) -> None:
    """Уважительная причина в зачёт НЕ идёт (на паре студента не было)."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 2, "excused", "Литература")
        _mark(conn, GROUP, STUDENT, "2026-10-02", 2, "excused", "Литература")
        _mark(conn, GROUP, STUDENT, "2026-10-06", 2, "excused", "Литература")

    item = next(i for i in _summary(conn)["items"]
                if i["subject"] == "Литература")
    assert item["attended"] == 0
    assert item["total_lessons"] == 3, "отметки учтены, но не зачтены"
    assert item["is_attested"] is False
    assert item["need_more"] == 3


def test_absent_not_counted(conn) -> None:
    """Прогул в зачёт не идёт."""
    with transaction(conn):
        for day in ("2026-10-01", "2026-10-02", "2026-10-06"):
            _mark(conn, GROUP, STUDENT, day, 3, "absent", "Физика")

    item = next(i for i in _summary(conn)["items"] if i["subject"] == "Физика")
    assert item["attended"] == 0
    assert item["is_attested"] is False


# --- состав сводки ---

def test_empty_group_subject_present_in_summary(conn) -> None:
    """Предмет без отметок виден как 0/3, а не исчезает из отчёта."""
    summary = _summary(conn)
    subjects = [item["subject"] for item in summary["items"]]
    assert subjects == ["История", "Литература", "Физика"]
    assert summary["total_count"] == 3
    assert summary["attested_count"] == 0


def test_at_risk_sorted_by_need(conn) -> None:
    """В at_risk первыми идут те, кому нужно больше пар."""
    with transaction(conn):
        # История: 2 пары (нужно 1), Литература и Физика: 0 (нужно 3).
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "История")
        _mark(conn, GROUP, STUDENT, "2026-10-02", 1, "present", "История")

    at_risk = _summary(conn)["at_risk"]
    assert at_risk[-1]["subject"] == "История"
    assert at_risk[0]["need_more"] == 3


def test_counts_attested_and_total(conn) -> None:
    """attested_count и total_count считаются по предметам."""
    with transaction(conn):
        for day in ("2026-10-01", "2026-10-02", "2026-10-06"):
            _mark(conn, GROUP, STUDENT, day, 1, "present", "История")

    summary = _summary(conn)
    assert summary["attested_count"] == 1
    assert summary["total_count"] == 3
    assert len(summary["at_risk"]) == 2
# --- period_for_month ---

def test_period_current_month() -> None:
    """Текущий месяц: 1 число → сегодня."""
    start, end = atts.period_for_month(date(2026, 10, 20), 0)
    assert start == date(2026, 10, 1)
    assert end == date(2026, 10, 20), "будущие числа не входят"


def test_period_previous_month() -> None:
    """Прошлый месяц: 1 → последнее число (30 сентября)."""
    start, end = atts.period_for_month(date(2026, 10, 20), -1)
    assert start == date(2026, 9, 1)
    assert end == date(2026, 9, 30)


def test_period_previous_month_crosses_year() -> None:
    """Январь → декабрь прошлого года (сдвиг через границу года)."""
    start, end = atts.period_for_month(date(2026, 1, 15), -1)
    assert start == date(2025, 12, 1)
    assert end == date(2025, 12, 31)


def test_period_february_end() -> None:
    """Конец месяца считается по календарю (февраль — 28 дней)."""
    start, end = atts.period_for_month(date(2026, 3, 10), -1)
    assert start == date(2026, 2, 1)
    assert end == date(2026, 2, 28)


def test_period_offset_minus_two() -> None:
    """Сдвиг на два месяца назад тоже работает."""
    start, end = atts.period_for_month(date(2026, 1, 5), -2)
    assert start == date(2025, 11, 1)
    assert end == date(2025, 11, 30)


# --- fallback предмета ---

def test_subject_fallback_from_schedule(conn) -> None:
    """subject NULL → предмет берётся из schedule_cache.

    Даты — понедельники (5, 12, 19 октября): расписание тестовой группы
    заведено на понедельник, поэтому пары находятся только в эти дни.
    """
    with transaction(conn):
        for day in ("2026-10-05", "2026-10-12", "2026-10-19"):
            _mark(conn, GROUP, STUDENT, day, 1, "present", subject=None)

    item = next(i for i in _summary(conn)["items"] if i["subject"] == "История")
    assert item["attended"] == 3, "предмет восстановлен по расписанию"
    assert item["is_attested"] is True


def test_stored_subject_wins_over_schedule(conn) -> None:
    """Сохранённый предмет важнее расписания (расписание могло измениться)."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "ОП.99 Старое")

    subjects = [item["subject"] for item in _summary(conn)["items"]]
    assert any("ОП.99" in subject for subject in subjects)


def test_subject_for_mark_unknown(conn) -> None:
    """Без предмета и без расписания — «Без предмета»."""
    assert atts.subject_for_mark(conn, {
        "group_name": "НетГруппы", "date_iso": "2026-10-01", "para": 1,
        "subject": None,
    }) == atts.UNKNOWN_SUBJECT


def test_subject_for_mark_broken_date(conn) -> None:
    """Испорченная дата не роняет расчёт."""
    assert atts.subject_for_mark(conn, {
        "group_name": GROUP, "date_iso": "не дата", "para": 1,
        "subject": None,
    }) == atts.UNKNOWN_SUBJECT
# --- отчёт по группе ---

def _close_all_subjects(conn, tg_id: int) -> None:
    """Отметить студенту все три предмета по 3 раза (аттестован везде)."""
    for para, subject in SUBJECTS.items():
        for day in ("2026-10-01", "2026-10-02", "2026-10-06"):
            _mark(conn, GROUP, tg_id, day, para, "present", subject)


def test_group_report_at_risk_and_excellent(conn) -> None:
    """Отличник попадает в excellent, остальные — в at_risk."""
    with transaction(conn):
        _close_all_subjects(conn, STUDENT)

    start, end = atts.period_for_month(PERIOD_DAY, 0)
    report = atts.get_group_attestation_report(conn, GROUP, start, end)

    names_ok = [item["full_name"] for item in report["excellent"]]
    names_risk = [item["full_name"] for item in report["at_risk"]]
    assert "Иванов И.И." in names_ok
    assert "Петров П.П." in names_risk
    assert "Иванов И.И." not in names_risk


def test_group_report_risk_sorted_by_count(conn) -> None:
    """Сначала те, у кого больше проблемных предметов."""
    with transaction(conn):
        # Петров закрывает Историю и Литературу — риск только по Физике.
        for para, subject in [(1, "История"), (2, "Литература")]:
            for day in ("2026-10-01", "2026-10-02", "2026-10-06"):
                _mark(conn, GROUP, OTHER_STUDENT, day, para, "present", subject)

    start, end = atts.period_for_month(PERIOD_DAY, 0)
    report = atts.get_group_attestation_report(conn, GROUP, start, end)

    counts = [item["at_risk_count"] for item in report["at_risk"]]
    assert counts == sorted(counts, reverse=True)
    assert report["at_risk"][-1]["full_name"] == "Петров П.П."


def test_group_report_subjects_listed(conn) -> None:
    """По студенту в риске перечислены его проблемные предметы."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "История")

    start, end = atts.period_for_month(PERIOD_DAY, 0)
    report = atts.get_group_attestation_report(conn, GROUP, start, end)

    student = next(item for item in report["at_risk"]
                   if item["full_name"] == "Иванов И.И.")
    subjects = [item["subject"] for item in student["subjects"]]
    assert "История" in subjects
    assert "Физика" in subjects


def test_group_report_empty_group(conn) -> None:
    """Пустая группа не роняет отчёт."""
    start, end = atts.period_for_month(PERIOD_DAY, 0)
    report = atts.get_group_attestation_report(conn, "НетТакой", start, end)
    assert report == {"at_risk": [], "excellent": []}


def test_group_report_period_isolates_months(conn) -> None:
    """Отметки другого месяца в отчёт не попадают.

    В группе трое (староста и двое студентов), и в прошлом месяце отметок
    нет ни у кого — значит все трое в зоне риска, отличников нет.
    """
    with transaction(conn):
        _close_all_subjects(conn, STUDENT)

    # Прошлый месяц: у студента там отметок нет.
    start, end = atts.period_for_month(PERIOD_DAY, -1)
    report = atts.get_group_attestation_report(conn, GROUP, start, end)

    assert report["excellent"] == []
    assert len(report["at_risk"]) == 3


def test_mark_saves_subject_into_row(conn) -> None:
    """Отметка хранит предмет (миграция 12) — иначе fallback недоступен."""
    with transaction(conn):
        _mark(conn, GROUP, STUDENT, "2026-10-01", 1, "present", "История")

    row = conn.execute(
        "SELECT subject FROM attendance WHERE tg_id = ?", (STUDENT,)
    ).fetchone()
    assert row["subject"] == "История"


def test_threshold_from_config() -> None:
    """Порог аттестации — 3 пары (значение из config)."""
    from bot.config import MIN_ATTESTATION_LESSONS

    assert MIN_ATTESTATION_LESSONS == 3