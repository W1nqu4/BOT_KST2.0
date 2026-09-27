"""Тесты сервиса дедлайнов (bot.services.deadline_service)."""

from datetime import date
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import deadline_service as dl

TODAY = date(2026, 9, 27)


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и одним пользователем."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (1, '26КАД', '2026-09-01T00:00:00+07:00')"
        )
    yield c
    c.close()


# --- add / get ---

def test_add_returns_id_and_stores(conn) -> None:
    deadline_id = dl.add(conn, 1, "МДК 01.01", "Кудрявцева П.",
                         "Курсовая", "2026-10-01")
    assert deadline_id > 0
    stored = dl.get(conn, deadline_id, 1)
    assert stored is not None
    assert stored["subject"] == "МДК 01.01"
    assert stored["teacher"] == "Кудрявцева П."
    assert stored["task"] == "Курсовая"
    assert stored["deadline_date"] == "2026-10-01"
    assert stored["deleted_at"] is None


def test_add_without_date(conn) -> None:
    """Дедлайн без даты: хранится NULL, а не пустая строка."""
    deadline_id = dl.add(conn, 1, "Химия", "", "Лаба", None)
    row = conn.execute(
        "SELECT deadline_date FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row["deadline_date"] is None


def test_add_empty_date_string_becomes_null(conn) -> None:
    deadline_id = dl.add(conn, 1, "X", "", "Y", "")
    row = conn.execute(
        "SELECT deadline_date FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row["deadline_date"] is None


def test_get_foreign_deadline_returns_none(conn) -> None:
    """Чужой дедлайн недоступен: id приходят из callback и подделываемы."""
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.get(conn, deadline_id, 999) is None


def test_get_unknown_id_returns_none(conn) -> None:
    assert dl.get(conn, 12345, 1) is None


# --- list_active ---

def test_list_active_sorted_by_date_without_date_last(conn) -> None:
    """Сортировка по сроку; дедлайны без даты — в конце."""
    dl.add(conn, 1, "C", "", "третья", "2026-12-01")
    dl.add(conn, 1, "A", "", "первая", "2026-10-01")
    dl.add(conn, 1, "B", "", "без даты", None)
    tasks = [item["task"] for item in dl.list_active(conn, 1)]
    assert tasks == ["первая", "третья", "без даты"]


def test_list_active_only_own(conn) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at) VALUES (2, '26Р', 'x')"
        )
    dl.add(conn, 1, "Мой", "", "мой", "2026-10-01")
    dl.add(conn, 2, "Чужой", "", "чужой", "2026-10-01")
    mine = dl.list_active(conn, 1)
    assert [i["subject"] for i in mine] == ["Мой"]


# --- update ---

def test_update_changes_fields(conn) -> None:
    deadline_id = dl.add(conn, 1, "Старый", "", "Старая задача", "2026-10-01")
    assert dl.update(conn, deadline_id, 1, "Новый", "Препод",
                     "Новая задача", "2026-11-11") is True
    updated = dl.get(conn, deadline_id, 1)
    assert updated["subject"] == "Новый"
    assert updated["teacher"] == "Препод"
    assert updated["task"] == "Новая задача"
    assert updated["deadline_date"] == "2026-11-11"


def test_update_can_clear_date(conn) -> None:
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.update(conn, deadline_id, 1, "X", "", "Y", None) is True
    assert dl.get(conn, deadline_id, 1)["deadline_date"] is None


def test_update_foreign_returns_false(conn) -> None:
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.update(conn, deadline_id, 999, "Взлом", "", "Взлом", None) is False
    assert dl.get(conn, deadline_id, 1)["subject"] == "X"


# --- soft_delete ---

