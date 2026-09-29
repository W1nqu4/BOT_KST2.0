"""Точка входа приложения.

Соединение с SQLite передаётся хендлерам через ``workflow_data``: aiogram
прокидывает эти значения в аргументы хендлеров по имени (``conn``). Одно
соединение на процесс: SQLite в WAL это выдерживает, а открывать новое
на каждый апдейт — лишние накладные расходы.

Надёжность (шаг 12):

- graceful shutdown по SIGINT/SIGTERM через :class:`asyncio.Event`
  (на Windows сигналы обрабатывает KeyboardInterrupt);
- автоперезапуск polling при сетевой ошибке: процесс не выходит, а
  пробует снова через :data:`POLLING_RESTART_DELAY` секунд;
- rate limit по ``tg_id``: при превышении сообщение молча игнорируется.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import closing
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonDefault, TelegramObject
from aiohttp import web

from bot.config import ConfigError, Settings
from bot.db import get_connection
from bot.handlers import all_routers
from bot.migrations import apply_migrations
from bot.services.backup_service import backup_loop
from bot.services.notify_service import (
    deadline_notify_loop,
    notify_substitutions_loop,
)
from bot.services.schedule_service import (
    refresh_schedule_loop,
    refresh_substitutions_loop,
)
from bot.services.daily_schedule_service import daily_schedule_loop
from bot.services.history_service import history_cleanup_loop
from bot.services.pin_service import unpin_after_lessons_loop
from bot.utils.logging_setup import setup_logging
from bot.utils.monitoring import health_loop, notify_admin
from bot.utils.security import RateLimiter
from bot.web import create_app

logger = logging.getLogger(__name__)

# Пауза перед повторным запуском polling после сетевой ошибки, сек.
POLLING_RESTART_DELAY = 10

# Команды, которые Telegram показывает в меню («/»).
BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="start", description="Начать / сменить группу"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="settings", description="Настройки уведомлений"),
    BotCommand(command="setup", description="Привязать чат к группе КСТ"),
    BotCommand(command="unsync", description="Отвязать чат от группы"),
    BotCommand(command="schedule", description="Расписание на сегодня в чат"),
    BotCommand(command="mygroup", description="Моя группа и посещаемость"),
)


async def reset_menu_button(bot) -> bool:
    """Сбросить кнопку меню Telegram к значению по умолчанию.

    Раньше у бота была кнопка-Web App («Расписание»), ведущая на старый URL:
    Mini App не разрабатывался, папки ``webapp/dist`` и ``/app/`` не существует,
    поэтому кнопка открывала 503. Расписание внутри бота работает через
    reply-клавиатуру и inline-кнопки — отдельный Web App не нужен.

    Кнопка задаётся в BotFather, но ``set_chat_menu_button`` с
    :class:`MenuButtonDefault` перекрывает её при каждом старте, поэтому
    ручная правка в BotFather не требуется.

    Args:
        bot: экземпляр :class:`aiogram.Bot`.

    Returns:
        True, если кнопка сброшена; False — если Telegram не ответил
        (не критично: бот продолжает работать, в логе остаётся предупреждение).
    """
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as exc:
        # Сетевые сбои и «menu button is not modified» не должны ронять старт.
        logger.warning("could not reset menu button", extra={"error": repr(exc)})
        return False
    logger.info("menu button reset to default")
    return True


class RateLimitMiddleware(BaseMiddleware):
    """Ограничение частоты сообщений: лишние молча отбрасываются.

    Пользователю не отвечаем «слишком часто»: это подсказывает, что лимит
    существует, и провоцирует обходить его. Молчание безопаснее.
    """

    def __init__(self, limiter: RateLimiter | None = None) -> None:
        """Создать middleware.

        Args:
            limiter: готовый ограничитель; по умолчанию создаётся новый.
        """
        self.limiter = limiter or RateLimiter()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Пропустить событие, если лимит не исчерпан."""
        user = data.get("event_from_user")
        if user is not None and not await self.limiter.allow(user.id):
            logger.info("rate limited", extra={"tg_id": user.id})
            return None
        return await handler(event, data)


