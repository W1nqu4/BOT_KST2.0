"""Тесты админки (шаг 11): /stats, /broadcast, /reparse, /users, /deactivate.

Bot подменяется заглушкой. Проверяется и доступ (только админы), и то, что
посторонним админ-команды не подтверждают своего существования.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import db
from bot.db import get_connection, transaction
from bot.handlers import admin as adm
from bot.keyboards import reply as reply_kb
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from bot.services import cache_service
from tests.test_handlers_dispatch import FakeBot

ADMIN_ID = 1001
USER_ID = 2002
GROUP = "26КАД"


def _settings() -> Settings:
    return Settings(
        bot_token="123:TEST", public_base_url="https://bot.example",
        port=8080, db_path="data/test.db", admin_ids=(ADMIN_ID,),
        admin_chat_id=ADMIN_ID, cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int) -> Update:
    user = User(id=tg_id, is_bot=False, first_name=f"U{tg_id}")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и двумя пользователями."""
    c = get_connection(tmp_path / "admin.db")
    apply_migrations(c)
    with transaction(c):
        c.execute(
            "INSERT INTO users (tg_id, group_name, full_name, created_at)"
            " VALUES (?, ?, 'Иван', '2026-09-01T00:00:00+07:00')",
            (USER_ID, GROUP),
        )
    yield c
    c.close()


@pytest.fixture(scope="module")
def shared_dp():
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed, settings=_settings())


@pytest.fixture()
def dp(shared_dp, conn):
    shared_dp.workflow_data["conn"] = conn
    shared_dp.workflow_data["settings"] = _settings()
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


# --- доступ ---

def test_is_admin_filter_accepts_admin() -> None:
    """Фильтр пропускает админа и отсекает постороннего."""
    import asyncio as _asyncio

    flt = adm.IsAdmin()
    admin_msg = _update("/stats", ADMIN_ID).message
    user_msg = _update("/stats", USER_ID).message

    assert _asyncio.run(flt(admin_msg, _settings())) is True
    assert _asyncio.run(flt(user_msg, _settings())) is False


def test_is_admin_filter_handles_no_settings() -> None:
    import asyncio as _asyncio

    assert _asyncio.run(adm.IsAdmin()(_update("/stats", ADMIN_ID).message, None)) is False


async def test_stats_for_admin(dp, conn) -> None:
    """/stats от админа: отчёт со всеми ключевыми полями."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/stats", ADMIN_ID))

    texts = _texts(bot)
    assert len(texts) == 1
    body = texts[0]
    assert "📊 <b>Статистика</b>" in body
    assert "Всего:" in body
    assert "активных за 7 дней:" in body
    assert "По группам:" in body
    assert GROUP in body
    assert "Размер БД:" in body
    assert "Расписание: обновлено" in body
    assert "Замены: обновлено" in body


async def test_stats_for_non_admin_is_neutral(dp, conn) -> None:
    """От не-админа — нейтральный ответ, без намёка на существование команды."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/stats", USER_ID))

    texts = _texts(bot)
    assert len(texts) == 1
    assert "Команда не найдена" in texts[0]
    assert "Статистика" not in texts[0]
    assert "доступ" not in texts[0].lower()


async def test_all_admin_commands_neutral_for_non_admin(dp, conn) -> None:
    """Ни одна админ-команда не выдаёт себя постороннему."""
    bot = FakeBot()
    for command in ("/stats", "/reparse", "/broadcast", "/users 26КАД",
                    "/deactivate 1"):
        bot.sent.clear()
        await dp.feed_update(bot, _update(command, USER_ID))
        texts = _texts(bot)
        assert any("Команда не найдена" in t for t in texts), command


# --- /stats: содержимое ---

def test_build_stats_text_lists_groups(conn) -> None:
    """Топ групп попадает в отчёт."""
    with transaction(conn):
        for tg_id in (3001, 3002):
            conn.execute(
                "INSERT INTO users (tg_id, group_name, created_at)"
                " VALUES (?, ?, '2026-09-01T00:00:00+07:00')", (tg_id, GROUP),
            )
    body = adm.build_stats_text(conn, "data/test.db")
    assert f"{GROUP} — 3" in body