def test_soft_delete_hides_from_active_but_keeps_row(conn) -> None:
    """Мягкое удаление: запись остаётся в таблице, но не в активных."""
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.soft_delete(conn, deadline_id, 1) is True

    assert dl.list_active(conn, 1) == []
    assert dl.get(conn, deadline_id, 1) is None

    row = conn.execute(
        "SELECT deleted_at FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row is not None, "строка должна остаться физически"
    assert row["deleted_at"], "deleted_at должен быть заполнен"


def test_soft_delete_twice_returns_false(conn) -> None:
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.soft_delete(conn, deadline_id, 1) is True
    assert dl.soft_delete(conn, deadline_id, 1) is False


def test_soft_delete_foreign_returns_false(conn) -> None:
    deadline_id = dl.add(conn, 1, "X", "", "Y", "2026-10-01")
    assert dl.soft_delete(conn, deadline_id, 999) is False
# --- days_left ---

@pytest.mark.parametrize(("iso", "expected"), [
    ("2026-09-20", -7),     # прошлое
    ("2026-09-26", -1),     # вчера
    ("2026-09-27", 0),      # сегодня
    ("2026-09-28", 1),      # завтра
    ("2026-10-04", 7),      # ровно неделя
    ("2026-10-05", 8),      # больше недели
])
def test_days_left_values(iso: str, expected: int) -> None:
    assert dl.days_left(iso, TODAY) == expected


@pytest.mark.parametrize("raw", [None, "", "мусор", "2026-13-45"])
def test_days_left_none_cases(raw) -> None:
    """Нет даты или она не разбирается → None."""
    assert dl.days_left(raw, TODAY) is None


# --- urgency_of / group_by_urgency ---

@pytest.mark.parametrize(("days", "expected_title"), [
    (-5, "Просрочено"),
    (-1, "Просрочено"),
    (0, "Сегодня"),
    (1, "Завтра"),
    (2, "На неделе"),
    (7, "На неделе"),
    (8, "Позже"),
    (100, "Позже"),
    (None, "Без даты"),
])
def test_urgency_of(days, expected_title: str) -> None:
    _, title = dl.urgency_of(days)
    assert title == expected_title


def test_group_by_urgency_all_six_groups() -> None:
    """Все шесть групп распределяются корректно."""
    items = [
        {"id": 1, "deadline_date": "2026-09-20"},   # просрочено
        {"id": 2, "deadline_date": "2026-09-27"},   # сегодня
        {"id": 3, "deadline_date": "2026-09-28"},   # завтра
        {"id": 4, "deadline_date": "2026-09-30"},   # на неделе (3 дня)
        {"id": 5, "deadline_date": "2026-10-20"},   # позже
        {"id": 6, "deadline_date": None},           # без даты
    ]
    groups = dl.group_by_urgency(items, TODAY)
    assert [title for _, title, _ in groups] == [
        "Просрочено", "Сегодня", "Завтра", "На неделе", "Позже", "Без даты",
    ]
    ids = {title: [i["id"] for i in chunk] for _, title, chunk in groups}
    assert ids["Просрочено"] == [1]
    assert ids["Сегодня"] == [2]
    assert ids["Завтра"] == [3]
    assert ids["На неделе"] == [4]
    assert ids["Позже"] == [5]
    assert ids["Без даты"] == [6]


def test_group_by_urgency_skips_empty_groups() -> None:
    """Пустые группы не показываем."""
    groups = dl.group_by_urgency(
        [{"id": 1, "deadline_date": "2026-09-28"}], TODAY
    )
    assert len(groups) == 1
    assert groups[0][1] == "Завтра"


def test_group_by_urgency_adds_days_left() -> None:
    groups = dl.group_by_urgency(
        [{"id": 1, "deadline_date": "2026-09-30"}], TODAY
    )
    assert groups[0][2][0]["days_left"] == 3


def test_group_by_urgency_empty_input() -> None:
    assert dl.group_by_urgency([], TODAY) == []


def test_group_by_urgency_does_not_mutate_input() -> None:
    """Исходные словари не меняются (добавляется копия)."""
    item = {"id": 1, "deadline_date": "2026-09-28"}
    dl.group_by_urgency([item], TODAY)
    assert "days_left" not in item


# --- parse_manual_date ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("15.10.2026", "2026-10-15"),
    ("15.10.26", "2026-10-15"),
    ("15.10", "2026-10-15"),
    ("5.5.2026", "2026-05-05"),
    ("01-10-2026", "2026-10-01"),
])
def test_parse_manual_date_valid(raw: str, expected: str) -> None:
    assert dl.parse_manual_date(raw, TODAY) == expected


def test_parse_manual_date_short_uses_next_year_if_passed() -> None:
    """«01.01» при базе 27.09.2026 → 2027-01-01 (дата уже прошла)."""
    assert dl.parse_manual_date("01.01", TODAY) == "2027-01-01"


def test_parse_manual_date_short_keeps_current_year_if_future() -> None:
    """«15.10» при базе 27.09.2026 → 2026-10-15 (дата ещё не прошла)."""
    assert dl.parse_manual_date("15.10", TODAY) == "2026-10-15"


@pytest.mark.parametrize("raw", ["", "   ", "мусор", "32.13.2026", "2026-10-15"])
def test_parse_manual_date_invalid(raw: str) -> None:
    assert dl.parse_manual_date(raw, TODAY) is None


# --- remind_window ---

def test_remind_window_today_and_tomorrow(conn) -> None:
    """В напоминания попадают только сроки сегодня и завтра."""
    dl.add(conn, 1, "A", "", "сегодня", "2026-09-27")
    dl.add(conn, 1, "B", "", "завтра", "2026-09-28")
    dl.add(conn, 1, "C", "", "позже", "2026-10-10")
    dl.add(conn, 1, "D", "", "без даты", None)

    tasks = {item["task"] for item in dl.remind_window(conn, TODAY)}
    assert tasks == {"сегодня", "завтра"}


def test_remind_window_excludes_deleted(conn) -> None:
    deadline_id = dl.add(conn, 1, "A", "", "сегодня", "2026-09-27")
    dl.soft_delete(conn, deadline_id, 1)
    assert dl.remind_window(conn, TODAY) == []


def test_remind_window_sets_days_left(conn) -> None:
    dl.add(conn, 1, "A", "", "сегодня", "2026-09-27")
    dl.add(conn, 1, "B", "", "завтра", "2026-09-28")
    values = {i["task"]: i["days_left"] for i in dl.remind_window(conn, TODAY)}
    assert values == {"сегодня": 0, "завтра": 1}


# --- миграция 3 ---

def test_deadline_date_is_nullable_in_schema(conn) -> None:
    """Миграция 3 сделала deadline_date необязательной."""
    cols = {r["name"]: r["notnull"]
            for r in conn.execute("PRAGMA table_info(deadlines)")}
    assert cols["deadline_date"] == 0