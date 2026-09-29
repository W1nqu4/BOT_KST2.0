"""Тесты генерации .ics (bot.services.ics_service).

Проверяется то, что легко сломать: правило чётности в событиях, стабильность
UID, экранирование и фолдинг кириллицы.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import cache_service, deadline_service as dl
from bot.services import ics_service as ics

GROUP = "26КАД"
DAY = date(2026, 9, 21)          # понедельник, число 21 → нечет


def _unfold(text: str) -> str:
    """Развернуть фолдинг .ics: убрать CRLF и ведущий пробел продолжений.

    Нужно для проверок подстрок: длинная строка может быть разрезана на
    несколько физических, и прямой поиск по тексту не сработает.
    """
    return text.replace("\r\n ", "").replace("\r\n", "\n")


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и пользователем."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (1, ?, '2026-09-01T00:00:00+07:00')", (GROUP,),
        )
    yield c
    c.close()


@pytest.fixture()
def conn_with_schedule(conn, parsed_schedule):
    """БД с реальным расписанием."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


# --- токены ---

def test_token_created_once(conn) -> None:
    """Первый вызов создаёт токен, второй возвращает тот же."""
    first = ics.get_or_create_token(conn, 1)
    second = ics.get_or_create_token(conn, 1)
    assert first == second
    assert len(first) == 32
    assert all(ch in "0123456789abcdef" for ch in first)


def test_tokens_differ_between_users(conn) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (2, '26Р', 'x')"
        )
    assert ics.get_or_create_token(conn, 1) != ics.get_or_create_token(conn, 2)


def test_get_tg_id_by_token(conn) -> None:
    token = ics.get_or_create_token(conn, 1)
    assert ics.get_tg_id_by_token(conn, token) == 1
    assert ics.get_tg_id_by_token(conn, "неизвестный") is None
    assert ics.get_tg_id_by_token(conn, "") is None


# --- ссылки ---

def test_calendar_urls() -> None:
    https = ics.build_calendar_url("https://bot-kst.amvera.io", "abc")
    assert https == "https://bot-kst.amvera.io/calendar/abc.ics"
    assert ics.build_webcal_url("https://bot-kst.amvera.io", "abc") == (
        "webcal://bot-kst.amvera.io/calendar/abc.ics"
    )


def test_calendar_url_strips_trailing_slash() -> None:
    assert ics.build_calendar_url("https://x.io/", "t") == "https://x.io/calendar/t.ics"


# --- normalize_base_url ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("example.com", "https://example.com"),
    ("https://example.com/", "https://example.com"),
    ("", ""),
    ("   ", ""),
    (None, ""),
    ("http://localhost:8080", "http://localhost:8080"),
    ("http://localhost:8080/", "http://localhost:8080"),
    ("  https://x.com//  ", "https://x.com"),
    ("kst24-kst24.up.railway.app", "https://kst24-kst24.up.railway.app"),
])
def test_normalize_base_url(raw, expected) -> None:
    """Нормализация: срез слэша, схема https:// при её отсутствии, "" для пустого."""
    assert ics.normalize_base_url(raw) == expected


def test_normalize_base_url_is_same_as_config() -> None:
    """Логика одна: ics_service реэкспортирует функцию из config."""
    from bot.config import normalize_base_url as from_config

    assert ics.normalize_base_url("example.com") == from_config("example.com")


# --- пустой базовый URL: пустая строка, НЕ None ---

def test_build_calendar_url_empty_returns_empty_string() -> None:
    """Не задан базовый URL → "" (раньше могло получиться "/calendar/....ics")."""
    assert ics.build_calendar_url("", "abc") == ""


def test_build_webcal_url_empty_returns_empty_string() -> None:
    """Не задан базовый URL → "" (НЕ None) — иначе в UI появлялось «webcal: None»."""
    assert ics.build_webcal_url("", "abc") == ""
    assert ics.build_webcal_url("", "abc") is not None


