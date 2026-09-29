"""Тесты админ-роли посещаемости (этап 1).

Проверяются права (``settings.admin_ids``), админ-панель, списки, удаление
группы, рассылка, назначение старосты и личные алерты ``notify_admin``.
Telegram не используется: Bot подменяется заглушкой.
"""

from datetime import datetime
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Chat, Message, Update, User

from bot import db
from bot.attendance import admin_service
from bot.attendance import db as att_db
from bot.attendance import keyboards as kb
from bot.attendance import service
from bot.db import get_connection, transaction
from bot.main import Settings, build_dispatcher
from bot.migrations import apply_migrations
from bot.utils import monitoring as mon
from tests.attendance.test_handlers import AttendanceBot, _callback, _texts
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
OTHER_GROUP = "26КАД"
ADMIN_ID = 908084777          # создатель (@W1nqu4) — из ADMIN_IDS
STUDENT_ID = 555001
STUDENT_ID_2 = 555002
STUDENT_ID_3 = 555003


def _settings(admin_ids: tuple[int, ...] = (ADMIN_ID,)) -> Settings:
    """Настройки с админами (без хардкода в коде — только через settings)."""
    return Settings(
        bot_token="123:TEST", public_base_url="https://bot.example",
        port=8080, db_path="data/test.db", admin_ids=admin_ids,
        admin_chat_id=None, cache_dir="data/cache", log_level="INFO",
    )


def _update(text: str, tg_id: int = ADMIN_ID) -> Update:
    """Апдейт с текстовым сообщением."""
    user = User(id=tg_id, is_bot=False, first_name="Тест Админ")
    message = Message(
        message_id=1, date=datetime.now().date(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _buttons(bot: FakeBot) -> list[str]:
    """Тексты кнопок последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.text for row in markup.inline_keyboard for b in row]
    return []


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и группами в расписании."""
    c = get_connection(tmp_path / "admin.db")
    apply_migrations(c)
    with transaction(c):
        for name in (GROUP, OTHER_GROUP):
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, week_type, updated_at)"
                " VALUES (?, 1, 1, 'ОД.01', '', 'x')", (name,)
            )
    yield c
    c.close()


@pytest.fixture(scope="module")
def shared_dp():
    """Единственный диспетчер на модуль (роутеры — синглтоны)."""
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed, settings=_settings())


@pytest.fixture()
def dp(shared_dp, conn):
    """Диспетчер со свежим соединением, настройками и пустой FSM."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.workflow_data["settings"] = _settings()
    shared_dp.fsm.storage.storage.clear()
    mon.reset_notify_throttle()
    yield shared_dp
    shared_dp.workflow_data["settings"] = _settings()


@pytest.fixture()
def group_with_students(conn):
    """Группа GROUP со старостой и двумя студентами."""
    code = service.create_group(conn, GROUP, STUDENT_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID_2, code, "Иванов И.И.")
    service.join_group(conn, STUDENT_ID_3, code, "Петров П.П.")
    return code


# --- is_admin ---

def test_is_admin_true_for_creator() -> None:
    """Создатель входит в admin_ids."""
    assert admin_service.is_admin(ADMIN_ID, _settings()) is True


def test_is_admin_false_for_other() -> None:
    """Посторонний не админ."""
    assert admin_service.is_admin(12345, _settings()) is False


def test_is_admin_false_without_settings() -> None:
    """Без настроек админов нет — доступ закрыт."""
    assert admin_service.is_admin(ADMIN_ID, None) is False


def test_is_admin_false_when_list_empty() -> None:
    """Пустой admin_ids — не админ никто."""
    assert admin_service.is_admin(ADMIN_ID, _settings(admin_ids=())) is False
# --- /admin ---

async def test_admin_panel_for_creator(dp, conn, group_with_students) -> None:
    """/admin от создателя: панель со счётчиками и кнопками."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update("/admin"))

    body = " ".join(_texts(bot))
    assert "Админ-панель" in body
    assert "Групп: <b>1</b>" in body
    assert "Студентов: <b>3</b>" in body
    assert "Активных кодов: <b>1</b>" in body

    labels = " | ".join(_buttons(bot))
    assert "Все группы" in labels
    assert "Все студенты" in labels
    assert "Найти группу" in labels
    assert "Удалить группу" in labels
    assert "Рассылка" in labels


