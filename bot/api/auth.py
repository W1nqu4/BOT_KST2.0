"""Авторизация Mini App через Telegram initData.

Подпись проверяет aiogram (:func:`safe_parse_webapp_init_data`) — свой HMAC не
пишем. Проверка подписи и есть защита: ``Origin`` подделать тривиально, а
подпись — нет, поэтому CORS в этом проекте открыт (см. ``bot.web``).

Отдельной проверки возраста ``auth_date`` здесь нет намеренно: Telegram сам
передаёт свежий initData при каждом открытии Web App, а жёсткий TTL ломал бы
уже открытую вкладку. Решение согласовано с владельцем проекта.
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any, Callable

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

from bot.api.keys import SETTINGS_KEY, TG_ID_KEY
from bot.api.responses import error_response

logger = logging.getLogger(__name__)

# Заголовок, в который фронт кладёт ``window.Telegram.WebApp.initData``.
INIT_DATA_HEADER = "X-Telegram-Init-Data"

# Код ошибки токена: настройки нет или она без токена.
_TOKEN_MISSING = "server_token_missing"

# ---------------------------------------------------------------------------
# DEV_TG_ID — ЛОКАЛЬНАЯ ЛАЗЕЙКА, НЕ ДЛЯ ПРОДА.
#
# При разработке фронт открывается в обычном браузере (localhost:3000), где
# ``window.Telegram`` отсутствует и initData пуст — API отвечает 401, и
# интерфейс не проверить. Если задана переменная окружения ``DEV_TG_ID`` И
# ``PUBLIC_BASE_URL`` указывает на localhost, авторизация подменяется этим
# Telegram id.
#
# Почему это безопасно (и когда перестанет быть):
#   - в проде PUBLIC_BASE_URL = https://kst24-kst24.up.railway.app, условие
#     localhost не выполняется → лазейка неактивна, даже если DEV_TG_ID задан;
#   - переменная НЕ коммитится (.env в .gitignore) и НЕ задаётся в Railway;
#   - в публичном репозитории её задавать нельзя.
#
# Убирать эту ветку перед публикацией кода — не обязательно, но помнить о ней
# нужно: одно условие «localhost» отделяет dev от прода.
# ---------------------------------------------------------------------------
DEV_TG_ID_ENV = "DEV_TG_ID"

# Хосты, на которых лазейка считается локальной разработкой.
_LOCAL_HOST_PREFIXES = ("http://localhost", "http://127.0.0.1")


def dev_tg_id(settings) -> int | None:
    """Telegram id из ``DEV_TG_ID``, если мы на localhost; иначе None.

    Args:
        settings: настройки приложения (нужен ``public_base_url``).

    Returns:
        id для подмены авторизации или None, если лазейка не активна.
    """
    raw = os.environ.get(DEV_TG_ID_ENV, "").strip()
    if not raw:
        return None

    base_url = str(getattr(settings, "public_base_url", "") or "")
    if not base_url.startswith(_LOCAL_HOST_PREFIXES):
        # Не localhost: значит прод. Молча игнорируем — лазейка выключена.
        return None

    try:
        return int(raw)
    except ValueError:
        logger.warning("DEV_TG_ID is not an integer — ignored",
                       extra={"value": raw})
        return None


def parse_tg_id(init_data: str, token: str) -> int | None:
    """Telegram id из подписанного initData или None.

    Args:
        init_data: строка ``window.Telegram.WebApp.initData``.
        token: токен бота (ключ проверки подписи).

    Returns:
        ``user.id`` при валидной подписи и наличии пользователя; иначе None.
    """
    if not init_data or not token:
        return None
    try:
        parsed = safe_parse_webapp_init_data(token=token, init_data=init_data)
    except Exception as exc:
        # Подпись не сошлась, строка битая, формат изменился — для клиента
        # это одно и то же: 401. Причина важна только в логе.
        logger.info("initData rejected", extra={"error": repr(exc)})
        return None
    if parsed.user is None:
        # Бывает у Web App, открытых из attachment menu: данных пользователя
        # в initData нет, и авторизовать некого.
        logger.info("initData without user")
        return None
    return int(parsed.user.id)


def auth_required(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Декоратор: пустить в эндпоинт только владельца валидного initData.

    Кладёт ``tg_id`` в ``request[TG_ID_KEY]`` — обработчик читает его оттуда
    и не парсит заголовок повторно.

    Returns:
        401 ``{'error': 'invalid_init_data'}``, если заголовка нет или подпись
        не сошлась. Ответы в едином формате: фронт показывает экран
        «откройте приложение из Telegram».
    """

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        settings = request.app[SETTINGS_KEY]
        token = getattr(settings, "bot_token", "") if settings else ""

        init_data = request.headers.get(INIT_DATA_HEADER, "")
        tg_id = parse_tg_id(init_data, token) if token else None

        if tg_id is None:
            # Лазейка для локальной разработки (см. блок про DEV_TG_ID выше).
            tg_id = dev_tg_id(settings)
            if tg_id is not None:
                logger.info("auth bypassed via DEV_TG_ID (localhost only)",
                            extra={"tg_id": tg_id})

        if tg_id is None and not token:
            # Подписи проверять нечем: это ошибка сервера, а не клиента.
            logger.warning("auth: bot token is not configured")
            return error_response(_TOKEN_MISSING, status=500)

        if tg_id is None:
            return error_response("invalid_init_data", status=401)

        request[TG_ID_KEY] = tg_id
        return await handler(request)

    return wrapper


def tg_id_of(request: web.Request) -> int:
    """Telegram id, положенный :func:`auth_required`.

    Raises:
        web.HTTPUnauthorized: если декоратора на маршруте не было — это
            ошибка сборки приложения, а не пользователя.
    """
    tg_id = request.get(TG_ID_KEY)
    if tg_id is None:
        raise web.HTTPUnauthorized(
            text='{"error": "invalid_init_data"}',
            content_type="application/json",
        )
    return int(tg_id)


__all__ = ["INIT_DATA_HEADER", "auth_required", "dev_tg_id", "parse_tg_id",
           "tg_id_of"]