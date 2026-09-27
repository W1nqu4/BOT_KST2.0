"""Настройка логирования: JSON-форматтер и setup_logging().

Логи — только через logging, одна JSON-запись на строку (удобно для
агрегаторов). print() в проекте запрещён. Токен и содержимое .env
никогда не передаются в логгер.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Стандартные поля LogRecord, которые не должны попадать в JSON как «допы».
_RESERVED_ATTRS: frozenset[str] = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class JsonFormatter(logging.Formatter):
    """Сериализует запись лога в одну JSON-строку (UTF-8, кириллица без экранирования)."""

    def format(self, record: logging.LogRecord) -> str:
        """Собрать JSON-payload из записи лога.

        Поля из ``extra={...}`` добавляются в payload как есть, если их имя
        не конфликтует со стандартными атрибутами LogRecord.
        """
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Настроить корневой логгер: JSON-записи в stdout.

    Args:
        level: уровень логирования (DEBUG / INFO / WARNING / ERROR).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Меньше шума от сторонних библиотек.
    for noisy in ("aiogram", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