def test_build_stats_text_missing_db_file(conn) -> None:
    """Отсутствие файла БД не ломает отчёт."""
    body = adm.build_stats_text(conn, "нет/такого/файла.db")
    assert "нет файла" in body


def test_build_stats_text_history_empty(conn) -> None:
    """Пустая история замен — строка «📜 История замен: пусто»."""
    body = adm.build_stats_text(conn, "data/test.db")
    assert "📜 История замен: пусто" in body


def test_build_stats_text_history_filled(conn) -> None:
    """История есть — строка с записями, числом дней и диапазоном дат."""
    from bot import db

    db.save_substitution_history(conn, GROUP, "2026-09-28", [{
        "para": 2, "old_subject": "A", "new_subject": "B",
        "teacher": "T", "room": "1", "is_cancelled": False,
        "is_self_study": False,
    }])
    db.save_substitution_history(conn, GROUP, "2026-09-29", [{
        "para": 3, "old_subject": "A", "new_subject": "C",
        "teacher": "T", "room": "2", "is_cancelled": False,
        "is_self_study": False,
    }])

    body = adm.build_stats_text(conn, "data/test.db")

    assert "📜 История замен: <b>2</b> записей за <b>2</b> дн.," in body
    assert "с 2026-09-28 по 2026-09-29" in body


def test_count_active_users_since(conn) -> None:
    """Учёт глубины: пользователь 8 дней назад в недельное окно не попадает."""
    from datetime import datetime as _dt

    from bot import db as _db

    # Пользователь из фикстуры создан 01.09; база отсчёта — 02.09.
    recent = _db.count_active_users_since(
        conn, days=7, now="2026-09-02T12:00:00+07:00"
    )
    assert recent == 1, "вчерашний пользователь попадает в окно"

    # База отсчёта через 8 дней — уже вне окна.
    later = _db.count_active_users_since(
        conn, days=7, now="2026-09-09T12:00:00+07:00"
    )
    assert later == 0, "за 7 дней новых регистраций не было"


# --- /users ---

async def test_users_command_lists_users(dp, conn) -> None:
    """/users 26КАД показывает tg_id и имя."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26КАД", ADMIN_ID))

    body = _texts(bot)[0]
    assert GROUP in body
    assert str(USER_ID) in body
    assert "Иван" in body
    assert "Отключить" in body


async def test_users_command_normalizes_group(dp, conn) -> None:
    """«26 кад» и «26КАД» дают один результат."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26 кад", ADMIN_ID))
    assert str(USER_ID) in _texts(bot)[0]


