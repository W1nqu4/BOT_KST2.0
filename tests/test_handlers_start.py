"""Тесты хендлеров Шага 7: регистрация, рендер расписания, клавиатуры.

Сеть Telegram не используется: проверяются чистые функции (рендер, валидация,
подбор групп) и прогон апдейтов через диспетчер с подменным Bot.
"""

from datetime import date
from html import escape
from pathlib import Path

import pytest

from bot import db
from bot.db import get_connection
from bot.handlers import schedule as sched
from bot.handlers import start as start_h
from bot.keyboards import inline as ik
from bot.keyboards import reply as rk
from bot.migrations import apply_migrations
from bot.services import cache_service


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def conn_with_groups(conn, parsed_schedule):
    """БД с расписанием: доступные группы берутся из кэша."""
    cache_service.save_schedule(conn, parsed_schedule)
    return conn


# --- reply-клавиатура ---

def test_main_kb_layout() -> None:
    """Главное меню: две кнопки в ряд, resize_keyboard=True."""
    kb = rk.main_kb()
    assert kb.resize_keyboard is True
    assert [b.text for b in kb.keyboard[0]] == [rk.BTN_TODAY, rk.BTN_SCHEDULE]
    assert [b.text for b in kb.keyboard[1]] == [rk.BTN_DEADLINES, rk.BTN_PROFILE]


def test_stub_buttons_have_text() -> None:
    """Кнопки-заглушки имеют осмысленный ответ."""
    assert rk.IN_DEVELOPMENT == "🚧 Раздел в разработке"


# --- inline-клавиатуры ---

def test_week_nav_kb_callbacks() -> None:
    """Навигация: ◀️/Сегодня/▶️ и «Выбрать день»."""
    kb = ik.week_nav_kb(date(2026, 9, 22), 0)
    flat = [b for row in kb.inline_keyboard for b in row]
    data = [b.callback_data for b in flat]
    assert "sched:nav:-1" in data
    assert "sched:today" in data
    assert "sched:nav:+1" in data
    assert ik.CB_PICK_DAY in data


def test_pick_day_kb_has_six_days() -> None:
    """Выбор дня: шесть учебных дней и кнопка «Сегодня»."""
    kb = ik.pick_day_kb(date(2026, 9, 22))
    labels = [b.text for row in kb.inline_keyboard for b in row]
    for name in ("Понедельник", "Вторник", "Среда", "Четверг",
                 "Пятница", "Суббота"):
        assert name in labels
    assert "🔄 Сегодня" in labels


def test_day_name() -> None:
    assert ik.day_name(1) == "Понедельник"
    assert ik.day_name(6) == "Суббота"
    assert ik.day_name(99) == ""


def test_group_suggestions_kb() -> None:
    kb = ik.group_suggestions_kb(["26КАД", "26МЭГ"])
    calls = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert calls == ["group:pick:26КАД", "group:pick:26МЭГ"]


# --- валидация группы и подбор похожих ---

@pytest.mark.parametrize("raw", ["26КАД", "26С1", "25-КАД", "026ИМС"])
def test_is_valid_group_accepts(raw: str) -> None:
    assert start_h.is_valid_group(raw) is True


@pytest.mark.parametrize("raw", ["X", "", "26 КАД", "26КАД!", "группа"])
def test_is_valid_group_rejects(raw: str) -> None:
    assert start_h.is_valid_group(raw) is False


def test_suggest_groups_finds_close_match() -> None:
    """Опечатка «26КД» подсказывает «26КАД»."""
    available = ["26КАД", "26МЭГ", "26Р", "26С1"]
    suggestions = start_h.suggest_groups("26КД", available)
    assert "26КАД" in suggestions
    assert len(suggestions) <= 3


def test_suggest_groups_empty_when_nothing_close() -> None:
    assert start_h.suggest_groups("99XXX", ["26КАД", "26МЭГ"]) == []


# --- render_day ---

def _lesson(**over: object) -> dict:
    """План пары с возможностью переопределить поля."""
    base = {
        "para_number": 1, "subject": "ОД.07 Математика",
        "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
        "week_type": "", "time_range": "09:00-10:35",
        "is_substitution": False, "is_cancelled": False,
        "is_self_study": False,
    }
    return {**base, **over}