def build_dispatcher(conn, reuse: bool = True,
                     settings: Settings | None = None) -> Dispatcher:
    """Собрать диспетчер: роутеры + соединение БД и настройки для хендлеров.

    Роутеры — модульные синглтоны, а aiogram запрещает прикреплять один
    Router к двум диспетчерам. В приложении диспетчер создаётся один раз,
    но тесты делают это многократно, поэтому при ``reuse=True`` повторные
    вызовы возвращают уже собранный диспетчер (и только обновляют ``conn``).
    Тесту, которому нужен чистый экземпляр, следует передать ``reuse=False``
    один раз за сессию.

    Args:
        conn: открытое соединение SQLite.
        reuse: переиспользовать ранее собранный диспетчер.

    Returns:
        Настроенный Dispatcher (без polling).
    """
    if reuse and _SHARED_DISPATCHER is not None:
        _SHARED_DISPATCHER.workflow_data["conn"] = conn
        if settings is not None:
            _SHARED_DISPATCHER.workflow_data["settings"] = settings
        _SHARED_DISPATCHER.fsm.storage.storage.clear()
        return _SHARED_DISPATCHER

    # Если роутеры уже привязаны к какому-то диспетчеру (тесты пересобирают
    # диспетчер после перезагрузки модулей), берём именно его: собрать второй
    # с теми же роутерами aiogram не даст, а пустой диспетчер бесполезен.
    for router in all_routers():
        attached = getattr(router, "parent_router", None)
        if attached is not None:
            attached.workflow_data["conn"] = conn
            if settings is not None:
                attached.workflow_data["settings"] = settings
            attached.fsm.storage.storage.clear()
            if reuse:
                globals()["_SHARED_DISPATCHER"] = attached
            return attached

    dp = Dispatcher(storage=MemoryStorage())
    for router in all_routers():
        dp.include_router(router)
    # Хендлеры получают соединение (``conn``) и настройки (``settings``).
    dp.workflow_data.update({"conn": conn, "settings": settings})
    if reuse:
        globals()["_SHARED_DISPATCHER"] = dp
    return dp


# Диспетчер, собранный первым вызовом build_dispatcher (для тестов).
_SHARED_DISPATCHER: Dispatcher | None = None


def build_bot(settings: Settings) -> Bot:
    """Создать Bot с HTML-разметкой по умолчанию."""
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def install_rate_limiter(dp: Dispatcher,
                         limiter: RateLimiter | None = None) -> RateLimiter:
    """Включить ограничение частоты сообщений.

    Middleware ставится отдельно от :func:`build_dispatcher`: сборка
    диспетчера — чистая операция (её переиспользуют тесты), а лимит частоты
    относится к запуску приложения. Один лимитер на диспетчер, поэтому при
    повторной установке создаётся новый (иначе счётчики предыдущего запуска
    продолжали бы действовать).

    Args:
        dp: диспетчер.
        limiter: готовый ограничитель (для тестов).

    Returns:
        Установленный ограничитель.
    """
    active = limiter or RateLimiter()
    middleware = RateLimitMiddleware(active)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    logger.info("rate limiter installed",
                extra={"max_per_minute": active._max})
    return active


