"""Тесты нормализации имён групп (bot.parsers.groups).

Ключевое требование проекта: в КСТ есть ДВЕ РАЗНЫЕ параллельные группы —
«26КАД» и «026КАД». Это не дубли и не опечатки сайта: у них разные
расписания, разные замены и разные студенты. Нормализация обязана
сохранять ведущий ноль и никогда не сводить эти группы друг к другу.

Регистр и пробелы/дефисы убираются, ведущая буква «О» заменяется на «0»
(в части источников год пишут буквой).
"""

import pytest

from bot.parsers.groups import normalize_group_name


@pytest.mark.parametrize(("raw", "expected"), [
    # Ведущий ноль сохраняется.
    ("26КАД", "26КАД"),
    ("026КАД", "026КАД"),
    ("26 кад", "26КАД"),
    ("026 кад", "026КАД"),
    ("26КАД ", "26КАД"),
    ("  26КАД  ", "26КАД"),
    ("26-КАД", "26КАД"),
    ("26КАД", "26КАД"),
    ("026КАД", "026КАД"),
    # Буква «О» в начале — это ноль.
    ("О26КАД", "026КАД"),
    ("O26KAD", "026KAD"),
    # Пробелы и дефисы внутри.
    ("26 С1", "26С1"),
    ("26МОСДР-1", "26МОСДР1"),
    ("25 КАД", "25КАД"),
    ("026 С", "026С"),
    # Нижний регистр → верхний.
    ("26кад", "26КАД"),
    ("026кад", "026КАД"),
    # Пустое.
    ("", ""),
    ("   ", ""),
])
def test_normalize_group_name(raw: str, expected: str) -> None:
    """Нормализация: регистр/пробелы чистятся, ведущий ноль остаётся."""
    assert normalize_group_name(raw) == expected


def test_26kad_and_026kad_are_different() -> None:
    """26КАД и 026КАД — РАЗНЫЕ ключи, они не равны друг другу."""
    assert normalize_group_name("26КАД") != normalize_group_name("026КАД")


def test_normalize_is_idempotent() -> None:
    """Повторная нормализация не меняет результат (обе группы стабильны)."""
    for raw in ("26КАД", "026КАД", "26 кад", "026 кад", "26МОСДР-1"):
        once = normalize_group_name(raw)
        assert normalize_group_name(once) == once


def test_leading_zero_is_never_stripped() -> None:
    """Ведущий ноль не срезается ни у одной группы."""
    for raw in ("026КАД", "025КАД", "024КПГ", "026ИМС", "024С"):
        assert normalize_group_name(raw).startswith("0"), \
            f"{raw!r} потеряла ведущий ноль"


def test_letter_o_becomes_zero_only_at_start() -> None:
    """Замена «О» → «0» работает только в начале имени.

    Буква «О» внутри названия — часть слова (например «26МОСДР»), её
    трогать нельзя.
    """
    assert normalize_group_name("О26КАД") == "026КАД"
    assert normalize_group_name("26МОСДР-1") == "26МОСДР1"
    assert "0" not in normalize_group_name("26МОСДР-1")[2:]


def test_case_and_space_variants_map_to_same_group() -> None:
    """Разные написания ОДНОЙ группы дают один ключ."""
    variants = ["26КАД", "26 кад", "26-КАД", "  26кад  ", "26КАД"]
    assert len({normalize_group_name(v) for v in variants}) == 1

    zero_variants = ["026КАД", "026 кад", "О26КАД", "026-КАД", "026кад"]
    assert len({normalize_group_name(v) for v in zero_variants}) == 1