def test_urls_never_return_none() -> None:
    """Ни одна из функций не возвращает None ни при каком входе."""
    for raw in ("", "   ", None, "example.com", "https://x.com/"):
        assert ics.build_calendar_url(raw, "t") is not None
        assert ics.build_webcal_url(raw, "t") is not None


def test_build_webcal_url_with_scheme() -> None:
    assert ics.build_webcal_url("https://x.com", "abc") == (
        "webcal://x.com/calendar/abc.ics"
    )


def test_build_webcal_url_without_scheme() -> None:
    """Базовый URL без схемы: раньше функция возвращала None (баг)."""
    assert ics.build_webcal_url("x.com", "abc") == "webcal://x.com/calendar/abc.ics"


def test_build_calendar_url_without_scheme_adds_https() -> None:
    assert ics.build_calendar_url("example.com", "t") == (
        "https://example.com/calendar/t.ics"
    )


def test_build_webcal_url_converts_http() -> None:
    """http:// тоже превращается в webcal:// (локальная отладка)."""
    assert ics.build_webcal_url("http://localhost:8080", "t") == (
        "webcal://localhost:8080/calendar/t.ics"
    )


# --- экранирование ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("просто", "просто"),
    ("a,b", "a\\,b"),
    ("a;b", "a\\;b"),
    ("a\\b", "a\\\\b"),
    ("a\nb", "a\\nb"),
    ("a\r\nb", "a\\nb"),
])
def test_ics_escape(raw: str, expected: str) -> None:
    assert ics._ics_escape(raw) == expected


def test_ics_escape_none() -> None:
    assert ics._ics_escape(None) == ""


# --- фолдинг ---

def test_fold_short_line_unchanged() -> None:
    assert ics._fold_line("SUMMARY:коротко") == "SUMMARY:коротко"


def test_fold_long_cyrillic_line_by_char_boundary() -> None:
    """Кириллица режется по границе символа, а не байта."""
    line = "SUMMARY:" + "Кириллический текст " * 8
    folded = ics._fold_line(line)

    parts = folded.split("\r\n")
    assert len(parts) > 1
    for part in parts:
        assert ics._octets(part) <= ics.ICS_MAX_OCTETS
    # Продолжения начинаются с пробела (RFC 5545).
    for part in parts[1:]:
        assert part.startswith(" ")

    restored = folded.replace("\r\n ", "")
    assert restored == line
    # Ни один символ не потерян и не битый.
# --- build_ics: структура ---

def test_build_ics_basic_structure(conn_with_schedule) -> None:
    """Файл обрамлён VCALENDAR, содержит VTIMEZONE и не содержит RRULE."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert text.startswith("BEGIN:VCALENDAR")
    assert text.rstrip().endswith("END:VCALENDAR")
    assert text.count("BEGIN:VTIMEZONE") == 1
    assert "TZID:Asia/Krasnoyarsk" in text
    assert "RRULE" not in text, "чёт/нечет по числу месяца — RRULE недопустим"


def test_build_ics_size_within_limit(conn_with_schedule) -> None:
    """Горизонт 60 дней даёт файл меньше 500 КБ."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert len(text.encode("utf-8")) < 500 * 1024


def test_build_ics_has_events(conn_with_schedule) -> None:
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert text.count("BEGIN:VEVENT") > 50


def test_build_ics_all_events_closed(conn_with_schedule) -> None:
    """Каждый VEVENT закрыт; число BEGIN и END совпадает."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT")


# --- правило чётности в событиях ---

def test_ics_even_day_has_even_lesson(conn_with_schedule) -> None:
    """22.09.2026 (число 22 → Чет): в файле есть «ОД.04 Обществознание»."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert "20260922" in text
    assert "Обществознание" in text