async def start_web_server(conn, settings: Settings):
    """Поднять aiohttp-сервер (/health, /calendar/{token}.ics).

    Returns:
        ``web.AppRunner`` (закрывать вызывающему коду) или None при ошибке
        привязки порта — тогда уже открытые ресурсы освобождаются здесь.
    """
    web_app = create_app(conn, settings)
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", settings.port)
    try:
        await site.start()
    except OSError as exc:
        # Порт занят: сообщаем понятно и закрываем уже открытые ресурсы,
        # иначе aiogram оставит «Unclosed client session» в логах.
        logger.error(
            "web server failed to bind",
            extra={"port": settings.port, "error": repr(exc)},
        )
        await web_runner.cleanup()
        return None
    logger.info("web server started", extra={"port": settings.port})
    return web_runner


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Подписаться на SIGINT/SIGTERM для graceful shutdown.

    На Windows ``loop.add_signal_handler`` не поддерживается
    (``NotImplementedError``): там остановку даёт ``KeyboardInterrupt``,
    который перехватывает :func:`run_bot_sync`. Чтобы поведение было
    одинаковым, при недоступности подписки ставим ``signal.signal`` —
    он работает в главном потоке и на Windows.

    Args:
        shutdown_event: событие, которое выставляется при остановке.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, ValueError, AttributeError):
            logger.debug("loop.add_signal_handler unavailable",
                         extra={"signal": str(sig)})
            try:
                # Windows-путь: обработчик ставится на процесс.
                signal.signal(sig, lambda *_: shutdown_event.set())
            except (ValueError, OSError, AttributeError):
                logger.debug("signal.signal unavailable",
                             extra={"signal": str(sig)})


def build_background_tasks(conn, bot, settings) -> list[asyncio.Task]:
    """Создать фоновые задачи.

    Returns:
        Список задач: обновление расписания и замен, напоминания о
        дедлайнах, рассылка замен, бэкапы и проверка здоровья.
    """
    factories: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        ("refresh_schedule_loop", lambda: refresh_schedule_loop(conn)),
        ("refresh_substitutions_loop", lambda: refresh_substitutions_loop(conn)),
        ("deadline_notify_loop", lambda: deadline_notify_loop(conn, bot)),
        ("notify_substitutions_loop",
         lambda: notify_substitutions_loop(conn, bot)),
        ("backup_loop", lambda: backup_loop(conn, settings)),
        ("health_loop", lambda: health_loop(conn, bot, settings)),
        ("history_cleanup_loop", lambda: history_cleanup_loop(conn)),
        ("daily_schedule_loop", lambda: daily_schedule_loop(conn, bot)),
        ("unpin_after_lessons_loop",
         lambda: unpin_after_lessons_loop(conn, bot)),
    )
    return [asyncio.create_task(factory(), name=name)
            for name, factory in factories]


async def run_polling_with_restart(dp: Dispatcher, bot: Bot,
                                   shutdown_event: asyncio.Event) -> None:
    """Запустить polling, перезапуская его при сетевой ошибке.

    Процесс не выходит при сетевом сбое: Telegram может быть недоступен
    минуту-другую, и перезапуск дешевле, чем падение с последующим
    ручным подъёмом. Если во время паузы пришёл сигнал остановки — выходим.

    Args:
        dp: диспетчер.
        bot: объект Bot.
        shutdown_event: событие остановки.
    """
    while not shutdown_event.is_set():
        try:
            await dp.start_polling(bot)
            logger.info("polling stopped normally")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "polling crashed, restarting",
                extra={"delay": POLLING_RESTART_DELAY},
            )
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=POLLING_RESTART_DELAY
                )
                logger.info("shutdown during restart delay")
                return
            except asyncio.TimeoutError:
                continue


