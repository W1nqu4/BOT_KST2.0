"""Пакет HTTP API Mini App: авторизация, сериализация и эндпоинты.

Разделён по файлам, чтобы правила читались без разметки:

- :mod:`bot.api.keys` — ключи приложения aiohttp (соединение, настройки);
- :mod:`bot.api.auth` — проверка Telegram initData и декоратор ``auth_required``;
- :mod:`bot.api.serializers` — предмет/преподаватель/статус пары → JSON;
- :mod:`bot.api.routes` — сами эндпоинты ``/api/...``.

Логика расписания, дедлайнов и посещаемости НЕ дублируется: эндпоинты зовут
существующие сервисы бота.
"""
from __future__ import annotations

__all__ = ["keys", "auth", "responses", "serializers", "routes"]