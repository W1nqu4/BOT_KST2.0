"""Тесты сервиса расписания (bot.services.schedule_service).

Чётность, выборка занятий с учётом чётности, наложение замен и refresh-циклы.
Тесты на реальном расписании используют образец из tests/conftest.py:
если файла нет, тест скипается.
"""

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from bot.db import get_connection
from bot.migrations import apply_migrations
from bot.services import cache_service, schedule_service as ss

GROUP = "26КАД"


async def _no_sleep(seconds: float) -> None:
    """Заглушка asyncio.sleep: тесты циклов не должны реально ждать."""
    return None


@pytest.fixture()
def conn(tmp_path: Path):
    """Соединение к временной БД с миграциями."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def db_with_schedule(conn, parsed_schedule):
    """БД с реальным расписанием из образца (76 групп, 1456 занятий)."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


# --- week_type_for_date: чётность ЧИСЛА МЕСЯЦА ---

@pytest.mark.parametrize(("iso", "expected"), [
    ("2026-09-20", "Чет"),      # вс, 20
    ("2026-09-21", "нечет"),    # пн, 21
    ("2026-09-22", "Чет"),      # вт, 22
    ("2026-09-23", "нечет"),    # ср, 23
    ("2026-09-27", "нечет"),    # вс, 27
    ("2026-09-28", "Чет"),      # пн, 28
    ("2026-09-30", "Чет"),      # ср, 30
    ("2026-10-01", "нечет"),    # чт, 01
])
def test_week_type_for_date(iso: str, expected: str) -> None:
    """Чёт/нечет определяется числом месяца. День недели и месяц не влияют."""
    assert ss.week_type_for_date(date.fromisoformat(iso)) == expected


def test_week_type_same_weekday_differs_across_months() -> None:
    """Оба понедельники, но числа разные → разная чётность (не по неделям)."""
    assert ss.week_type_for_date(date(2026, 9, 21)) == "нечет"
    assert ss.week_type_for_date(date(2026, 9, 28)) == "Чет"


def test_week_type_month_change_keeps_counting() -> None:
    """Смена месяца не сбрасывает отсчёт: 30.09 → Чет, 01.10 → нечет."""
    assert ss.week_type_for_date(date(2026, 9, 30)) == "Чет"
    assert ss.week_type_for_date(date(2026, 10, 1)) == "нечет"
    assert ss.week_type_for_date(date(2026, 10, 2)) == "Чет"


def test_week_type_values_match_parser_convention() -> None:
    """Возвращаются те же строки, что пишет парсер расписания."""
    assert ss.WEEK_TYPE_EVEN == "Чет"
    assert ss.WEEK_TYPE_ODD == "нечет"
    assert ss.WEEK_TYPE_ALWAYS == ""


# --- time_range_for_para ---

def test_time_range_known_paras() -> None:
    assert ss.time_range_for_para(1) == "09:00-10:35"
    assert ss.time_range_for_para(3) == "13:15-14:50"


def test_time_range_unknown_para_is_empty() -> None:
    assert ss.time_range_for_para(9) == ""


# --- get_lessons_for_day: реальное расписание 26КАД ---

SECOND_TUESDAY_EVEN = date(2026, 9, 22)     # вторник, число 22 → Чет
THIRD_TUESDAY_ODD = date(2026, 9, 29)       # вторник, число 29 → нечет


def test_tuesday_even_has_social_studies(db_with_schedule) -> None:
    """26КАД, вт 22.09.2026 (Чет): пара 1 — «ОД.04 Обществознание»."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    subjects = {l["para_number"]: l["subject"] for l in lessons}
    assert 1 in subjects
    assert "Обществознание" in subjects[1]
    assert subjects[1] == "ОД.04 Обществознание"


def test_tuesday_even_history_at_para_3(db_with_schedule) -> None:
    """В чётный вторник пара 3 — «ОД.03 История» (week_type 'Чет')."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    by_para = {l["para_number"]: l for l in lessons}
    assert by_para[3]["subject"] == "ОД.03 История"
    assert by_para[3]["week_type"] == "Чет"