async def run_bot(settings: Settings, conn, shutdown_event=None) -> None:
    """Запустить polling, фоновые задачи и HTTP-сервер.

    Порядок остановки: сначала отменяются фоновые задачи, затем гасится
    HTTP-сервер, закрывается сессия бота и соединение с БД. Так не остаётся
    «Unclosed client session» и незакрытых портов.

    Args:
        settings: настройки.
        conn: соединение SQLite.
        shutdown_event: внешнее событие остановки (в тестах — готовое).
    """
    event = shutdown_event or asyncio.Event()
    if shutdown_event is None:
        install_signal_handlers(event)

    bot = build_bot(settings)
    dp = build_dispatcher(conn, settings=settings)
    install_rate_limiter(dp)

    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
    except Exception as exc:
        # Неверный токен, недоступный API и т.п.: сообщаем понятно и
        # закрываем сессию, иначе в логах остаётся «Unclosed client session».
        logger.error("could not register bot commands",
                     extra={"error": repr(exc)})
        # Критичная ошибка старта: сообщаем владельцу в личку, он узнаёт
        # раньше студентов. Ошибка доставки не мешает основной обработке.
        await notify_admin(
            bot, settings,
            "Не удалось зарегистрировать команды бота — проверь BOT_TOKEN",
            error=exc, module="main.set_my_commands",
        )
        await bot.session.close()
        raise SystemExit(4) from exc

    # Кнопка меню: убираем унаследованный Web App на старый URL (падал 503).
    # Ошибка здесь не критична — бот продолжит работать (см. reset_menu_button).
    await reset_menu_button(bot)

    web_runner = await start_web_server(conn, settings)
    if web_runner is None:
        await bot.session.close()
        raise SystemExit(3)

    tasks = build_background_tasks(conn, bot, settings)
    logger.info("background tasks started", extra={"count": len(tasks)})

    polling_task = asyncio.create_task(
        run_polling_with_restart(dp, bot, event), name="polling"
    )

    try:
        # Ждём либо сигнала остановки, либо завершения polling.
        stop_waiter = asyncio.create_task(event.wait(), name="shutdown_wait")
        done, _pending = await asyncio.wait(
            {polling_task, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter in done:
            logger.info("shutdown signal received")
        else:
            logger.info("polling finished, stopping")
            stop_waiter.cancel()
    finally:
        polling_task.cancel()
        for task in tasks:
            task.cancel()

        results = await asyncio.gather(
            polling_task, *tasks, return_exceptions=True
        )
        for task, result in zip([polling_task, *tasks], results):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error(
                    "task stopped with error",
                    extra={"task": task.get_name(), "error": repr(result)},
                )
                # Фоновая задача умерла — это тихий сбой, о нём надо сказать.
                await notify_admin(
                    bot, settings,
                    f"Фоновая задача {task.get_name()} остановилась из-за ошибки",
                    error=result, module=f"task.{task.get_name()}",
                )

        await web_runner.cleanup()
        logger.info("web server stopped")
        await bot.session.close()
        logger.info("bot stopped")


def main() -> None:
    """Запустить приложение."""
    setup_logging()
    logger.info("bot starting")

    # Конфигурация: бросает ConfigError (RuntimeError), если BOT_TOKEN или
    # PUBLIC_BASE_URL не заданы. Падать сразу при старте дешевле, чем внутри
    # обработчика Telegram.
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logger.error("configuration error", extra={"error": str(exc)})
        raise SystemExit(2) from exc

    logger.info(
        "configuration loaded",
        extra={
            "port": settings.port,
            "db_path": settings.db_path,
            "log_level": settings.log_level,
            "admin_ids_count": len(settings.admin_ids),
            "has_admin_chat_id": settings.admin_chat_id is not None,
        },
    )
    # Диагностика админ-доступа: без ADMIN_IDS админ-команды (/admin,
    # /make_starosta) и личные алерты notify_admin работать не будут.
    if settings.admin_ids:
        logger.info(
            "bot started",
            extra={
                "admin_username": "W1nqu4",
                "admin_ids_count": len(settings.admin_ids),
                "admin_ids": settings.admin_ids,
            },
        )
    else:
        logger.warning(
            "ADMIN_IDS is empty: admin commands and notify_admin disabled",
        )
    # Порядок логирования из .env применяется после чтения настроек.
    setup_logging(settings.log_level)

    with closing(get_connection(settings.db_path)) as conn:
        version = apply_migrations(conn)
        logger.info("migrations applied", extra={"schema_version": version})
        try:
            asyncio.run(run_bot(settings, conn))
        except KeyboardInterrupt:
            # Ctrl+C на Windows: graceful shutdown отработал внутри run_bot.
            logger.info("stopped by user")
        except SystemExit as exc:
            # run_bot сообщил о проблеме (занятый порт, неверный токен) —
            # не печатаем traceback: причина уже в логе.
            raise
        except Exception:
            logger.exception("bot stopped with error")
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
