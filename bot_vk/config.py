"""Конфигурация VK-бота: VK_TOKEN и VK_GROUP_ID из переменных окружения.

При импорте модуля вызывается :func:`load_dotenv`: значения из ``.env`` в корне
проекта попадают в ``os.environ``. Уже заданные переменные окружения НЕ
перезаписываются (``override=False``) — в контейнере env имеет приоритет над
файлом. Тот же приём, что и в :mod:`bot.config`, поэтому оба бота читают один
и тот же ``.env``.

Проверка значений вынесена в :func:`validate` и вызывается из
:mod:`bot_vk.main_vk`, а не при импорте: тесты импортируют модуль без env.

Секреты (VK_TOKEN) не попадают в логи: значение не пишется ни в сообщения,
ни в repr.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# Корень проекта: bot_vk/config.py → bot_vk/ → корень.
PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

# .env необязателен: в контейнере переменные приходят из окружения.
load_dotenv(dotenv_path=ENV_FILE, override=False)

VK_TOKEN: str = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID: int = int(os.environ.get("VK_GROUP_ID", "0") or "0")


def validate() -> None:
    """Проверить обязательные переменные VK-бота.

    Вызывается из :mod:`bot_vk.main_vk` перед созданием ``Bot`` — чтобы ошибка
    была понятной («добавь в .env»), а не «Unauthorized» от VK API.

    Raises:
        RuntimeError: если ``VK_TOKEN`` пуст или ``VK_GROUP_ID`` равен нулю.
    """
    if not VK_TOKEN:
        raise RuntimeError(
            "VK_TOKEN не задан. Добавь в .env: VK_TOKEN=...")
    if not VK_GROUP_ID:
        raise RuntimeError(
            "VK_GROUP_ID не задан. Добавь в .env: VK_GROUP_ID=...")