def test_tuesday_odd_has_math_at_para_3(db_with_schedule) -> None:
    """В нечётный вторник пара 3 — «ОД.07 Математика» (week_type 'нечет')."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, THIRD_TUESDAY_ODD)
    by_para = {l["para_number"]: l for l in lessons}
    assert 3 in by_para
    assert by_para[3]["subject"] == "ОД.07 Математика"
    assert by_para[3]["week_type"] == "нечет"


def test_weekly_lessons_shown_on_both_dates(db_with_schedule) -> None:
    """Пары с week_type='' показываются в обе даты (обе недели)."""
    even = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    odd = ss.get_lessons_for_day(db_with_schedule, GROUP, THIRD_TUESDAY_ODD)

    for lessons in (even, odd):
        by_para = {l["para_number"]: l for l in lessons}
        assert "Обществознание" in by_para[1]["subject"]
        assert "Физика" in by_para[2]["subject"]
        assert by_para[1]["week_type"] == ""
        assert by_para[2]["week_type"] == ""


def test_parity_dependent_pairs_do_not_mix(db_with_schedule) -> None:
    """Пара 3 в чётный и нечётный вторник — разные предметы, не оба сразу."""
    even = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    odd = ss.get_lessons_for_day(db_with_schedule, GROUP, THIRD_TUESDAY_ODD)
    even_para3 = next(l["subject"] for l in even if l["para_number"] == 3)
    odd_para3 = next(l["subject"] for l in odd if l["para_number"] == 3)
    assert even_para3 != odd_para3
    assert "История" in even_para3
    assert "Математика" in odd_para3


def test_lessons_sorted_by_para_number_as_int(db_with_schedule) -> None:
    """Сортировка по номеру пары как по числу (не строке)."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    paras = [l["para_number"] for l in lessons]
    assert paras == sorted(paras)
    assert all(isinstance(p, int) for p in paras)


def test_lesson_fields_and_time_range(db_with_schedule) -> None:
    """У занятия есть все поля, time_range — по звонкам."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    expected = {"para_number", "subject", "teacher", "room",
                "week_type", "time_range"}
    for lesson in lessons:
        assert expected <= set(lesson)
    by_para = {l["para_number"]: l for l in lessons}
    assert by_para[1]["time_range"] == "09:00-10:35"
    assert by_para[2]["time_range"] == "10:45-12:20"


def test_unknown_group_returns_empty(db_with_schedule) -> None:
    assert ss.get_lessons_for_day(db_with_schedule, "99XXX", SECOND_TUESDAY_EVEN) == []


def test_empty_cache_returns_empty(conn) -> None:
    """Без данных в кэше — пустой список, без исключений."""
    assert ss.get_lessons_for_day(conn, GROUP, SECOND_TUESDAY_EVEN) == []


def test_workdays_have_lessons_tuesday_and_wednesday(db_with_schedule) -> None:
    """Задача 1: Вт 29.09.2026 и Ср 30.09.2026 у 26КАД дают непустые списки."""
    tuesday = ss.get_lessons_for_day(db_with_schedule, GROUP, date(2026, 9, 29))
    wednesday = ss.get_lessons_for_day(db_with_schedule, GROUP, date(2026, 9, 30))

    assert len(tuesday) == 3
    assert len(wednesday) == 3
    assert {l["para_number"] for l in tuesday} == {1, 2, 3}
    assert {l["para_number"] for l in wednesday} == {1, 2, 3}
    assert ss.week_type_for_date(date(2026, 9, 29)) == "нечет"
    assert ss.week_type_for_date(date(2026, 9, 30)) == "Чет"


def test_monday_to_saturday_all_have_lessons(db_with_schedule) -> None:
    """Пн–Сб (28.09–03.10) у 26КАД непустые — листание не упирается в пусто."""
    for offset in range(6):
        d = date(2026, 9, 28) + timedelta(days=offset)
        lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, d)
        assert lessons, f"{d.isoformat()} ({d.isoweekday()}) пуст"


def test_saturday_sunday_have_no_lessons_for_group(db_with_schedule) -> None:
    """Воскресенье (day 7) у 26КАД занятий нет."""
    sunday = date(2026, 9, 27)
    assert sunday.isoweekday() == 7
    assert ss.get_lessons_for_day(db_with_schedule, GROUP, sunday) == []
# --- apply_substitutions ---

SUB_DATE = "2026-09-22"


def _seed_substitutions(conn, rows: list[dict]) -> None:
    """Положить замены в кэш."""
    base = {"group": GROUP, "date_iso": SUB_DATE, "para": 1,
            "old_subject": "ОД.04 Обществознание", "new_subject": "",
            "teacher": "", "room": "", "is_cancelled": False,
            "is_self_study": False}
    cache_service.save_substitutions(conn, [dict(base, **row) for row in rows])


def test_substitution_replaces_subject_and_marks(db_with_schedule) -> None:
    """Замена на паре 1 → subject заменён, is_substitution=True."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    _seed_substitutions(db_with_schedule, [{
        "para": 1, "new_subject": "ОД.12 Химия",
        "teacher": "Витюгова Наталья Владимировна", "room": "313А",
    }])

    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    para1 = next(l for l in result if l["para_number"] == 1)
    assert para1["subject"] == "ОД.12 Химия"
    assert para1["teacher"] == "Витюгова Наталья Владимировна"
    assert para1["room"] == "313А"
    assert para1["is_substitution"] is True
    assert para1["planned_subject"] == "ОД.04 Обществознание"


