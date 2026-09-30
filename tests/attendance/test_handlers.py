"""Тесты хендлеров посещаемости (этап 1) через реальный Dispatcher.

Bot подменяется заглушкой (см. ``tests/test_handlers_dispatch.FakeBot``),
поэтому проверяется вся цепочка: фильтры → FSM → сервис → БД → ответ.
"""

from datetime import date
from pathlib import Path

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import db
from bot.attendance import db as att_db
from bot.attendance import keyboards as kb
from bot.attendance import service
from bot.db import get_connection, transaction
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
STAROSTA_ID = 7001
STUDENT_ID = 7002
OTHER_ID = 7003


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с миграциями и группой в расписании (для fuzzy-поиска)."""
    c = get_connection(tmp_path / "att_handlers.db")
    apply_migrations(c)
    with transaction(c):
        for name in (GROUP, "26КАД"):
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
    return build_dispatcher(seed)


@pytest.fixture()
def dp(shared_dp, conn):
    """Диспетчер со свежим соединением и очищенной FSM."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _update(text: str, tg_id: int = STUDENT_ID) -> Update:
    """Апдейт с текстовым сообщением."""
    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = STUDENT_ID) -> Update:
    """Апдейт с нажатием inline-кнопки."""
    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


def _texts(bot: FakeBot) -> list[str]:
    return [m["text"] for m in bot.sent if m["text"]]


class AttendanceBot(FakeBot):
    """FakeBot, который дополнительно помнит ``show_alert`` у ответов.

    Обычный ``FakeBot`` сохраняет только текст метода, а тестам заглушек
    важно отличать всплывающее окно (``show_alert=True``) от обычного
    подтверждения нажатия.
    """

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        result = await super().__call__(method, request_timeout)
        if name == "AnswerCallbackQuery":
            self.sent[-1]["show_alert"] = getattr(method, "show_alert", None)
        return result


def _markup(bot: FakeBot):
    """Последняя inline-разметка из ответов."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return markup
    return None


def _buttons(bot: FakeBot) -> list[str]:
    """Тексты кнопок последней inline-клавиатуры."""
    markup = _markup(bot)
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def _alerts(bot: FakeBot) -> list[tuple[str, bool]]:
    """Тексты всплывающих ответов (AnswerCallbackQuery)."""
    return [
        (m.get("text") or "", bool(m.get("show_alert")))
        for m in bot.sent if m["method"] == "AnswerCallbackQuery"
    ]
def _alerts(bot: FakeBot) -> list[tuple[str, bool]]:
    """Тексты всплывающих ответов (AnswerCallbackQuery)."""
    return [
        (m.get("text") or "", bool(m.get("show_alert")))
        for m in bot.sent if m["method"] == "AnswerCallbackQuery"
    ]


async def _create_group(dp, bot, tg_id: int = STAROSTA_ID) -> str:
    """Пройти создание группы и вернуть код приглашения."""
    await dp.feed_update(bot, _callback(kb.CB_CREATE, tg_id))
    await dp.feed_update(bot, _update(GROUP, tg_id))
    await dp.feed_update(bot, _update("Абрамчик С.Г.", tg_id))
    return _last_code(bot)


def _last_code(bot: FakeBot) -> str:
    """Вытащить 6-значный код из последнего сообщения (<code>...</code>)."""
    import re

    for text in reversed(_texts(bot)):
        match = re.search(r"<code>(\d{6})</code>", text)
        if match:
            return match.group(1)
    return ""


# --- /start ---

async def test_start_shows_greeting(dp, conn) -> None:
    """/start показывает приветствие из texts.GREETING."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update("/start"))

    body = " ".join(_texts(bot))
    assert "Я бот расписания КСТ" in body
    assert "Моя группа" in body


async def test_start_after_group_short_greeting(dp, conn) -> None:
    """/start у зарегистрированного — короткое приветствие."""
    service.create_group(conn, GROUP, STUDENT_ID, "Иванов И.И.")
    bot = AttendanceBot()
    await dp.feed_update(bot, _update("/start"))

    assert any("С возвращением" in t for t in _texts(bot))


# --- «📊 Моя группа» ---