async def test_admin_denied_for_student(dp, conn) -> None:
    """/admin от обычного tg_id — «Команда не найдена»."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update("/admin", STUDENT_ID))

    body = " ".join(_texts(bot))
    assert "Команда не найдена" in body
    # Никаких подсказок о существовании админ-панели.
    assert "Админ-панель" not in body
    assert "Групп:" not in body


async def test_admin_denied_with_empty_admin_ids(dp, conn) -> None:
    """/admin при пустом admin_ids — «Команда не найдена»."""
    dp.workflow_data["settings"] = _settings(admin_ids=())
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/admin"))

    assert any("Команда не найдена" in t for t in _texts(bot))


async def test_admin_counts_are_correct(dp, conn,
                                        group_with_students) -> None:
    """Счётчики панели соответствуют содержимому БД."""
    second_code = service.create_group(conn, OTHER_GROUP, 999001, "Сидоров С.С.")
    service.join_group(conn, 999002, second_code, "Кузнецов К.К.")

    stats = admin_service.get_bot_stats(conn)
    assert stats == {"groups": 2, "students": 5, "active_codes": 2}


# --- списки ---

async def test_list_groups_shows_cards(dp, conn,
                                       group_with_students) -> None:
    """📋 Все группы: карточки с числом студентов, старостой и кодом."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_GROUPS, ADMIN_ID))

    body = " ".join(_texts(bot))
    assert "Группы (1)" in body
    assert GROUP in body
    assert "3 студ." in body
    assert "староста Абрамчик С.Г." in body
    assert group_with_students in body, "код группы показан"


async def test_list_groups_empty(dp, conn) -> None:
    """Без групп список пуст, но сообщение есть."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_GROUPS, ADMIN_ID))

    assert any("ни одной группы" in t for t in _texts(bot))


async def test_list_students_grouped(dp, conn, group_with_students) -> None:
    """👥 Все студенты: сгруппированы по группе, роли помечены."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_STUDENTS, ADMIN_ID))

    body = " ".join(_texts(bot))
    assert "Студенты (3)" in body
    assert GROUP in body
    assert "Абрамчик С.Г." in body
    assert "Иванов И.И." in body
    assert "⭐ староста" in body
    assert str(STUDENT_ID) in body, "tg_id показан"

    students = admin_service.list_all_students(conn)
    assert len(students) == 3
    assert all(s["group_name"] == GROUP for s in students)
# --- поиск группы ---

async def test_find_group_card(dp, conn, group_with_students) -> None:
    """🔍 Найти группу: карточка найденной группы."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_FIND, ADMIN_ID))
    await dp.feed_update(bot, _update(GROUP, ADMIN_ID))

    body = " ".join(_texts(bot))
    assert f"Группа {GROUP}" in body
    assert "Студентов: <b>3</b>" in body
    assert "Абрамчик С.Г." in body


async def test_find_group_not_found(dp, conn) -> None:
    """Несуществующая группа — понятная ошибка."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_FIND, ADMIN_ID))
    await dp.feed_update(bot, _update(OTHER_GROUP, ADMIN_ID))

    assert any("нет среди созданных" in t for t in _texts(bot))


# --- удаление группы ---