def test_cancelled_substitution_marks_lesson(db_with_schedule) -> None:
    """is_cancelled=True → у занятия is_cancelled=True."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    _seed_substitutions(db_with_schedule, [{"para": 2, "is_cancelled": True}])

    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    para2 = next(l for l in result if l["para_number"] == 2)
    assert para2["is_cancelled"] is True
    assert para2["is_substitution"] is True


def test_self_study_substitution_marks_lesson(db_with_schedule) -> None:
    """is_self_study=True → у занятия is_self_study=True."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    _seed_substitutions(db_with_schedule, [{
        "para": 2, "new_subject": "ОД.11 Физика", "is_self_study": True,
    }])

    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    para2 = next(l for l in result if l["para_number"] == 2)
    assert para2["is_self_study"] is True


def test_substitution_for_unknown_para_added_separately(db_with_schedule) -> None:
    """Замена на пару вне плана → отдельная запись, planned_subject=''."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    assert all(l["para_number"] != 5 for l in lessons), "пары 5 в плане нет"

    _seed_substitutions(db_with_schedule, [{
        "para": 5, "new_subject": "Консультация по математике",
        "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
    }])

    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    added = next(l for l in result if l["para_number"] == 5)
    assert added["subject"] == "Консультация по математике"
    assert added["planned_subject"] == ""
    assert added["is_substitution"] is True
    assert added["time_range"] == "16:45-18:05"
    assert len(result) == len(lessons) + 1
def test_apply_does_not_mutate_input(db_with_schedule) -> None:
    """Входной список не мутируется: возвращается новый."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    snapshot = [dict(l) for l in lessons]
    _seed_substitutions(db_with_schedule, [{
        "para": 1, "new_subject": "ОД.12 Химия", "room": "313А",
    }])

    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    assert lessons == snapshot, "входные занятия изменились"
    assert result is not lessons
    para1_result = next(l for l in result if l["para_number"] == 1)
    assert para1_result["subject"] == "ОД.12 Химия"


def test_apply_without_substitutions_returns_copy(db_with_schedule) -> None:
    """Замен нет → новый список с теми же данными и флагами по умолчанию."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    assert len(result) == len(lessons)
    assert result is not lessons
    assert all(l["is_substitution"] is False for l in result)
    assert all(l["is_cancelled"] is False for l in result)


def test_apply_keeps_other_lessons_untouched(db_with_schedule) -> None:
    """Замена на паре 1 не влияет на остальные пары."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    _seed_substitutions(db_with_schedule, [{
        "para": 1, "new_subject": "ОД.12 Химия",
    }])
    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    para2 = next(l for l in result if l["para_number"] == 2)
    assert para2["is_substitution"] is False
    assert "Физика" in para2["subject"]