async def test_users_command_empty_group(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 99XXX", ADMIN_ID))
    assert "нет активных пользователей" in _texts(bot)[0]


async def test_users_command_without_args(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users", ADMIN_ID))
    assert "Использование" in _texts(bot)[0]


async def test_users_command_truncates_after_limit(dp, conn) -> None:
    """Больше 50 пользователей → «и ещё N»."""
    with transaction(conn):
        for tg_id in range(3000, 3060):
            conn.execute(
                "INSERT INTO users (tg_id, group_name, full_name, created_at)"
                " VALUES (?, ?, 'Имя', '2026-09-01T00:00:00+07:00')",
                (tg_id, GROUP),
            )
    bot = FakeBot()
    await dp.feed_update(bot, _update("/users 26КАД", ADMIN_ID))

    body = _texts(bot)[0]
    assert "и ещё" in body
    assert "11" in body, "60 - 50 показанных = 10 новых + 1 исходный"
    assert len(body) < 4096
# --- /deactivate ---

async def test_deactivate_marks_user_inactive(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update(f"/deactivate {USER_ID}", ADMIN_ID))

    assert "отключён" in _texts(bot)[0]
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 0


async def test_deactivate_unknown_user(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/deactivate 999999", ADMIN_ID))
    assert "не найден" in _texts(bot)[0]


async def test_deactivate_without_args(dp, conn) -> None:
    bot = FakeBot()
    await dp.feed_update(bot, _update("/deactivate", ADMIN_ID))
    assert "Использование" in _texts(bot)[0]


# --- /broadcast ---

async def test_broadcast_flow_sends_and_reports(dp, conn) -> None:
    """/broadcast → текст → предпросмотр → подтверждение → отчёт."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (3003, '26КАД', '2026-09-01T00:00:00+07:00')"
        )
    bot = FakeBot()

    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    assert any("Пришли текст сообщения" in t for t in _texts(bot))

    bot.sent.clear()
    await dp.feed_update(bot, _update("Скоро сессия!", ADMIN_ID))
    preview = _texts(bot)[0]
    assert "Предпросмотр рассылки" in preview
    assert "Получателей: <b>2</b>" in preview
    assert "Скоро сессия!" in preview

    bot.sent.clear()
    await dp.feed_update(bot, _callback("bc:send", ADMIN_ID))

    # Двум пользователям ушло сообщение + отчёт админу.
    texts = _texts(bot)
    assert sum("Скоро сессия!" in t for t in texts) >= 1
    report = next(t for t in texts if "Рассылка завершена" in t)
    assert "Всего получателей: <b>2</b>" in report
    assert "Отправлено: <b>2</b>" in report
    assert "Ошибок: <b>0</b>" in report


async def test_broadcast_cancel_does_not_send(dp, conn) -> None:
    """Отмена на предпросмотре: ничего не отправлено."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    await dp.feed_update(bot, _update("Текст", ADMIN_ID))

    bot.sent.clear()
    await dp.feed_update(bot, _callback("bc:cancel", ADMIN_ID))

    assert any("отменена" in t for t in _texts(bot))
    assert not any("Текст" in t for t in _texts(bot))


async def test_broadcast_cancel_by_text_button(dp, conn) -> None:
    """Отмена текстовой кнопкой на шаге ввода."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/broadcast", ADMIN_ID))
    bot.sent.clear()
    await dp.feed_update(bot, _update(reply_kb.BTN_CANCEL, ADMIN_ID))
    assert any("отменена" in t for t in _texts(bot))


async def test_broadcast_deactivates_blocked(conn) -> None:
    """TelegramForbiddenError в рассылке → is_active = 0, отчёт считает."""
    from aiogram.exceptions import TelegramForbiddenError

    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (3004, '26КАД', '2026-09-01T00:00:00+07:00')"
        )

    class PartlyBlockedBot(FakeBot):
        """Блокирует только одного пользователя."""

        async def __call__(self, method, request_timeout=None):
            if getattr(method, "chat_id", None) == 3004:
                raise TelegramForbiddenError(method=method, message="blocked")
            return await super().__call__(method, request_timeout)

    report = await adm.run_broadcast(conn, PartlyBlockedBot(), "Привет",
                                     throttle=False)

    assert report["total"] == 2
    assert report["sent"] == 1
    assert report["blocked"] == 1
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = 3004"
    ).fetchone()["is_active"]
    assert active == 0


async def test_broadcast_counts_errors(conn) -> None:
    """Прочие ошибки считаются как failed, рассылка не срывается."""
    class FailingBot(FakeBot):
        async def __call__(self, method, request_timeout=None):
            raise RuntimeError("сеть")

    report = await adm.run_broadcast(conn, FailingBot(), "Привет",
                                     throttle=False)
    assert report["failed"] == report["total"]
    assert report["sent"] == 0
    # Ошибка отправки НЕ должна деактивировать пользователя.
    active = conn.execute(
        "SELECT is_active FROM users WHERE tg_id = ?", (USER_ID,)
    ).fetchone()["is_active"]
    assert active == 1


# --- /reparse ---

async def test_reparse_reports_counts(dp, conn, monkeypatch) -> None:
    """/reparse вызывает cache_service и сообщает числа, группы и время."""
    async def fake_schedule(connection, session=None, directory=None):
        return 1456

    async def fake_subs(connection, session=None, directory=None):
        return 78

    monkeypatch.setattr(cache_service, "refresh_schedule", fake_schedule)
    monkeypatch.setattr(cache_service, "refresh_substitutions", fake_subs)
    with transaction(conn):
        for name in ("25КАД", "26КАД", "26МЭГ"):
            conn.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 1, 1, 'ОД.01', 'Т.Т.', '1', '', 'x')", (name,),
            )

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    texts = _texts(bot)
    assert any("Принудительный перепарсинг" in t for t in texts)
    report = next(t for t in texts if "Расписание:" in t)
    assert "1456" in report
    assert "78" in report
    assert "<b>3</b> групп" in report, "число групп из кэша"
    assert "Заняло" in report, "время выполнения"
    assert "сек" in report


async def test_reparse_counts_groups_from_cache(dp, conn, monkeypatch) -> None:
    """Число групп считается по кэшу расписания, а не берётся из отчёта."""
    async def fake(connection, session=None, directory=None):
        return 10

    monkeypatch.setattr(cache_service, "refresh_schedule", fake)
    monkeypatch.setattr(cache_service, "refresh_substitutions", fake)
    with transaction(conn):
        for name in ("25КАД", "26КАД"):
            conn.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 1, 1, 'ОД.01', 'Т.Т.', '1', '', 'x')", (name,),
            )

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    report = next(t for t in _texts(bot) if "Расписание:" in t)
    assert "<b>2</b> групп" in report


async def test_reparse_denied_for_non_admin(dp, conn) -> None:
    """/reparse от не-админа — нейтральный ответ, без чисел."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", USER_ID))

    texts = _texts(bot)
    assert not any("перепарсинг" in t.lower() for t in texts)
    assert not any("Расписание:" in t for t in texts)


async def test_reparse_reports_exception(dp, conn, monkeypatch) -> None:
    """Исключение при обновлении показывается админу, а не роняет бота."""
    async def exploding(connection, session=None, directory=None):
        raise RuntimeError("источник недоступен")

    monkeypatch.setattr(cache_service, "refresh_schedule", exploding)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    texts = _texts(bot)
    report = next(t for t in texts if "Ошибка" in t)
    assert "источник недоступен" in report
    assert "Расписание:" not in report, "чисел при ошибке нет"


async def test_reparse_reports_errors(dp, conn, monkeypatch) -> None:
    """Отрицательный результат cache_service — «ошибка (кэш не изменён)»."""
    async def failing(connection, session=None, directory=None):
        return -1

    monkeypatch.setattr(cache_service, "refresh_schedule", failing)
    monkeypatch.setattr(cache_service, "refresh_substitutions", failing)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/reparse", ADMIN_ID))

    report = next(t for t in _texts(bot) if "Расписание:" in t)
    assert report.count("ошибка") == 2
    assert "Заняло" in report
    return [m["text"] for m in bot.sent if m["text"]]
# --- /teachers: модерация заявок преподавателей ---

TEACHER_FIO = "Богатырева Ирина Павловна"
TEACHER_FIO_2 = "Виссарионова Анна Сергеевна"


def _teacher_pending(conn, tg_id: int, full_name: str) -> None:
    """Создать заявку преподавателя напрямую в БД."""
    db.apply_teacher(conn, tg_id, full_name)


def _messages(bot: FakeBot) -> list[dict]:
    """Только исходящие СООБЩЕНИЯ (без ответов на callback)."""
    return [m for m in bot.sent if m["method"] in ("SendMessage", "EditMessageText",
                                                   "EditMessageReplyMarkup")]


def _alerts(bot: FakeBot) -> list[str]:
    """Тексты ответов на callback (``show_alert`` и подписи кнопок)."""
    return [m["text"] for m in bot.sent
            if m["method"] == "AnswerCallbackQuery" and m["text"]]


def _last_markup(bot: FakeBot):
    """Разметка последнего сообщения с клавиатурой."""
    for message in reversed(_messages(bot)):
        if message.get("reply_markup"):
            return message["reply_markup"]
    return None
async def test_teachers_without_requests(dp, conn) -> None:
    """/teachers без заявок сообщает, что их нет."""
    bot = FakeBot()
    await dp.feed_update(bot, _update("/teachers", ADMIN_ID))

    text = "\n".join(_texts(bot))
    assert "Заявок преподавателей нет" in text
    assert "/teacher_apply" in text


async def test_teachers_lists_pending_with_buttons(dp, conn) -> None:
    """/teachers с двумя заявками: список и по кнопке на каждую."""
    _teacher_pending(conn, 555001, TEACHER_FIO)
    _teacher_pending(conn, 555002, TEACHER_FIO_2)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/teachers", ADMIN_ID))

    text = "\n".join(_texts(bot))
    assert TEACHER_FIO in text
    assert TEACHER_FIO_2 in text
    assert "Ожидают (2)" in text

    markup = _last_markup(bot)
    assert markup is not None, "должны быть кнопки обработки"
    approve = [b for row in markup.inline_keyboard for b in row
               if b.callback_data.startswith(adm.CB_TEACHER_APPROVE_PREFIX)]
    assert len(approve) == 2, approve


async def test_teachers_shows_approved_section(dp, conn) -> None:
    """Одобренные показываются отдельной секцией."""
    _teacher_pending(conn, 555001, TEACHER_FIO)
    _teacher_pending(conn, 555002, TEACHER_FIO_2)
    db.approve_teacher(conn, 555002, ADMIN_ID)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/teachers", ADMIN_ID))

    text = "\n".join(_texts(bot))
    assert "Ожидают (1)" in text
    assert "Одобрено (1)" in text
    assert TEACHER_FIO_2 in text


async def test_teachers_button_only_for_pending(dp, conn) -> None:
    """Кнопка «одобрить» есть только у ожидающих заявок."""
    _teacher_pending(conn, 555001, TEACHER_FIO)
    _teacher_pending(conn, 555002, TEACHER_FIO_2)
    db.approve_teacher(conn, 555002, ADMIN_ID)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/teachers", ADMIN_ID))

    markup = _last_markup(bot)
    data = [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data.startswith(adm.CB_TEACHER_APPROVE_PREFIX)]
    assert data == [f"{adm.CB_TEACHER_APPROVE_PREFIX}555001"], data


async def test_teachers_neutral_for_non_admin(dp, conn) -> None:
    """Не-админ не видит список и не узнаёт о команде."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(bot, _update("/teachers", USER_ID))

    text = "\n".join(_texts(bot))
    assert "Команда не найдена" in text
    assert TEACHER_FIO not in text, "заявки постороннему не показываем"
    assert _last_markup(bot) is None, "кнопок постороннему тоже нет"
async def test_cb_approve_shows_card(dp, conn) -> None:
    """Кнопка заявки открывает карточку с двумя решениями."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_APPROVE_PREFIX}555001", ADMIN_ID)
    )

    text = "\n".join(_texts(bot))
    assert TEACHER_FIO in text
    assert "555001" in text

    markup = _last_markup(bot)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"{adm.CB_TEACHER_YES_PREFIX}555001" in data
    assert f"{adm.CB_TEACHER_NO_PREFIX}555001" in data
    assert adm.CB_TEACHER_REFRESH in data


