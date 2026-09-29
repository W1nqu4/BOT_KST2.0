"""Конфигурация приложения: env-переменные и доменные константы.

При импорте модуля вызывается :func:`load_dotenv`: значения из ``.env`` в корне
проекта попадают в ``os.environ``. Уже заданные переменные окружения НЕ
перезаписываются — в контейнере (Amvera/Docker) env имеет приоритет над файлом.

Все настройки читаются исключительно из переменных окружения (правило:
никаких хардкодов). Секреты (BOT_TOKEN) не попадают в repr и в логи.

Класс :class:`ConfigError` наследуется от :class:`RuntimeError`, поэтому
`except RuntimeError` в вызывающем коде тоже поймает ошибку конфигурации.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Корень проекта: bot/config.py → bot/ → корень.
PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
ENV_FILE: Final = PROJECT_ROOT / ".env"

# .env необязателен: в контейнере переменные приходят из окружения.
# override=False — реальные env-переменные приоритетнее файла.
load_dotenv(dotenv_path=ENV_FILE, override=False)


# Значение-заглушка из .env: если оно не заменено, ошибка будет понятной
# сразу при старте, а не «Unauthorized» от Telegram.
PLACEHOLDER_TOKEN: Final = "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER"


def normalize_base_url(url: str) -> str:
    """Привести базовый URL к каноническому виду.

    Единственное место с этой логикой: функцию используют и настройки
    (:meth:`Settings.from_env`), и :mod:`bot.services.ics_service`.
    Живёт в ``config``, чтобы не было циклического импорта
    (``ics_service`` уже импортирует константы из ``config``).

    - обрезает пробелы и завершающий слэш;
    - пустое значение остаётся пустым (сигнал «адрес не настроен»);
    - URL без схемы получает ``https://`` (клиенты календарей без схемы
      ссылку не открывают).

    Args:
        url: исходное значение (строка, ``None`` или мусор).

    Returns:
        URL со схемой без завершающего слэша, либо ``""``.
    """
    if not url:
        return ""
    cleaned = str(url).strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned
    return cleaned


class ConfigError(RuntimeError):
    """Ошибка конфигурации: env-переменная отсутствует или некорректна.

    Наследник :class:`RuntimeError` — ошибку ловит и ``except ConfigError``,
    и ``except RuntimeError``.
    """


# --- Доменные константы ---

# Часовой пояс техникума: UTC+7, все даты и время внутри бота — в нём.
TIMEZONE: Final = ZoneInfo("Asia/Krasnoyarsk")

# Псевдоним для обратной совместимости с ТЗ шага 10 (тот же пояс).
KRASNOYARSK: Final = TIMEZONE

# Эталонный понедельник: неделя, содержащая эту дату, считается ЧЁТНОЙ.
# Отсчёт чётности идёт непрерывно по 7 дней от него (НЕ ISO-номер недели!).
REFERENCE_MONDAY: Final = date(2026, 9, 21)

# Источники данных.
SCHEDULE_PAGE_URL: Final = "https://24kst.ru/студенту/расписание-занятий/"
SUBSTITUTIONS_PAGE_URL: Final = "https://24kst.ru/студенту/лист-замен/"

# Периодичность обновления кэшей, сек.
SCHEDULE_REFRESH_SECONDS: Final = 6 * 60 * 60     # расписание — раз в 6 часов
SUBSTITUTIONS_REFRESH_SECONDS: Final = 15 * 60    # замены — раз в 15 минут

# Параметры рассылки уведомлений.
NOTIFY_CONCURRENCY: Final = 25                    # asyncio.Semaphore(25)
NOTIFY_SEND_DELAY_SECONDS: Final = 0.05           # sleep между отправками

# Ежедневная рассылка расписания в чаты (шаг 5).
# Час отправки по Красноярску: вечером, когда лист замен уже опубликован,
# а студенты планируют следующий день. 18:00 выбрано как компромисс —
# замены на завтра к этому времени обычно есть, и расписание ещё актуально.
DAILY_SCHEDULE_HOUR: Final = 18

# Параметры скачивания источников (шаг 5).
DOWNLOAD_CONNECT_TIMEOUT: Final = 30          # таймаут установки соединения, сек
DOWNLOAD_TOTAL_TIMEOUT: Final = 60            # таймаут на всё чтение, сек
DOWNLOAD_RETRY_DELAY_SECONDS: Final = 5       # пауза перед единственной повторной попыткой
DOWNLOAD_MAX_SIZE_WARN: Final = 5 * 1024 * 1024   # 5 МБ: больше — WARNING, но качаем
USER_AGENT: Final = "Mozilla/5.0 (compatible; StudyBot/1.0)"

# Время пар по звонкам (источник: страница «Лист замен», таблица «ПАРА | ВРЕМЯ»).
# Первая пара — два урока с перерывом внутри (9:00–9:45, 9:50–10:35), поэтому
# в значении общий интервал пары. Пятая пара: консультации, лабораторные,
# курсовое проектирование.
#
# В СУББОТУ звонки другие: большая перемена короче (12:20–12:50), третья и
# четвёртая пары идут одним уроком, пятой пары нет вовсе. Поэтому расписание
# звонков разбито на два словаря: будни (Пн–Пт) и суббота.
BELL_TIMES_WEEKDAY: Final = {
    1: ("09:00", "10:35"),
    2: ("10:45", "12:20"),
    3: ("13:15", "14:50"),
    4: ("15:00", "16:35"),
    5: ("16:45", "18:05"),
}

BELL_TIMES_SATURDAY: Final = {
    1: ("09:00", "10:35"),
    2: ("10:45", "12:20"),
    3: ("12:50", "14:20"),   # одним уроком (большая перемена 12:20–12:50)
    4: ("14:30", "15:50"),   # одним уроком
    # 5 пары в субботу нет
}

# Перемены между парами (для справки и возможного вывода в UI).
# Будни: большая перемена 12:20–13:15; суббота: 12:20–12:50.
BELL_BREAKS_WEEKDAY: Final = {
    "big": ("12:20", "13:15"),
}
BELL_BREAKS_SATURDAY: Final = {
    "big": ("12:20", "12:50"),
}

# Обратная совместимость: BELL_TIMES — будничные звонки.
BELL_TIMES: Final = BELL_TIMES_WEEKDAY

# Задержка перезапуска упавшей фоновой задачи, сек.
TASK_RESTART_DELAY_SECONDS: Final = 10

# --- .ics-подписка (шаг 9) ---
# Горизонт генерации: сколько дней вперёд раскладываем занятия.
# RRULE с INTERVAL=2 здесь НЕ работает: чёт/нечет у нас идёт по числу месяца,
# а не по неделям, поэтому события генерируются конкретными датами.
ICS_HORIZON_DAYS: Final = 60
# За сколько минут до пары напоминать (VALARM).
ICS_ALARM_MINUTES_BEFORE: Final = 5
# Время напоминания о дедлайне накануне (местное, Asia/Krasnoyarsk).
ICS_DEADLINE_ALARM_TIME: Final = "20:00"
# Имя часового пояса в .ics (DTSTART указываем с TZID, не в UTC).
ICS_TIMEZONE = "Asia/Krasnoyarsk"
ICS_TIMEZONE_NAME: Final = "KRAT"
# Интервал, через который клиенту стоит перечитать подписку.
ICS_PUBLISHED_TTL: Final = "PT30M"

# Значения по умолчанию для необязательных env-переменных.
# Путь к кэшу источников (скачанные DOCX/HTML и их .meta.json).
DEFAULT_CACHE_DIR: Final = "data/cache"

# Официальный порт Amvera/локальной разработки.
DEFAULT_PORT: Final = 8080
DEFAULT_DB_PATH: Final = "data/bot.db"            # в контейнере: /app/data/bot.db
DEFAULT_LOG_LEVEL: Final = "INFO"


@dataclass(frozen=True, slots=True)
class Settings:
    """Настройки приложения, собранные из переменных окружения."""

    bot_token: str
    public_base_url: str
    port: int
    db_path: str
    admin_ids: tuple[int, ...]
    admin_chat_id: int | None
    cache_dir: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Прочитать os.environ и вернуть проверенные настройки.

        Raises:
            ConfigError: если обязательные поля отсутствуют или некорректны.
                В сообщении перечислены ВСЕ найденные проблемы сразу.
        """
        errors: list[str] = []

        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        if not bot_token or bot_token == PLACEHOLDER_TOKEN:
            errors.append(
                "BOT_TOKEN: обязательная переменная пуста или не задана. "
                "Получите токен у @BotFather и впишите в .env "
                "(шаблон: .env.example)"
            )

        raw_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
        public_base_url = normalize_base_url(raw_base_url)
        if not public_base_url:
            errors.append("PUBLIC_BASE_URL: обязательная переменная пуста или не задана")
        elif raw_base_url and not raw_base_url.startswith(("http://", "https://")):
            # Схема добавлена автоматически: бот запустится, но проверь адрес —
            # именно он попадает в ссылки .ics-подписки.
            logger.warning(
                "PUBLIC_BASE_URL без схемы — добавлен https://",
                extra={"raw": raw_base_url, "normalized": public_base_url},
            )

        port = _parse_port(os.environ.get("PORT", ""), errors)
        db_path = os.environ.get("DB_PATH", "").strip() or DEFAULT_DB_PATH
        admin_ids = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""), errors)
        admin_chat_id = _parse_optional_int(
            os.environ.get("ADMIN_CHAT_ID", ""), "ADMIN_CHAT_ID", errors
        )
        cache_dir = (
            os.environ.get("CACHE_DIR", "").strip() or DEFAULT_CACHE_DIR
        )
        log_level = os.environ.get("LOG_LEVEL", "").strip().upper() or DEFAULT_LOG_LEVEL

        if errors:
            raise ConfigError("Некорректная конфигурация:\n" + "\n".join(errors))

        return cls(
            bot_token=bot_token,
            public_base_url=public_base_url,
            port=port,
            db_path=db_path,
            admin_ids=admin_ids,
            admin_chat_id=admin_chat_id,
            cache_dir=cache_dir,
            log_level=log_level,
        )

    def __repr__(self) -> str:
        """Представление для логов: значение токена маскируется."""
        return (
            "Settings(bot_token='***', "
            f"public_base_url={self.public_base_url!r}, port={self.port!r}, "
            f"db_path={self.db_path!r}, admin_ids={self.admin_ids!r}, "
            f"admin_chat_id={self.admin_chat_id!r}, "
            f"cache_dir={self.cache_dir!r}, "
            f"log_level={self.log_level!r})"
        )


