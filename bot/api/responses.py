"""JSON-ответы Mini App: единая точка сериализации.

``ensure_ascii=False`` обязателен: без него кириллица уезжает в ``\\uXXXX``,
и ответ читается только машинно. Формат ошибок — ``{'error': 'код'}``: фронт
различает случаи по коду, а не по тексту сообщения.
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web


def json_response(payload: Any, status: int = 200) -> web.Response:
    """Отдать JSON с UTF-8 и без экранирования кириллицы.

    Args:
        payload: любой JSON-совместимый объект.
        status: HTTP-код ответа.

    Returns:
        Готовый ``web.Response`` с ``application/json``.
    """
    return web.json_response(
        payload, status=status,
        dumps=lambda data: json.dumps(data, ensure_ascii=False),
    )


def error_response(code: str, status: int = 400) -> web.Response:
    """Ответ-ошибка в едином формате ``{'error': 'код'}``.

    Args:
        code: машинный код (``group_not_set``, ``bad_date``, ...).
        status: HTTP-код (400/401/404/409).

    Returns:
        ``web.Response`` с JSON-телом ошибки.
    """
    return json_response({"error": code}, status=status)


__all__ = ["json_response", "error_response"]