async def test_my_group_without_registration(dp, conn) -> None:
    """Без регистрации — меню с двумя кнопками."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _update(kb.BTN_MY_GROUP))

    assert any("не в группе" in t for t in _texts(bot))
    assert _buttons(bot) == ["🔢 Ввести код", "⭐ Создать группу"]


async def test_my_group_student_sees_student_kb(dp, conn) -> None:
    """Студент видит свою карточку и плитки студента."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update(kb.BTN_MY_GROUP))

    body = " ".join(_texts(bot))
    assert GROUP in body
    assert "Студентов: <b>2</b>" in body
    labels = " | ".join(_buttons(bot))
    assert "Отметиться на паре" in labels
    assert "Список группы" in labels
    # «Моя посещаемость» переехала в «Профиль» — в «Моей группе» её нет.
    assert "Моя посещаемость" not in labels


async def test_my_group_starosta_sees_starosta_kb(dp, conn) -> None:
    """Староста видит плитки старосты."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update(kb.BTN_MY_GROUP, STAROSTA_ID))

    labels = " | ".join(_buttons(bot))
    assert "Отметить вручную" in labels
    assert "Отчёт за неделю" in labels
    assert "Управление группой" in labels
    # У старосты нет «Отметиться на паре» — только ручная отметка.
    assert "Отметиться на паре" not in labels
# --- регистрация по коду ---

async def test_enter_code_flow(dp, conn) -> None:
    """Код → запрос ФИО → регистрация в группе."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_ENTER_CODE))
    assert any("Введи код приглашения" in t for t in _texts(bot))

    bot.sent.clear()
    await dp.feed_update(bot, _update(code))
    body = " ".join(_texts(bot))
    assert GROUP in body, "показываем найденную группу"
    assert "ФИО" in body, "просим ФИО"

    bot.sent.clear()
    await dp.feed_update(bot, _update("иванов иван иванович"))
    assert any("Ты в группе" in t for t in _texts(bot))

    student = att_db.get_student(conn, STUDENT_ID)
    assert student["group_name"] == GROUP
    assert student["full_name"] == "Иванов И.И."
    assert student["role"] == "student"


async def test_enter_code_unknown(dp, conn) -> None:
    """Неизвестный код — понятная ошибка, регистрации нет."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_ENTER_CODE))
    await dp.feed_update(bot, _update("999999"))

    assert any("Код не найден" in t for t in _texts(bot))
    assert att_db.get_student(conn, STUDENT_ID) is None


async def test_enter_code_bad_format(dp, conn) -> None:
    """Не 6 цифр — просим повторить."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_ENTER_CODE))
    await dp.feed_update(bot, _update("42"))

    assert any("6 цифр" in t for t in _texts(bot))


async def test_bad_name_reprompts(dp, conn) -> None:
    """ФИО без инициалов — просим ещё раз, студент не создан."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_ENTER_CODE))
    await dp.feed_update(bot, _update(code))
    bot.sent.clear()
    await dp.feed_update(bot, _update("Хренов"))

    assert any("разобрать ФИО" in t for t in _texts(bot))
    assert att_db.get_student(conn, STUDENT_ID) is None


# --- создание группы ---

async def test_create_group_flow(dp, conn) -> None:
    """«Создать группу» → название → ФИО → код выдан."""
    bot = AttendanceBot()
    code = await _create_group(dp, bot)

    assert len(code) == 6, "код должен быть выдан"

    body = " ".join(_texts(bot))
    assert "Ты староста группы" in body
    assert "Код приглашения" in body

    group = att_db.get_group(conn, GROUP)
    assert group["invite_code"] == code
    assert att_db.get_student(conn, STAROSTA_ID)["role"] == "starosta"


async def test_create_group_shows_new_code_button(dp, conn) -> None:
    """Под карточкой старосты есть кнопка новой генерации кода."""
    bot = AttendanceBot()
    await _create_group(dp, bot)

    assert "Сгенерировать новый" in " | ".join(_buttons(bot))


async def test_create_group_typo_shows_suggestions(dp, conn) -> None:
    """Ввод без найденной группы → показаны похожие кнопками.

    «24А» fuzzy не сводит ни к одной из двух групп расписания, но они
    попадают в подсказки — пользователь выбирает свою.
    """
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_CREATE))
    await dp.feed_update(bot, _update("24А"))

    assert any("нет в расписании" in t for t in _texts(bot))
    assert GROUP in " | ".join(_buttons(bot))


async def test_create_group_typo_close_match_goes_to_name(dp, conn) -> None:
    """«25КД» fuzzy сводит к «25КАД» — сразу просим ФИО (без подсказок)."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_CREATE))
    await dp.feed_update(bot, _update("25КД"))

    body = " ".join(_texts(bot))
    assert GROUP in body
    assert "ФИО" in body
    assert not any("нет в расписании" in t for t in _texts(bot))


