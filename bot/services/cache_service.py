"""Слой данных: скачивание источников и запись кэша (шаг 5).

Сервис скачивает страницы 24kst.ru, сохраняет их на диск в ``data/cache/``
и пишет разобранные данные в ``schedule_cache`` / ``substitutions_cache``.

Правила кэша (согласованы с владельцем проекта — не менять самовольно):

1. Кэш хранит **только последнюю успешную версию**, история не нужна.
2. Скачивание упало ⇒ кэш **НЕ перезаписывается**; в лог WARNING с текстом
   ошибки и возрастом кэша. Пользователь видит старые данные, не пустоту.
3. Файл скачался, но ``parse`` вернул ``[]`` ⇒ это **ошибка**, кэш не
   перезаписываем: пустой результат обычно значит изменение формата
   источника, а не «расписания нет».
4. Мета-ключ ``last_successful_fetch`` в таблице ``meta`` обновляется
   **только при успехе**.
5. Файлы источников лежат в ``data/cache/`` под фиксированными именами
   ``schedule.docx`` и ``substitutions.html``; рядом — ``<имя>.meta.json``
   с timestamp и HTTP-статусом для отладки.

Параметры сети: connect-timeout 30 с, общий таймаут 60 с, одна повторная
попытка через 5 с при сетевой ошибке (при HTTP 4xx повтора нет). Файл больше
5 МБ — WARNING, но скачивание продолжается.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from bot.config import (
    DEFAULT_CACHE_DIR,
    DOWNLOAD_CONNECT_TIMEOUT,
    DOWNLOAD_MAX_SIZE_WARN,
    DOWNLOAD_RETRY_DELAY_SECONDS,
    DOWNLOAD_TOTAL_TIMEOUT,
    SCHEDULE_PAGE_URL,
    SUBSTITUTIONS_PAGE_URL,
    TIMEZONE,
    USER_AGENT,
)
from bot.db import transaction
from bot.parsers.schedule import parse_docx
from bot.parsers.substitutions import parse_html

logger = logging.getLogger(__name__)

# Фиксированные имена файлов кэша (правило 5).
SCHEDULE_FILE = "schedule.docx"
SUBSTITUTIONS_FILE = "substitutions.html"

# Ключи таблицы meta.
META_LAST_SCHEDULE = "last_successful_fetch"
META_LAST_SUBSTITUTIONS = "last_successful_fetch_substitutions"

# Заголовки, которые не считаются «сетевой ошибкой» для повтора.
RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Ссылка на DOCX расписания ищем в HTML страницы (название меняется каждый
# семестр, поэтому хардкод запрещён — решение владельца).
DOCX_HREF_RE = re.compile(
    r"""href\s*=\s*["']([^"']+\.docx)["']""",
    re.IGNORECASE,
)


class DownloadError(Exception):
    """Не удалось скачать источник: сеть исчерпала попытки или HTTP 4xx."""


def extract_docx_url(html: str) -> str | None:
    """Найти ссылку на DOCX расписания в HTML страницы.

    Название файла меняется каждый семестр («…-1-семестр-2026-2027-уч.-год.docx»),
    поэтому ссылку ищем в разметке, а не хардкодим (решение владельца).

    Args:
        html: HTML страницы «Расписание занятий».

    Returns:
        Абсолютный URL первой найденной .docx-ссылки или None.

    Ссылка на кириллическое имя требует percent-кодирования при запросе —
    это делает вызывающий код через :func:`_quote_url`.
    """
    match = DOCX_HREF_RE.search(html)
    if match is None:
        return None
    return match.group(1).strip()


def _quote_url(url: str) -> str:
    """Percent-кодировать URL: в именах файлов источника есть кириллица."""
    from urllib.parse import quote

    return quote(url, safe=":/?&=#%")


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[bytes, int]:
    """Скачать URL с одной повторной попыткой при сетевой ошибке.

    Таймауты: 30 с на соединение, 60 с на чтение. HTTP 4xx — ошибка без
    повтора (повторять нечего). Сетевые сбои и 5xx/408/429 — одна повторная
    попытка через :data:`DOWNLOAD_RETRY_DELAY_SECONDS`.

    Args:
        session: открытая aiohttp-сессия.
        url: адрес (может содержать кириллицу).

    Returns:
        ``(содержимое, HTTP-статус)``.

    Raises:
        DownloadError: обе попытки не удались.
    """
    timeout = aiohttp.ClientTimeout(
        total=DOWNLOAD_TOTAL_TIMEOUT,
        connect=DOWNLOAD_CONNECT_TIMEOUT,
    )
    request_url = _quote_url(url)
    last_error: str = ""

    for attempt in (1, 2):
        try:
            async with session.get(request_url, timeout=timeout) as response:
                if 400 <= response.status < 500:
                    raise DownloadError(
                        f"HTTP {response.status} для {url} — повтор не выполняется"
                    )
                if response.status in RETRIABLE_STATUSES and attempt == 1:
                    last_error = f"HTTP {response.status}"
                    logger.warning(
                        "download retriable status, will retry",
                        extra={"url": url, "status": response.status},
                    )
                    await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
                    continue
                if response.status >= 400:
                    raise DownloadError(f"HTTP {response.status} для {url}")
                body = await response.read()
                return body, response.status
        except DownloadError:
            raise
        except Exception as exc:  # сетевые сбои: таймауты, обрывы, DNS
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                logger.warning(
                    "download failed, retrying once",
                    extra={"url": url, "error": last_error},
                )
                await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
                continue

    raise DownloadError(f"не удалось скачать {url}: {last_error}")
def cache_dir() -> Path:
    """Каталог кэша (``data/cache`` или ``CACHE_DIR`` из окружения)."""
    import os

    raw = os.environ.get("CACHE_DIR", "").strip() or DEFAULT_CACHE_DIR
    return Path(raw)


def _save_source(directory: Path, name: str, body: bytes,
                 status: int, url: str) -> None:
    """Сохранить файл источника и рядом ``<имя>.meta.json`` для отладки.

    Файлы лежат под фиксированными именами (правило 5): перезаписываются
    только при успешном скачивании, потому что вызываются лишь после успеха.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(body)

    meta = {
        "url": url,
        "status": status,
        "size": len(body),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetched_at_local": datetime.now(TIMEZONE).isoformat(),
    }
    (directory / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if len(body) > DOWNLOAD_MAX_SIZE_WARN:
        logger.warning(
            "source file is larger than expected",
            extra={"file": name, "size": len(body),
                   "limit": DOWNLOAD_MAX_SIZE_WARN},
        )


def get_meta(conn, key: str) -> str | None:
    """Прочитать значение из таблицы ``meta`` (None, если ключа нет)."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def _set_meta(conn, key: str, value: str) -> None:
    """Записать значение в ``meta`` (upsert)."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def cache_age_seconds(conn, key: str) -> float | None:
    """Возраст кэша в секундах по мете ``key``; None — меты нет.

    Используется в WARNING, когда скачивание упало: пользователю важно знать,
    насколько устаревшие данные он видит (правило 2).
    """
    raw = get_meta(conn, key)
    if not raw:
        return None
    try:
        fetched = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched).total_seconds()