async def test_delete_group_with_confirmation(
        dp, conn, group_with_students) -> None:
    """Удаление с подтверждением: группа и студенты стёрты."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_DELETE, ADMIN_ID))
    await dp.feed_update(bot, _update(GROUP, ADMIN_ID))

    body = " ".join(_texts(bot))
    assert "Удалить группу" in body
    assert "<b>3</b> студ." in body
    assert "✅ Удалить" in " | ".join(_buttons(bot))

    bot.sent.clear()
    await dp.feed_update(
        bot, _callback(f"{kb.CB_ADM_DELETE_OK_PREFIX}{GROUP}", ADMIN_ID)
    )

    assert any("удалена" in t for t in _texts(bot))
    assert att_db.get_group(conn, GROUP) is None
    assert att_db.get_student(conn, STUDENT_ID) is None
    assert admin_service.list_all_students(conn) == []


async def test_delete_group_cancel_keeps_data(
        dp, conn, group_with_students) -> None:
    """Отмена удаления ничего не меняет."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_DELETE, ADMIN_ID))
    await dp.feed_update(bot, _update(GROUP, ADMIN_ID))
    bot.sent.clear()
    await dp.feed_update(bot, _callback(kb.CB_ADM_BACK, ADMIN_ID))

    assert att_db.get_group(conn, GROUP) is not None
    assert len(admin_service.list_all_students(conn)) == 3


def test_delete_group_unknown(conn) -> None:
    """Удаление несуществующей группы — ok=False."""
    result = admin_service.delete_group(conn, OTHER_GROUP)
    assert result == {"ok": False, "students_deleted": 0}


# --- рассылка ---

async def test_broadcast_flow(dp, conn, group_with_students) -> None:
    """Рассылка: текст → подтверждение → 3 студента получили."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_BROADCAST, ADMIN_ID))

    assert any("Рассылка" in t for t in _texts(bot))

    bot.sent.clear()
    await dp.feed_update(bot, _update("Скоро сессия!", ADMIN_ID))

    body = " ".join(_texts(bot))
    assert "Предпросмотр рассылки" in body
    assert "Получателей: <b>3</b>" in body

    bot.sent.clear()
    await dp.feed_update(bot, _callback(kb.CB_ADM_BC_SEND, ADMIN_ID))

    report = " ".join(_texts(bot))
    assert "Отправлено: <b>3</b>" in report
    assert "Ошибок: <b>0</b>" in report

    # Получатели рассылки — только студенты. Отчёт уходит админу отдельным
    # сообщением (тоже SendMessage), поэтому его исключаем по тексту.
    recipients = [m["chat_id"] for m in bot.sent
                  if m["method"] == "SendMessage"
                  and m.get("text") == "Скоро сессия!"]
    assert sorted(recipients) == sorted([STUDENT_ID, STUDENT_ID_2, STUDENT_ID_3])


async def test_broadcast_counts_forbidden(dp, conn,
                                          group_with_students) -> None:
    """TelegramForbiddenError у одного → ошибка в счётчике, остальные получили."""
    class BlockedBot(AttendanceBot):
        """Бот, у которого один студент заблокировал бота."""

        async def __call__(self, method, request_timeout=None):
            if (type(method).__name__ == "SendMessage"
                    and getattr(method, "chat_id", None) == STUDENT_ID_2):
                raise TelegramForbiddenError(method=method, message="blocked")
            return await super().__call__(method, request_timeout)

    bot = BlockedBot()

    await dp.feed_update(bot, _callback(kb.CB_ADM_BROADCAST, ADMIN_ID))
    await dp.feed_update(bot, _update("Тест", ADMIN_ID))
    bot.sent.clear()
    await dp.feed_update(bot, _callback(kb.CB_ADM_BC_SEND, ADMIN_ID))

    report = " ".join(_texts(bot))
    assert "Отправлено: <b>2</b>" in report
    assert "Ошибок: <b>1</b>" in report
    assert "Всего: <b>3</b>" in report


async def test_broadcast_all_forbidden(conn, group_with_students) -> None:
    """Все заблокировали → 0 отправлено, 3 ошибки (рассылка не падает)."""
    class AllBlockedBot(FakeBot):
        """Бот, которому никто не может написать."""

        async def __call__(self, method, request_timeout=None):
            if type(method).__name__ == "SendMessage":
                raise TelegramForbiddenError(method=method, message="blocked")
            return await super().__call__(method, request_timeout)

    result = await admin_service.broadcast_to_all(
        conn, AllBlockedBot(), "Тест", _settings(), throttle=False
    )

    assert result == {"sent": 0, "failed": 3, "total": 3}


async def test_broadcast_no_students(dp, conn) -> None:
    """Студентов нет — рассылать некому."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ADM_BROADCAST, ADMIN_ID))

    assert any("некому" in t for t in _texts(bot))