async def test_create_group_taken(dp, conn) -> None:
    """Группа уже создана — просим код у старосты."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_CREATE, OTHER_ID))
    await dp.feed_update(bot, _update(GROUP, OTHER_ID))

    assert any("уже создана" in t for t in _texts(bot))
# --- назначение зама через бота ---

async def test_make_deputy_flow(dp, conn) -> None:
    """Староста выбирает студента из списка → тот становится замом."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_MAKE_DEPUTY, STAROSTA_ID))
    assert "Иванов И.И." in " | ".join(_buttons(bot)), "кандидат в кнопках"

    bot.sent.clear()
    await dp.feed_update(
        bot, _callback(f"{kb.CB_DEPUTY_PREFIX}{STUDENT_ID}", STAROSTA_ID)
    )

    assert any("Зам" in t for t in _texts(bot))
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "deputy"
    assert att_db.get_group(conn, GROUP)["deputy_tg_id"] == STUDENT_ID


async def test_make_deputy_requires_starosta(dp, conn) -> None:
    """Обычный студент не может открыть назначение зама."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_MAKE_DEPUTY, STUDENT_ID))

    _, show_alert = _alerts(bot)[-1]
    assert show_alert is True
    assert att_db.get_student(conn, STUDENT_ID)["role"] == "student"


async def test_make_deputy_no_candidates(dp, conn) -> None:
    """Кроме старосты никого нет — сообщаем, что некого назначать."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_MAKE_DEPUTY, STAROSTA_ID))

    assert any("Некого назначить" in t for t in _texts(bot))


async def test_make_deputy_command(dp, conn) -> None:
    """Команда /make_deputy показывает список кандидатов."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/make_deputy", STAROSTA_ID))

    assert "Иванов И.И." in " | ".join(_buttons(bot))


# --- новый код приглашения ---

async def test_new_code_changes_code(dp, conn) -> None:
    """Кнопка «Новый код» меняет код; старый перестаёт работать."""
    old_code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_NEW_CODE, STAROSTA_ID))

    assert any("Код обновлён" in t for t in _texts(bot))
    new_code = att_db.get_group(conn, GROUP)["invite_code"]
    assert new_code != old_code

    # Старый код не находит группу, новый — работает.
    assert att_db.get_group_by_code(conn, old_code) is None
    assert att_db.get_group_by_code(conn, new_code) is not None


async def test_new_code_denied_for_student(dp, conn) -> None:
    """Студент не может перегенерировать код."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_NEW_CODE, STUDENT_ID))

    _, show_alert = _alerts(bot)[-1]
    assert show_alert is True
    assert att_db.get_group(conn, GROUP)["invite_code"] == code

# --- заглушки этапа 2 ---

@pytest.mark.parametrize("callback_data", sorted(kb.STUB_CALLBACKS))
async def test_stub_buttons_alert(dp, conn, callback_data: str) -> None:
    """Заглушки отвечают всплывающим «Скоро — этап 2»."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(callback_data))

    alerts = _alerts(bot)
    assert alerts, "должен быть ответ на нажатие"
    text, show_alert = alerts[-1]
    assert "Скоро" in text and "этап 2" in text
    assert show_alert is True, "это всплывающее окно, а не тихое подтверждение"


@pytest.mark.parametrize("callback_data", sorted(kb.STUB_CALLBACKS))
async def test_stub_buttons_send_no_messages(dp, conn,
                                             callback_data: str) -> None:
    """Заглушки ничего не пишут в чат — только alert."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(callback_data))

    # Сообщения в чат идут методом SendMessage; текст самого alert лежит в
    # записи AnswerCallbackQuery и сообщением не является.
    chat_messages = [m for m in bot.sent if m["method"] == "SendMessage"]
    assert chat_messages == []


