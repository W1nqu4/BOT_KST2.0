"""Резервное копирование БД (шаг 12).

Копия делается **средствами SQLite** (``src.backup(dst)``), а не копированием
файла: в режиме WAL часть данных лежит в ``-wal``, и простое копирование
``bot.db`` даёт неполную или битую копию.

Ротация — по имени файла: ``bot_YYYYMMDD.db``, старше
:data:`BACKUP_KEEP_DAYS` дней удаляются.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import TIMEZONE

logger = logging.getLogger(__name__)

# Каталог бэкапов относительно файла БД.
BACKUP_DIR_NAME = "backups"

# Сколько дней храним копии.
BACKUP_KEEP_DAYS = 7

# Интервал цикла, часы.
BACKUP_INTERVAL_HOURS = 24

# Имя файла копии: bot_20260927.db
BACKUP_NAME_RE = re.compile(r"^bot_(\d{8})\.db$")


def backup_dir_for(db_path: str) -> Path:
    """Каталог бэкапов: рядом с файлом БД, в подпапке ``backups``."""
    return Path(db_path).resolve().parent / BACKUP_DIR_NAME


def backup_name(moment: datetime | None = None) -> str:
    """Имя файла копии для даты."""
    stamp = (moment or datetime.now(TIMEZONE)).strftime("%Y%m%d")
    return f"bot_{stamp}.db"


def make_backup(db_path: str, moment: datetime | None = None) -> Path | None:
    """Сделать копию БД через SQLite backup API.

    Args:
        db_path: путь к рабочей БД.
        moment: момент (для тестов).

    Returns:
        Путь к копии или None, если БД отсутствует или копирование упало.
    """
    source_path = Path(db_path)
    if not source_path.exists():
        logger.warning("backup skipped: db file not found",
                       extra={"db_path": str(db_path)})
        return None

    target_dir = backup_dir_for(db_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / backup_name(moment)

    source = None
    destination = None
    try:
        source = sqlite3.connect(str(source_path))
        destination = sqlite3.connect(str(target_path))
        with destination:
            source.backup(destination)
        logger.info(
            "backup done",
            extra={"path": str(target_path),
                   "size": target_path.stat().st_size},
        )
        return target_path
    except Exception as exc:
        logger.warning("backup failed", extra={"error": repr(exc)})
        return None
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def cleanup_old_backups(directory: Path,
                        keep_days: int = BACKUP_KEEP_DAYS,
                        moment: datetime | None = None) -> list[str]:
    """Удалить копии старше ``keep_days`` дней.

    Возраст берётся из имени файла (``bot_YYYYMMDD.db``), а не из метки
    времени: при копировании на другой носитель или из архива mtime может
    оказаться любым.

    Args:
        directory: каталог бэкапов.
        keep_days: сколько дней хранить.
        moment: база отсчёта (для тестов).

    Returns:
        Список имён удалённых файлов.
    """
    if not directory.exists():
        return []

    threshold = (moment or datetime.now(TIMEZONE)) - timedelta(days=keep_days)
    removed: list[str] = []

    for path in sorted(directory.iterdir()):
        match = BACKUP_NAME_RE.match(path.name)
        if match is None:
            continue
        try:
            created = datetime.strptime(match.group(1), "%Y%m%d").replace(
                tzinfo=threshold.tzinfo
            )
        except ValueError:
            continue
        if created < threshold:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                logger.warning("could not remove old backup",
                               extra={"file": path.name, "error": repr(exc)})

    if removed:
        logger.info("old backups removed", extra={"removed": removed})
    return removed


def backup_once(db_path: str, keep_days: int = BACKUP_KEEP_DAYS,
                moment: datetime | None = None) -> dict:
    """Один проход резервного копирования: копия + ротация.

    Returns:
        ``{'created': имя_или_None, 'removed': [...]}``.
    """
    path = make_backup(db_path, moment)
    removed = cleanup_old_backups(backup_dir_for(db_path), keep_days, moment)
    return {
        "created": path.name if path else None,
        "removed": removed,
    }


async def backup_loop(conn, settings, interval_hours: int = BACKUP_INTERVAL_HOURS,
                      keep_days: int = BACKUP_KEEP_DAYS) -> None:
    """Бесконечный цикл бэкапов (раз в сутки).

    Первый бэкап делается сразу при старте: если процесс упал и его
    перезапустили, ждать сутки до первой копии неправильно.

    Args:
        conn: соединение SQLite (не используется напрямую, но сохраняет
            единый интерфейс фоновых задач).
        settings: настройки (нужен ``db_path``).
        interval_hours: пауза между копиями.
        keep_days: сколько дней хранить копии.

    Raises:
        asyncio.CancelledError: при отмене задачи.
    """
    from bot.services.schedule_service import _sleep

    db_path = getattr(settings, "db_path", "")
    interval = interval_hours * 3600

    while True:
        try:
            if db_path:
                result = backup_once(db_path, keep_days)
                if result["created"]:
                    # ВАЖНО: ключи extra не должны совпадать с полями
                    # LogRecord («created», «message», «name» и т.п.) —
                    # иначе logging падает с KeyError.
                    logger.info(
                        "backup cycle finished",
                        extra={
                            "backup_file": result["created"],
                            "removed_count": len(result["removed"]),
                        },
                    )
            else:
                logger.warning("backup skipped: db_path is not configured")
        except asyncio.CancelledError:
            logger.info("backup_loop cancelled")
            raise
        except Exception:
            logger.exception("backup_loop iteration failed")
        await _sleep(interval)