def test_ics_odd_day_has_odd_lesson(conn_with_schedule) -> None:
    """23.09.2026 (число 23 → нечет): в файле есть Математика."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    assert "20260923" in text
    assert "Математика" in text


def _events_starting_on(text: str, day_text: str) -> list[str]:
    """Блоки VEVENT, у которых DTSTART приходится на указанную дату.

    Фильтровать по вхождению даты во ВЕСЬ блок нельзя: в каждом событии есть
    ``DTSTAMP`` с датой генерации (``datetime.now``), поэтому в день, совпадающий
    с искомой датой, фильтр захватил бы все события подряд, и тест «падал» бы
    без всякой ошибки в коде. Сравниваем именно строку ``DTSTART``.
    """
    blocks: list[str] = []
    for block in text.split("BEGIN:VEVENT"):
        for line in block.splitlines():
            if line.startswith("DTSTART;TZID") and day_text in line:
                blocks.append(block)
                break
    return blocks


def test_ics_parity_lesson_not_on_wrong_week(conn_with_schedule) -> None:
    """Пара «Чет»-недели не попадает в нечётный день того же дня недели.

    22.09 и 29.09 — оба вторники, но числа 22 (Чет) и 29 (нечет).
    """
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    blocks = _events_starting_on(text, "20260929")
    assert blocks, "на 29.09 должны быть события"

    joined = " ".join(blocks)
    assert "ОД.03 История" not in joined, "в нечётный вторник История не идёт"
    assert "ОД.07 Математика" in joined


def test_ics_weekly_lesson_in_both_weeks(conn_with_schedule) -> None:
    """Пары с week_type='' (Обществознание, Физика) есть в обе даты вторника."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    for day_text in ("20260922", "20260929"):
        blocks = _events_starting_on(text, day_text)
        assert blocks, f"на {day_text} должны быть события"
        block = " ".join(blocks)
        assert "Обществознание" in block
        assert "Физика" in block


# --- UID ---

def test_ics_uids_stable_between_generations(conn_with_schedule) -> None:
    """Две генерации подряд дают одинаковые UID (клиент не создаёт дубли)."""
    first = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    second = ics.build_ics(conn_with_schedule, GROUP, today=DAY)
    uids_first = sorted(l for l in first.splitlines() if l.startswith("UID:"))
    uids_second = sorted(l for l in second.splitlines() if l.startswith("UID:"))
    assert uids_first == uids_second
    assert len(uids_first) == len(set(uids_first)), "UID уникальны в файле"
# --- замены ---

def test_ics_substitution_marked(conn_with_schedule) -> None:
    """Замена попадает в .ics с пометкой ЗАМЕНА и прежним предметом.

    Строка может быть свёрнута фолдингом (продолжение с ведущим пробелом),
    поэтому сравнение идёт по тексту с удалёнными переводами строк.
    """
    cache_service.save_substitutions(conn_with_schedule, [{
        "group": GROUP, "date_iso": "2026-09-22", "para": 1,
        "old_subject": "ОД.04 Обществознание", "new_subject": "ОД.12 Химия",
        "teacher": "Витюгова Наталья Владимировна", "room": "313А",
        "is_cancelled": False, "is_self_study": False,
    }])
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY, horizon_days=2)
    unfolded = _unfold(text)

    assert "ЗАМЕНА" in unfolded
    assert "ОД.12 Химия" in unfolded
    assert "Было: ОД.04 Обществознание" in unfolded


def test_ics_cancelled_marked(conn_with_schedule) -> None:
    """Отменённая пара помечается ОТМЕНА."""
    cache_service.save_substitutions(conn_with_schedule, [{
        "group": GROUP, "date_iso": "2026-09-22", "para": 2,
        "old_subject": "ОД.11 Физика", "new_subject": "",
        "teacher": "", "room": "", "is_cancelled": True, "is_self_study": False,
    }])
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY, horizon_days=2)
    assert "ОТМЕНА" in text


# --- дедлайны ---