def _now() -> str:
    """Текущий момент в ISO-8601 с поясом Asia/Krasnoyarsk."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")
def save_schedule(conn, lessons: list[dict]) -> int:
    """Заменить ``schedule_cache`` разобранным расписанием.

    Полная замена в одной транзакции (правило 1: только последняя успешная
    версия, история не нужна).

    Args:
        conn: соединение SQLite.
        lessons: результат :func:`bot.parsers.schedule.parse_docx`.

    Returns:
        Количество записанных занятий.
    """
    now = _now()
    with transaction(conn):
        conn.execute("DELETE FROM schedule_cache")
        conn.executemany(
            "INSERT INTO schedule_cache"
            " (group_name, day_of_week, para_number, subject, teacher, room,"
            "  week_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["group_name"], item["day_of_week"], item["para_number"],
                    item["subject"], item["teacher"], item["room"],
                    item["week_type"], now,
                )
                for item in lessons
            ],
        )
    return len(lessons)


def save_substitutions(conn, rows: list[dict]) -> int:
    """Заменить ``substitutions_cache`` разобранным листом замен.

    Returns:
        Количество записанных строк.
    """
    now = _now()
    with transaction(conn):
        conn.execute("DELETE FROM substitutions_cache")
        conn.executemany(
            "INSERT INTO substitutions_cache"
            " (group_name, date_iso, para, old_subject, new_subject, teacher,"
            "  room, is_cancelled, is_self_study, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["group"], row["date_iso"], row["para"],
                    row["old_subject"], row["new_subject"], row["teacher"],
                    row["room"], int(bool(row["is_cancelled"])),
                    int(bool(row["is_self_study"])), now,
                )
                for row in rows
            ],
        )
    return len(rows)


def _log_failure(conn, key: str, source: str, error: str) -> None:
    """WARNING с текстом ошибки и возрастом кэша (правило 2)."""
    age = cache_age_seconds(conn, key)
    age_text = f"{int(age)} с" if age is not None else "кэш отсутствует"
    logger.warning(
        f"источник {source} не обновлён: {error}; возраст кэша: {age_text}",
        extra={"source": source, "error": error, "cache_age_seconds": age},
    )
async def refresh_schedule(conn, session: aiohttp.ClientSession | None = None,
                           directory: Path | None = None) -> int:
    """Скачать DOCX расписания, разобрать и записать кэш.

    Порядок и правила:
    1. скачать страницу расписания, найти ссылку на .docx;
    2. скачать файл, сохранить как ``schedule.docx`` + мету;
    3. ``parse_docx``; **пустой результат — ошибка** (правило 3), кэш не трогаем;
    4. успех ⇒ заменить ``schedule_cache`` и обновить ``last_successful_fetch``.

    Args:
        conn: соединение SQLite.
        session: готовая aiohttp-сессия (в тестах — подменная).
        directory: каталог кэша (по умолчанию ``data/cache``).

    Returns:
        Количество занятий; -1 при неудаче (кэш не изменён).
    """
    target_dir = directory or cache_dir()
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    try:
        try:
            page, _ = await _fetch(session, SCHEDULE_PAGE_URL)
        except Exception as exc:
            _log_failure(conn, META_LAST_SCHEDULE, "расписание (страница)", repr(exc))
            return -1

        html = page.decode("utf-8", "replace")
        docx_url = extract_docx_url(html)
        if not docx_url:
            _log_failure(conn, META_LAST_SCHEDULE, "расписание",
                         "ссылка на .docx не найдена на странице")
            return -1

        try:
            body, status = await _fetch(session, docx_url)
        except Exception as exc:
            _log_failure(conn, META_LAST_SCHEDULE, "расписание (DOCX)", repr(exc))
            return -1

        if not body:
            _log_failure(conn, META_LAST_SCHEDULE, "расписание",
                         "скачан пустой файл")
            return -1

        # Файл сохраняем ДО парсинга: он нужен для разбора и для отладки.
        _save_source(target_dir, SCHEDULE_FILE, body, status, docx_url)

        lessons = parse_docx(target_dir / SCHEDULE_FILE)
        if not lessons:
            # Правило 3: пустой parse — это ошибка формата, а не «нет занятий».
            _log_failure(
                conn, META_LAST_SCHEDULE, "расписание",
                "parse_docx вернул пустой список (вероятно, изменился формат)",
            )
            return -1

        count = save_schedule(conn, lessons)
        with transaction(conn):
            _set_meta(conn, META_LAST_SCHEDULE, datetime.now(timezone.utc).isoformat())
        logger.info("schedule cache updated",
                    extra={"lessons": count, "source_url": docx_url})
        return count
    except Exception as exc:
        _log_failure(conn, META_LAST_SCHEDULE, "расписание (неожиданная ошибка)",
                     repr(exc))
        return -1
    finally:
        if owns_session:
            await session.close()


async def refresh_substitutions(conn,
                                session: aiohttp.ClientSession | None = None,
                                directory: Path | None = None) -> int:
    """Скачать лист замен, разобрать и записать кэш.

    Если на странице нет таблицы замен (например, замен на день нет) — это
    НЕ ошибка: разбор вернёт пустой список, и мы просто не трогаем кэш,
    сохраняя прежние данные.

    Args:
        conn: соединение SQLite.
        session: готовая aiohttp-сессия (в тестах — подменная).
        directory: каталог кэша (по умолчанию ``data/cache``).

    Returns:
        Количество строк замен; -1 при неудаче скачивания.
    """
    target_dir = directory or cache_dir()
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    try:
        try:
            body, status = await _fetch(session, SUBSTITUTIONS_PAGE_URL)
        except Exception as exc:
            _log_failure(conn, META_LAST_SUBSTITUTIONS, "лист замен", repr(exc))
            return -1

        if not body:
            _log_failure(conn, META_LAST_SUBSTITUTIONS, "лист замен",
                         "скачана пустая страница")
            return -1

        _save_source(target_dir, SUBSTITUTIONS_FILE, body, status,
                     SUBSTITUTIONS_PAGE_URL)

        rows = parse_html(target_dir / SUBSTITUTIONS_FILE)
        if not rows:
            _log_failure(
                conn, META_LAST_SUBSTITUTIONS, "лист замен",
                "parse_html вернул пустой список (нет таблицы замен "
                "или изменился формат)",
            )
            return -1

        count = save_substitutions(conn, rows)
        with transaction(conn):
            _set_meta(conn, META_LAST_SUBSTITUTIONS,
                      datetime.now(timezone.utc).isoformat())
        logger.info("substitutions cache updated", extra={"rows": count})
        return count
    except Exception as exc:
        _log_failure(conn, META_LAST_SUBSTITUTIONS, "лист замен (неожиданная ошибка)",
                     repr(exc))
        return -1
    finally:
        if owns_session:
            await session.close()


def get_schedule_for_group_day(conn, group_name: str,
                               day_of_week: int) -> list[dict]:
    """Занятия группы на день недели из ``schedule_cache`` (без чётности).

    Фильтр по чётности сознательно НЕ применяется здесь: это задача сервиса
    расписания (:func:`bot.services.schedule_service.get_lessons_for_day`).
    Здесь только чтение кэша.

    Args:
        conn: соединение SQLite.
        group_name: нормализованное имя группы («26КАД»).
        day_of_week: 1..7, понедельник = 1 (ISO).

    Returns:
        Список словарей: ``para_number`` (int), ``subject``, ``teacher``,
        ``room``, ``week_type`` — отсортирован по ``para_number``.
    """
    rows = conn.execute(
        "SELECT para_number, subject, teacher, room, week_type"
        " FROM schedule_cache"
        " WHERE group_name = ? AND day_of_week = ?"
        " ORDER BY para_number",
        (group_name, day_of_week),
    ).fetchall()
    return [dict(row) for row in rows]


def get_substitutions_for_group_date(conn, group_name: str,
                                     date_iso: str) -> list[dict]:
    """Замены группы на конкретную дату из ``substitutions_cache``.

    Args:
        conn: соединение SQLite.
        group_name: нормализованное имя группы.
        date_iso: дата в ISO (``YYYY-MM-DD``).

    Returns:
        Список словарей: ``para`` (int), ``old_subject``, ``new_subject``,
        ``teacher``, ``room``, ``is_cancelled`` (0/1), ``is_self_study``
        (0/1) — отсортирован по ``para``.
    """
    rows = conn.execute(
        "SELECT para, old_subject, new_subject, teacher, room,"
        " is_cancelled, is_self_study"
        " FROM substitutions_cache"
        " WHERE group_name = ? AND date_iso = ?"
        " ORDER BY para",
        (group_name, date_iso),
    ).fetchall()
    return [dict(row) for row in rows]