def test_render_day_header() -> None:
    """Шапка: день, дата, группа, число → чётность."""
    text = sched.render_day("26КАД", date(2026, 9, 22), [_lesson()])
    assert "📅 <b>Вторник, 22.09.2026</b>" in text
    assert "🎓 Группа: <b>26КАД</b>" in text
    assert "🗓 Число: 22 → <b>Чет</b>" in text


def test_render_day_header_odd() -> None:
    text = sched.render_day("26КАД", date(2026, 9, 23), [_lesson()])
    assert "🗓 Число: 23 → <b>нечет</b>" in text


def test_render_day_empty() -> None:
    """Пустой день — дружелюбная строка."""
    text = sched.render_day("26КАД", date(2026, 9, 28), [])
    assert "🎉 <b>Пар нет</b>" in text
    assert "Отдыхай или закрой хвосты по дедлайнам." in text


def test_render_day_card_format() -> None:
    """Карточка пары: номер, предмет, преподаватель, кабинет, время."""
    text = sched.render_day("26КАД", date(2026, 9, 22), [_lesson()])
    assert "📚 <b>1 пара</b>" in text
    assert "<b>ОД.07 Математика</b>" in text
    assert "👤 Кудрявцева Полина Алексеевна" in text
    assert "🚪 307А" in text
    assert "⏰ 09:00-10:35" in text


def test_render_day_icons() -> None:
    """Иконки: 🔁 замена, ❌ отмена, 📖 самостоятельная работа."""
    text = sched.render_day("26КАД", date(2026, 9, 22), [
        _lesson(para_number=1, is_substitution=True,
                planned_subject="ОД.04 Обществознание"),
        _lesson(para_number=2, is_cancelled=True),
        _lesson(para_number=3, is_self_study=True),
    ])
    assert "🔁 <b>1 пара</b>" in text
    assert "❌ <b>2 пара</b>" in text
    assert "📖 <b>3 пара</b>" in text
    assert "<i>Было: ОД.04 Обществознание</i>" in text
    assert "<i>Пара отменена</i>" in text
    assert "<i>Самостоятельная работа</i>" in text


def test_lesson_icon_priority() -> None:
    """Отмена важнее замены; самостоятельная работа важнее замены."""
    assert sched.lesson_icon(_lesson(is_cancelled=True,
                                     is_substitution=True)) == sched.ICON_CANCELLED
    assert sched.lesson_icon(_lesson(is_self_study=True,
                                     is_substitution=True)) == sched.ICON_SELF_STUDY
    assert sched.lesson_icon(_lesson(is_substitution=True)) == sched.ICON_SUBSTITUTION
    assert sched.lesson_icon(_lesson()) == sched.ICON_PLANNED


def test_render_day_escapes_html() -> None:
    """Опасные символы из БД экранируются (защита от сломанной разметки)."""
    text = sched.render_day("26<КАД>", date(2026, 9, 22), [
        _lesson(subject="Математика <b>и</b> физика", teacher="Иванов & Ко",
                room="<307>"),
    ])
    assert "Математика &lt;b&gt;и&lt;/b&gt; физика" in text
    assert "Иванов &amp; Ко" in text
    assert "&lt;307&gt;" in text
    assert "26&lt;КАД&gt;" in text
    assert "<b>и</b>" not in text


def test_render_day_escape_matches_html_escape() -> None:
    """Экранирование совпадает с html.escape (единый подход)."""
    raw = 'предмет "в кавычках" & <тег>'
    assert escape(raw) in sched.render_day(
        "26КАД", date(2026, 9, 22), [_lesson(subject=raw)]
    )


def test_render_day_missing_optional_fields() -> None:
    """Пустые преподаватель/кабинет не ломают карточку."""
    text = sched.render_day("26КАД", date(2026, 9, 22), [
        _lesson(teacher="", room="", time_range=""),
    ])
    assert "👤" not in text
    assert "🚪" not in text
    assert "⏰" not in text
    assert "ОД.07 Математика" in text
    assert start_h.suggest_groups("99XXX", ["26КАД", "26МЭГ"]) == []