async def test_broadcast_to_all_direct(conn, group_with_students) -> None:
    """Прямой вызов broadcast_to_all: отчёт и доставка."""
    bot = FakeBot()
    result = await admin_service.broadcast_to_all(
        conn, bot, "Привет", _settings(), throttle=False
    )

    assert result == {"sent": 3, "failed": 0, "total": 3}
# --- /make_starosta ---

async def test_make_starosta_by_tg_id(dp, conn, group_with_students) -> None:
    """Админ назначает старосту по tg_id: роль и study_groups обновлены."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update(f"/make_starosta {GROUP} {STUDENT_ID_2}"))

    assert any("Староста группы" in t for t in _texts(bot))

    assert att_db.get_student(conn, STUDENT_ID_2)["role"] == "starosta"
    assert att_db.get_group(conn, GROUP)["starosta_tg_id"] == STUDENT_ID_2
    # Прежний староста стал обычным студентом.
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "student"


async def test_make_starosta_by_username_fails_with_hint(
        dp, conn, group_with_students) -> None:
    """@username в БД не хранится — просим tg_id числом."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update(f"/make_starosta {GROUP} @username"))

    body = " ".join(_texts(bot))
    assert "Не нашёл студента" in body
    assert "tg_id" in body
    # Ничего не изменилось.
    assert att_db.get_group(conn, GROUP)["starosta_tg_id"] == STUDENT_ID