async def test_cb_confirm_yes_approves_and_notifies(dp, conn) -> None:
    """Одобрение меняет статус и уведомляет преподавателя."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_YES_PREFIX}555001", ADMIN_ID)
    )

    assert db.is_teacher(conn, 555001) is True
    assert db.get_teacher(conn, 555001)["approved_by"] == ADMIN_ID

    to_teacher = [m for m in _messages(bot) if m["chat_id"] == 555001]
    assert to_teacher, "преподаватель должен получить уведомление"
    assert "одобрена" in to_teacher[0]["text"]

    assert "Одобрено" in _alerts(bot)


async def test_cb_confirm_no_rejects_and_notifies(dp, conn) -> None:
    """Отклонение меняет статус и оставляет заявку в истории."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_NO_PREFIX}555001", ADMIN_ID)
    )

    teacher = db.get_teacher(conn, 555001)
    assert teacher["status"] == db.TEACHER_REJECTED, "заявка не удалена"
    assert db.is_teacher(conn, 555001) is False

    to_teacher = [m for m in _messages(bot) if m["chat_id"] == 555001]
    assert to_teacher, "преподаватель должен получить уведомление"
    assert "отклонена" in to_teacher[0]["text"]


async def test_approve_processed_request_alerts(dp, conn) -> None:
    """Повторное открытие обработанной заявки: «уже обработана»."""
    _teacher_pending(conn, 555001, TEACHER_FIO)
    db.approve_teacher(conn, 555001, ADMIN_ID)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_APPROVE_PREFIX}555001", ADMIN_ID)
    )

    assert "обработана" in " ".join(_alerts(bot))
    assert _last_markup(bot) is None, "карточку повторно не показываем"