# --- список группы ---

async def test_group_list_show(dp, conn) -> None:
    """Список группы: студенты по алфавиту с ролями."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Яковлев Я.Я.")
    service.join_group(conn, STUDENT_ID, code, "Абрамов А.А.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_LIST, STUDENT_ID))

    body = " ".join(_texts(bot))
    assert "Список группы" in body
    assert "Абрамов А.А." in body
    assert "Яковлев Я.Я." in body
    assert "⭐ староста" in body, "роль старосты помечена"
    # Алфавитный порядок: Абрамов раньше Яковлева.
    assert body.index("Абрамов") < body.index("Яковлев")


async def test_group_list_requires_registration(dp, conn) -> None:
    """Без регистрации список не показывается."""
    bot = AttendanceBot()
    await dp.feed_update(bot, _callback(kb.CB_LIST, OTHER_ID))

    assert _alerts(bot), "должен быть alert про регистрацию"


# --- управление группой ---

async def test_manage_requires_starosta(dp, conn) -> None:
    """«Управление группой» закрыто для обычного студента."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_MANAGE, STUDENT_ID))

    text, show_alert = _alerts(bot)[-1]
    assert "только староста" in text.lower() or "старост" in text.lower()
    assert show_alert is True


async def test_manage_shows_code_for_starosta(dp, conn) -> None:
    """Староста видит экран управления с кодом."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _callback(kb.CB_MANAGE, STAROSTA_ID))

    body = " ".join(_texts(bot))
    assert "Управление группой" in body
    assert code in body
# --- /my_attendance и /report_week с блоком аттестации ---

async def test_my_attendance_shows_attestation_block(dp, conn) -> None:
    """/my_attendance показывает блок аттестации по предметам."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/my_attendance", STUDENT_ID))

    body = " ".join(_texts(bot))
    assert "📊 <b>Моя посещаемость</b>" in body
    assert "Аттестация по предметам" in body
    assert "Минимум 3 пары" in body


async def test_my_attendance_period_buttons(dp, conn) -> None:
    """Под сводкой — кнопки периода: обновить, прошлый месяц, назад, меню."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/my_attendance", STUDENT_ID))

    labels = " | ".join(_buttons(bot))
    assert "Обновить" in labels
    assert "Прошлый месяц" in labels
    assert "Назад" in labels


async def test_my_attendance_prev_month_recalculates(dp, conn) -> None:
    """«📅 Прошлый месяц» пересчитывает сводку на прошлый период."""
    from bot.attendance import attendance_texts as atext
    from bot.attendance import attestation_service as atts

    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/my_attendance", STUDENT_ID))
    current = " ".join(_texts(bot))
    bot.sent.clear()

    await dp.feed_update(bot, _callback(f"{kb.CB_ATT_PERIOD_PREFIX}-1",
                                        STUDENT_ID))

    previous = " ".join(_texts(bot))
    prev_title = atext.month_title(atts.period_for_month(offset_months=-1)[0])
    assert prev_title in previous, "заголовок прошлого месяца"
    assert current != previous


async def test_report_week_shows_attestation_blocks(dp, conn) -> None:
    """/report_week старосты: блоки риска и отличников."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/report_week", STAROSTA_ID))

    body = " ".join(_texts(bot))
    assert "Отчёт за неделю" in body
    assert "Под угрозой неаттестации" in body


async def test_report_week_buttons(dp, conn) -> None:
    """Под отчётом — «🔄 Обновить» и «🏠 Главное меню»."""
    service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/report_week", STAROSTA_ID))

    labels = " | ".join(_buttons(bot))
    assert "Обновить" in labels
    assert "Главное меню" in labels


async def test_report_week_denied_for_student(dp, conn) -> None:
    """Обычному студенту отчёт недоступен."""
    code = service.create_group(conn, GROUP, STAROSTA_ID, "Абрамчик С.Г.")
    service.join_group(conn, STUDENT_ID, code, "Иванов И.И.")
    bot = AttendanceBot()

    await dp.feed_update(bot, _update("/report_week", STUDENT_ID))

    assert any("только старосте" in t for t in _texts(bot))