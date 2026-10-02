"""Ключи приложения aiohttp, общие для :mod:`bot.web` и :mod:`bot.api`.

Вынесены в отдельный модуль, чтобы пакет API не импортировал ``bot.web``
(иначе получился бы цикл: ``web`` собирает приложение и зовёт ``api``).
:mod:`bot.web` реэкспортирует ``CONN_KEY`` / ``SETTINGS_KEY`` — тесты берут их
оттуда, и это те же самые объекты, что используются здесь.
"""
from __future__ import annotations

from aiohttp import web

# Соединение SQLite (общее с polling) и настройки приложения.
CONN_KEY: web.AppKey = web.AppKey("conn")
SETTINGS_KEY: web.AppKey = web.AppKey("settings")

# Telegram id авторизованного пользователя: заполняется ``auth_required``.
# RequestKey, а не AppKey: значение живёт в конкретном запросе, и aiohttp
# иначе предупреждает NotAppKeyWarning.
TG_ID_KEY: web.RequestKey = web.RequestKey("tg_id")

__all__ = ["CONN_KEY", "SETTINGS_KEY", "TG_ID_KEY"]