async def test_double_approve_does_not_pass(dp, conn) -> None:
    """Повторное одобрение не проходит (статус уже approved)."""
    _teacher_pending(conn, 555001, TEACHER_FIO)
    db.approve_teacher(conn, 555001, ADMIN_ID)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_YES_PREFIX}555001", ADMIN_ID)
    )

    assert "обработана" in " ".join(_alerts(bot))
    to_teacher = [m for m in _messages(bot) if m["chat_id"] == 555001]
    assert not to_teacher, "второе уведомление не отправляем"


async def test_cb_refresh_shows_list(dp, conn) -> None:
    """Кнопка «Обновить» отдаёт актуальный список."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(bot, _callback(adm.CB_TEACHER_REFRESH, ADMIN_ID))

    text = "\n".join(_texts(bot))
    assert "Заявки преподавателей" in text
    assert TEACHER_FIO in text


async def test_callbacks_refused_for_non_admin(dp, conn) -> None:
    """Не-админ не может одобрить заявку через callback."""
    _teacher_pending(conn, 555001, TEACHER_FIO)

    bot = FakeBot()
    await dp.feed_update(
        bot, _callback(f"{adm.CB_TEACHER_YES_PREFIX}555001", USER_ID)
    )

    assert db.is_teacher(conn, 555001) is False, "права не выданы"
# --- format_relative_time ---

def test_relative_time_just_now() -> None:
    """Меньше минуты — «только что»."""
    from bot.config import TIMEZONE

    moment = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    assert adm.format_relative_time(moment) == "только что"


def test_relative_time_minutes() -> None:
    """Минуты назад."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(minutes=5)).isoformat()
    assert adm.format_relative_time(moment) == "5 мин назад"