async def test_make_starosta_without_args(dp, conn,
                                          group_with_students) -> None:
    """Без аргументов — подсказка формата, ничего не меняется."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update("/make_starosta"))

    body = " ".join(_texts(bot))
    assert "Формат" in body
    assert att_db.get_group(conn, GROUP)["starosta_tg_id"] == STUDENT_ID


async def test_make_starosta_unknown_group(dp, conn,
                                          group_with_students) -> None:
    """Несуществующая группа — ошибка."""
    bot = AttendanceBot()
    await dp.feed_update(
        bot, _update(f"/make_starosta {OTHER_GROUP} {STUDENT_ID_2}")
    )

    assert any("нет среди созданных" in t for t in _texts(bot))


async def test_make_starosta_denied_for_student(dp, conn,
                                                group_with_students) -> None:
    """Не-админу — «Команда не найдена», роль не меняется."""
    bot = AttendanceBot()
    await dp.feed_update(
        bot, _update(f"/make_starosta {GROUP} {STUDENT_ID_2}", STUDENT_ID_2)
    )

    body = " ".join(_texts(bot))
    assert "Команда не найдена" in body
    assert att_db.get_student(conn, STUDENT_ID_2)["role"] == "student"


async def test_make_starosta_from_other_group_denied(
        dp, conn, group_with_students) -> None:
    """Студент из другой группы не может стать старостой этой."""
    service.create_group(conn, OTHER_GROUP, 777001, "Чужой Ч.Ч.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update(f"/make_starosta {GROUP} 777001"))

    assert any("учится в группе" in t for t in _texts(bot))
    assert att_db.get_group(conn, GROUP)["starosta_tg_id"] == STUDENT_ID


def test_assign_starosta_helper(conn, group_with_students) -> None:
    """assign_starosta: ok для своего студента, отказ для чужого/неизвестного."""
    from bot.attendance import admin_handlers as ah

    assert ah.assign_starosta(conn, GROUP, STUDENT_ID_2)["ok"] is True
    assert ah.assign_starosta(conn, GROUP, 424242)["ok"] is False
    assert ah.assign_starosta(conn, OTHER_GROUP, STUDENT_ID)["ok"] is False
# --- notify_admin ---

@pytest.fixture(autouse=True)
def _clean_notify_throttle():
    """Сбрасывать кулдаун notify_admin перед каждым тестом.

    Кулдаун живёт в памяти процесса и по ключу «модуль + класс ошибки»,
    поэтому без сброса тесты влияли бы друг на друга.
    """
    mon.reset_notify_throttle()
    yield
    mon.reset_notify_throttle()


async def test_notify_admin_sends_to_first_admin() -> None:
    """notify_admin шлёт в личку первому админу из settings.admin_ids."""
    bot = FakeBot()
    sent = await mon.notify_admin(
        bot, _settings(), "Тестовая ошибка",
        error=RuntimeError("boom"), module="tests.module", now=1000.0,
    )

    assert sent is True
    chat_id, text = bot.sent[-1]["chat_id"], bot.sent[-1]["text"]
    assert chat_id == ADMIN_ID, "первый админ из admin_ids"
    assert "Ошибка в боте" in text
    assert "tests.module" in text
    assert "RuntimeError" in text
    assert "@W1nqu4" in text, "контакт для вопросов"
    assert "(Krasnoyarsk)" in text


async def test_notify_admin_without_admins() -> None:
    """Без admin_ids алерт не отправляется (и не падает)."""
    bot = FakeBot()
    sent = await mon.notify_admin(bot, _settings(admin_ids=()),
                                  "Тест", module="m", now=2000.0)

    assert sent is False
    assert bot.sent == []


async def test_notify_admin_throttles_same_error() -> None:
    """Повтор той же ошибки в течение 60 сек не отправляется."""
    bot = FakeBot()

    first = await mon.notify_admin(bot, _settings(), "Ошибка",
                                   error=RuntimeError("x"),
                                   module="m1", now=1000.0)
    second = await mon.notify_admin(bot, _settings(), "Ошибка",
                                    error=RuntimeError("x"),
                                    module="m1", now=1030.0)

    assert first is True
    assert second is False, "в пределах кулдауна не повторяем"
    assert len(bot.sent) == 1


async def test_notify_admin_allows_after_throttle() -> None:
    """После кулдауна та же ошибка снова отправляется."""
    bot = FakeBot()

    await mon.notify_admin(bot, _settings(), "Ошибка",
                           error=RuntimeError("x"), module="m1", now=1000.0)
    again = await mon.notify_admin(
        bot, _settings(), "Ошибка", error=RuntimeError("x"), module="m1",
        now=1000.0 + mon.NOTIFY_THROTTLE_SECONDS + 1,
    )

    assert again is True
    assert len(bot.sent) == 2


async def test_notify_admin_different_errors_both_sent() -> None:
    """Разные ошибки отправляются независимо."""
    bot = FakeBot()

    first = await mon.notify_admin(bot, _settings(), "A",
                                   error=RuntimeError("x"),
                                   module="m1", now=1000.0)
    second = await mon.notify_admin(bot, _settings(), "B",
                                    error=ValueError("y"),
                                    module="m1", now=1001.0)
    third = await mon.notify_admin(bot, _settings(), "C",
                                   error=RuntimeError("z"),
                                   module="m2", now=1002.0)

    assert first and second and third
    assert len(bot.sent) == 3, "разные ключи не мешают друг другу"


async def test_notify_admin_message_escapes_html() -> None:
    """Опасные символы в тексте и traceback экранируются."""
    text = mon.notify_admin_message("<script>alert(1)</script>",
                                    error=RuntimeError("<b>bad</b>"),
                                    module="m")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_notify_admin_traceback_is_truncated() -> None:
    """Traceback обрезается до лимита."""
    error = RuntimeError("x" * 2000)
    text = mon.notify_admin_message("t", error=error, module="m")
    assert len(text) < 2000, "сообщение не раздувается"


async def test_notify_admin_survives_send_error() -> None:
    """Ошибка доставки не роняет вызывающий код."""
    class BrokenBot(FakeBot):
        """Бот, который не может отправить сообщение."""

        async def __call__(self, method, request_timeout=None):
            raise RuntimeError("network down")

    assert await mon.notify_admin(BrokenBot(), _settings(), "t",
                                  module="m", now=3000.0) is False