def _parse_port(raw: str, errors: list[str]) -> int:
    """Разобрать PORT; при отсутствии вернуть дефолт, при ошибке — записать её в errors."""
    raw = raw.strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        errors.append(f"PORT: ожидалось целое число, получено {raw!r}")
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        errors.append(f"PORT: значение вне диапазона 1..65535: {port}")
        return DEFAULT_PORT
    return port


def _parse_admin_ids(raw: str, errors: list[str]) -> tuple[int, ...]:
    """Разобрать ADMIN_IDS — целые числа через запятую; пустое значение допустимо."""
    raw = raw.strip()
    if not raw:
        return ()
    result: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            errors.append(f"ADMIN_IDS: {chunk!r} не является целым числом")
    return tuple(result)


def _parse_optional_int(raw: str, name: str, errors: list[str]) -> int | None:
    """Разобрать необязательную целочисленную переменную.

    Пустое значение — норма (возвращается None). Нечисловое значение попадает
    в ``errors`` как ошибка конфигурации.

    Args:
        raw: сырое значение переменной окружения.
        name: имя переменной — для текста ошибки.
        errors: список, куда добавляется описание проблемы.

    Returns:
        Целое число или None, если переменная не задана.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name}: ожидалось целое число, получено {raw!r}")
        return None