def test_relative_time_hours() -> None:
    """Часы назад."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(hours=3)).isoformat()
    assert adm.format_relative_time(moment) == "3 ч назад"


def test_relative_time_yesterday() -> None:
    """Вчера."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(hours=30)).isoformat()
    assert adm.format_relative_time(moment) == "вчера"


def test_relative_time_days() -> None:
    """Дни назад."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(days=3)).isoformat()
    assert adm.format_relative_time(moment) == "3 дн назад"


def test_relative_time_future_is_safe() -> None:
    """Момент из будущего не даёт отрицательное «-5 мин»."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) + timedelta(minutes=5)).isoformat()
    assert adm.format_relative_time(moment) == "только что"


def test_relative_time_naive_datetime() -> None:
    """Наивное время (без пояса) тоже обрабатывается."""
    moment = (datetime.now() - timedelta(minutes=10)).isoformat()
    assert adm.format_relative_time(moment) == "10 мин назад"


def test_relative_time_garbage() -> None:
    """Мусор возвращается как есть — лучше показать, чем упасть."""
    assert adm.format_relative_time("не дата") == "не дата"
    assert adm.format_relative_time("") == "—"
    assert adm.format_relative_time(None) == "—"


def test_relative_time_boundary_hour() -> None:
    """Ровно на границе часа переключаемся на часы."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(seconds=3600)).isoformat()
    assert adm.format_relative_time(moment) == "1 ч назад"


def test_relative_time_boundary_day() -> None:
    """Ровно на границе суток — «вчера»."""
    from bot.config import TIMEZONE

    moment = (datetime.now(TIMEZONE) - timedelta(seconds=86400)).isoformat()
    assert adm.format_relative_time(moment) == "вчера"