def test_ics_deadline_event_all_day(conn_with_schedule) -> None:
    """Дедлайн — событие на весь день с напоминанием накануне в 20:00."""
    deadline_id = dl.add(conn_with_schedule, 1, "Химия", "", "Сдать лабу",
                         "2026-10-05")
    item = dl.get(conn_with_schedule, deadline_id, 1)
    text = ics.build_ics(conn_with_schedule, GROUP, deadlines=[item],
                         today=DAY, horizon_days=1)
    assert "DTSTART;VALUE=DATE:20261005" in text
    assert "DTEND;VALUE=DATE:20261006" in text
    assert "Сдать лабу" in text
    assert "TRIGGER;VALUE=DATE-TIME:20261004T200000" in text


def test_ics_deadline_without_date_skipped(conn_with_schedule) -> None:
    """Дедлайн без даты не попадает в календарь."""
    deadline_id = dl.add(conn_with_schedule, 1, "Химия", "", "Без срока", None)
    item = dl.get(conn_with_schedule, deadline_id, 1)
    text = ics.build_ics(conn_with_schedule, GROUP, deadlines=[item],
                         today=DAY, horizon_days=0)
    assert "Без срока" not in text


# --- экранирование и формат ---

def test_ics_escapes_comma_in_subject(conn_with_schedule) -> None:
    """Запятая в тексте экранируется обратным слэшем."""
    cache_service.save_substitutions(conn_with_schedule, [{
        "group": GROUP, "date_iso": "2026-09-22", "para": 1,
        "old_subject": "X", "new_subject": "Математика, часть 2",
        "teacher": "", "room": "", "is_cancelled": False, "is_self_study": False,
    }])
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY, horizon_days=2)
    assert "Математика\\, часть 2" in text


def test_ics_timezone_not_utc(conn_with_schedule) -> None:
    """DTSTART указан с TZID, без перевода в UTC (суффикса Z нет)."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY, horizon_days=0)
    starts = [l for l in text.splitlines() if l.startswith("DTSTART;TZID")]
    assert starts, "должны быть события с TZID"
    assert all(not line.endswith("Z") for line in starts)


def test_ics_uses_bell_times(conn_with_schedule) -> None:
    """Время события совпадает со звонками (1 пара — 09:00–10:35)."""
    text = ics.build_ics(conn_with_schedule, GROUP, today=DAY, horizon_days=7)
    assert "T090000" in text
    assert "T103500" in text


def test_ics_saturday_uses_saturday_bell_times(conn_with_schedule) -> None:
    """Суббота: 3 пара в .ics идёт 12:50-14:20 (не будничные 13:15-14:50)."""
    saturday = date(2026, 10, 3)
    assert saturday.weekday() == 5

    text = ics.build_ics(conn_with_schedule, GROUP, today=saturday, horizon_days=0)

    starts = [l for l in text.splitlines() if l.startswith("DTSTART;TZID")]
    assert starts, "в субботу у 26КАД должны быть события"

    # 1 пара 09:00-10:35 и 3 пара 12:50-14:20 (субботний график).
    assert "T090000" in text
    assert "T125000" in text, "3 пара должна начинаться в 12:50"
    assert "T142000" in text, "3 пара должна заканчиваться в 14:20"
    assert "T131500" not in text, "будничное 13:15 в субботу не должно появиться"


def test_ics_friday_uses_weekday_bell_times(conn_with_schedule) -> None:
    """Пятница: 3 пара идёт по будничным звонкам — 13:15-14:50."""
    friday = date(2026, 10, 2)
    assert friday.weekday() == 4

    text = ics.build_ics(conn_with_schedule, GROUP, today=friday, horizon_days=0)
    assert "T131500" in text
    assert "T145000" in text


def test_build_test_ics_single_event() -> None:
    """Проверочный .ics: одно событие через 2 минуты."""
    text = ics.build_test_ics(GROUP, minutes_ahead=2,
                              now=datetime(2026, 9, 21, 10, 0))
    assert text.count("BEGIN:VEVENT") == 1
    assert "20260921T100200" in text
    assert "Проверка календаря" in text