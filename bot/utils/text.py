"""Хелперы текста: русские склонения числительных.

Нужны там, где в сообщение подставляется число и слово рядом с ним:
«3 прогула», но «5 прогулов»; «1 пара», «3 пары», «5 пар».
"""
from __future__ import annotations


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Выбрать форму слова по числу (русские правила склонения).

    Args:
        count: число.
        one: форма для 1, 21, 31 («прогул», «пара»).
        few: форма для 2..4, 22..24 («прогула», «пары»).
        many: форма для 0, 5..20, 25..30 («прогулов», «пар»).

    Returns:
        Нужная форма слова.
    """
    hundred = count % 100
    if 11 <= hundred <= 14:
        return many
    ten = count % 10
    if ten == 1:
        return one
    if 2 <= ten <= 4:
        return few
    return many