def test_apply_other_group_substitutions_not_applied(db_with_schedule) -> None:
    """Замены другой группы не применяются."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    cache_service.save_substitutions(db_with_schedule, [{
        "group": "26МЭГ", "date_iso": SUB_DATE, "para": 1,
        "old_subject": "ОД.04 Обществознание", "new_subject": "ЧУЖАЯ ЗАМЕНА",
        "teacher": "", "room": "", "is_cancelled": False, "is_self_study": False,
    }])
    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    assert all("ЧУЖАЯ ЗАМЕНА" not in l["subject"] for l in result)


def test_apply_sorted_by_para(db_with_schedule) -> None:
    """Результат отсортирован по номеру пары."""
    lessons = ss.get_lessons_for_day(db_with_schedule, GROUP, SECOND_TUESDAY_EVEN)
    _seed_substitutions(db_with_schedule, [{
        "para": 5, "new_subject": "Добавленная пара",
    }])
    result = ss.apply_substitutions(
        db_with_schedule, lessons, GROUP, SECOND_TUESDAY_EVEN
    )
    paras = [l["para_number"] for l in result]
    assert paras == sorted(paras)


# --- get_subjects_for_group / get_nearest_lessons_for_subject ---

def test_subjects_are_unique_and_sorted(db_with_schedule) -> None:
    """Предметы группы: без дублей, по алфавиту, без пустых строк."""
    subjects = ss.get_subjects_for_group(db_with_schedule, GROUP)
    assert subjects == sorted(subjects)
    assert len(subjects) == len(set(subjects))
    assert all(s.strip() for s in subjects)
    assert "ОД.07 Математика" in subjects


def test_subjects_unknown_group_is_empty(db_with_schedule) -> None:
    assert ss.get_subjects_for_group(db_with_schedule, "99XXX") == []


def test_nearest_lessons_sorted_by_date_then_para(db_with_schedule) -> None:
    """10 ближайших пар предмета отсортированы по дате, затем по номеру пары."""
    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        start=date(2026, 9, 28),
    )
    assert len(lessons) == 10
    keys = [(l["date"], l["para_number"]) for l in lessons]
    assert keys == sorted(keys)
    assert all(l["subject"] == "ОД.07 Математика" for l in lessons)
    assert all(l["date"] >= date(2026, 9, 28) for l in lessons)


def test_nearest_lessons_respect_limit(db_with_schedule) -> None:
    """Лимит соблюдается: просим 3 — получаем 3."""
    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        limit=3, start=date(2026, 9, 28),
    )
    assert len(lessons) == 3


def test_nearest_lessons_respect_horizon(db_with_schedule) -> None:
    """Горизонт соблюдается: все пары внутри окна ``horizon_days``."""
    start = date(2026, 9, 28)
    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        limit=50, horizon_days=7, start=start,
    )
    assert lessons
    assert all((l["date"] - start).days <= 7 for l in lessons)


def test_nearest_lessons_follow_parity(db_with_schedule) -> None:
    """Чётность учитывается: в нечётный день пары с week_type 'нечет' есть,
    а пары, привязанные только к чётному дню, не появляются.

    «ОД.07 Математика» идёт у 26КАД и в чёт, и в нечет (проверено на
    расписании); проверяем, что чётность дня в результате совпадает с
    фактической для этой даты.
    """
    for iso, expected in (("2026-09-29", "нечет"), ("2026-09-30", "Чет")):
        d = date.fromisoformat(iso)
        lessons = ss.get_nearest_lessons_for_subject(
            db_with_schedule, GROUP, "ОД.07 Математика",
            limit=5, horizon_days=0, start=d,
        )
        assert lessons, f"{iso} должен содержать пары по математике"
        assert all(ss.week_type_for_date(l["date"]) == expected
                   for l in lessons)


def test_nearest_lessons_unknown_subject_is_empty(db_with_schedule) -> None:
    """Несуществующий предмет → пустой список (в UI будет «пар не найдено»)."""
    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "Нет такого предмета",
        start=date(2026, 9, 28),
    )
    assert lessons == []


def test_nearest_lessons_apply_substitutions(db_with_schedule) -> None:
    """Замены учитываются: подменённый предмет попадает в свою выдачу.

    Ставим замену «математика → химия» на пару 3 вторника 29.09 и проверяем,
    что в выдаче по химии этот день появился.
    """
    cache_service.save_substitutions(db_with_schedule, [{
        "group": GROUP, "date_iso": "2026-09-29", "para": 3,
        "old_subject": "ОД.07 Математика", "new_subject": "ОД.12 Химия",
        "teacher": "Витюгова Наталья Владимировна", "room": "313А",
        "is_cancelled": False, "is_self_study": False,
    }])

    chemistry = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.12 Химия",
        limit=10, horizon_days=0, start=date(2026, 9, 29),
    )
    assert any(l["date"] == date(2026, 9, 29) and l["para_number"] == 3
               for l in chemistry)

    math = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        limit=10, horizon_days=0, start=date(2026, 9, 29),
    )
    assert not any(l["para_number"] == 3 for l in math)


def test_nearest_lessons_skip_cancelled(db_with_schedule) -> None:
    """Отменённая пара не показывается как занятие по предмету."""
    cache_service.save_substitutions(db_with_schedule, [{
        "group": GROUP, "date_iso": "2026-09-29", "para": 3,
        "old_subject": "ОД.07 Математика", "new_subject": "",
        "teacher": "", "room": "", "is_cancelled": True,
        "is_self_study": False,
    }])

    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        limit=10, horizon_days=0, start=date(2026, 9, 29),
    )
    assert not any(l["para_number"] == 3 for l in lessons)


def test_nearest_lessons_have_display_fields(db_with_schedule) -> None:
    """В выдаче есть всё для карточки: дата, номер пары, время, кабинет."""
    lessons = ss.get_nearest_lessons_for_subject(
        db_with_schedule, GROUP, "ОД.07 Математика",
        limit=1, start=date(2026, 9, 28),
    )
    lesson = lessons[0]
    for field in ("date", "para_number", "subject", "teacher", "room",
                  "time_range", "week_type"):
        assert field in lesson, f"нет поля {field}"
    assert isinstance(lesson["date"], date)
    assert lesson["time_range"]
# --- refresh-циклы ---

async def test_schedule_loop_survives_errors(conn, monkeypatch) -> None:
    """Ошибка прохода не роняет цикл: логируется и продолжается."""
    calls: list[int] = []

    async def fake_refresh(connection):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сеть упала")
        raise asyncio.CancelledError

    monkeypatch.setattr(cache_service, "refresh_schedule", fake_refresh)
    monkeypatch.setattr(ss, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ss.refresh_schedule_loop(conn)

    assert len(calls) == 2, "после ошибки цикл должен сделать ещё проход"


async def test_schedule_loop_propagates_cancellation(conn, monkeypatch) -> None:
    """CancelledError пролетает наружу — задача останавливается штатно."""

    async def cancel_refresh(connection):
        raise asyncio.CancelledError

    monkeypatch.setattr(cache_service, "refresh_schedule", cancel_refresh)

    with pytest.raises(asyncio.CancelledError):
        await ss.refresh_schedule_loop(conn)


async def test_substitutions_loop_survives_errors(conn, monkeypatch) -> None:
    """Цикл замен тоже переживает ошибку и продолжает работу."""
    calls: list[int] = []

    async def fake_refresh(connection):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сеть упала")
        raise asyncio.CancelledError

    monkeypatch.setattr(cache_service, "refresh_substitutions", fake_refresh)
    monkeypatch.setattr(ss, "_sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ss.refresh_substitutions_loop(conn)

    assert len(calls) == 2


async def test_substitutions_loop_propagates_cancellation(conn, monkeypatch) -> None:
    async def cancel_refresh(connection):
        raise asyncio.CancelledError

    monkeypatch.setattr(cache_service, "refresh_substitutions", cancel_refresh)

    with pytest.raises(asyncio.CancelledError):
        await ss.refresh_substitutions_loop(conn)


async def test_loops_use_configured_intervals(conn, monkeypatch) -> None:
    """Интервалы берутся из конфига: 6 часов и 15 минут."""
    from bot.config import SCHEDULE_REFRESH_SECONDS, SUBSTITUTIONS_REFRESH_SECONDS

    assert SCHEDULE_REFRESH_SECONDS == 6 * 60 * 60
    assert SUBSTITUTIONS_REFRESH_SECONDS == 15 * 60

    slept: list[float] = []

    async def record_sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    async def ok_refresh(connection):
        return 0

    monkeypatch.setattr(cache_service, "refresh_schedule", ok_refresh)
    monkeypatch.setattr(ss, "_sleep", record_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ss.refresh_schedule_loop(conn)
    assert slept == [SCHEDULE_REFRESH_SECONDS]
    assert ss.time_range_for_para(9) == ""