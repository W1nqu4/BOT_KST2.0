"""Тесты хендлеров заявки на роль преподавателя: /teacher_apply и другие.

Сети нет: сообщения и FSM — заглушки с записью ответов. Диспетчер aiogram не
поднимаем: проверяем логику самих хендлеров, как в test_account_link_tg.py.
"""

import pytest

from bot import db
from bot.db import get_connection
from bot.handlers import teacher_apply
from bot.migrations import apply_migrations
from bot.parsers.teachers import PLACEHOLDER_MARK

TG_ID = 908084777
FIO = "Богатырева Ирина Павловна"
FIO_2 = "Виссарионова Анна Сергеевна"


class FakeUser:
    """Пользователь Telegram."""

    def __init__(self, user_id: int = TG_ID) -> None:
        self.id = user_id
        self.full_name = "Иван Петров"


class FakeMessage:
    """Сообщение-заглушка: запоминает ответы и правки."""

    def __init__(self, text: str = "", user_id: int = TG_ID) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.answers]


class FakeCallback:
    """Callback-заглушка: помнит ответы и сообщение под собой."""

    def __init__(self, data: str, user_id: int = TG_ID) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.bot = FakeBotForNotify()
        self.message = FakeMessage()
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answers.append((text, kwargs))

    @property
    def edited(self) -> list[str]:
        return [text for text, _ in self.message.edits]


class FakeBotForNotify:
    """Бот-заглушка: запоминает отправленные админам сообщения."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> bool:
        self.sent.append((chat_id, text))
        return True


class FakeState:
    """FSM-заглушка: состояние и data."""

    def __init__(self) -> None:
        self.state: object | None = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


class FakeSettings:
    """Настройки с одним админом (для уведомления)."""

    def __init__(self, admin_ids=(111222333,)) -> None:
        self.admin_ids = admin_ids


@pytest.fixture()
def conn(tmp_path):
    """БД со всеми миграциями."""
    connection = get_connection(tmp_path / "teacher_handlers.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def state() -> FakeState:
    return FakeState()
# --- /teacher_apply ---

async def test_apply_starts_fsm(conn, state) -> None:
    """Первая заявка: просим фамилию и переводим в шаг ввода."""
    message = FakeMessage("/teacher_apply")

    await teacher_apply.cmd_teacher_apply(message, state, conn)

    assert state.state is teacher_apply.TeacherApply.waiting_name
    assert any("Заявка на роль преподавателя" in t for t in message.texts)
    assert db.get_teacher(conn, TG_ID) is None, "заявки ещё нет"


async def test_apply_when_pending(conn, state) -> None:
    """Заявка уже отправлена: напоминаем и просим подождать."""
    db.apply_teacher(conn, TG_ID, FIO)
    message = FakeMessage("/teacher_apply")

    await teacher_apply.cmd_teacher_apply(message, state, conn)

    text = "\n".join(message.texts)
    assert "уже отправлена" in text
    assert FIO in text
    assert "/teacher_cancel" in text
    assert state.state is None, "FSM не запускаем"


async def test_apply_when_approved(conn, state) -> None:
    """Уже преподаватель: подсказываем команды."""
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, 111222333)
    message = FakeMessage("/teacher_apply")

    await teacher_apply.cmd_teacher_apply(message, state, conn)

    text = "\n".join(message.texts)
    assert "уже преподаватель" in text
    assert "/teacher" in text


async def test_apply_when_rejected(conn, state) -> None:
    """Заявка отклонена: отправляем к админу."""
    db.apply_teacher(conn, TG_ID, FIO)
    db.reject_teacher(conn, TG_ID, 111222333)
    message = FakeMessage("/teacher_apply")

    await teacher_apply.cmd_teacher_apply(message, state, conn)

    assert "отклонена" in "\n".join(message.texts)


# --- поиск ФИО ---

async def test_surname_shows_matches(conn, state) -> None:
    """Ввод фамилии показывает совпадения кнопками."""
    state.state = teacher_apply.TeacherApply.waiting_name
    message = FakeMessage("Богатырева")

    await teacher_apply.process_name(message, state, conn)

    assert message.answers, "должен быть ответ"
    _text, kwargs = message.answers[0]
    markup = kwargs.get("reply_markup")
    assert markup is not None, "должны быть кнопки выбора"
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert FIO in labels, labels
    assert state.data.get("teacher_matches"), "совпадения сохранены"


async def test_unknown_surname(conn, state) -> None:
    """Неизвестная фамилия — подсказка, без кнопок."""
    state.state = teacher_apply.TeacherApply.waiting_name
    message = FakeMessage("Пупкинехко")

    await teacher_apply.process_name(message, state, conn)

    assert "Не нашёл" in "\n".join(message.texts)
    assert state.data.get("teacher_matches") is None


async def test_cancel_word_clears_state(conn, state) -> None:
    """«Отмена» выходит из шага ввода."""
    state.state = teacher_apply.TeacherApply.waiting_name
    message = FakeMessage("Отмена")

    await teacher_apply.process_name(message, state, conn)

    assert state.state is None
    assert "Отменено" in "\n".join(message.texts)


async def test_command_in_state_asks_again(conn, state) -> None:
    """Команда вместо фамилии — просим фамилию, состояние не сбрасываем."""
    state.state = teacher_apply.TeacherApply.waiting_name
# --- выбор ФИО ---

async def test_pick_creates_application(conn, state) -> None:
    """Выбор ФИО из списка создаёт заявку."""
    state.data["teacher_matches"] = [FIO, FIO_2]
    callback = FakeCallback(f"{teacher_apply.CB_PICK_PREFIX}0")

    await teacher_apply.cb_pick_name(callback, state, conn,
                                     settings=FakeSettings())

    assert db.get_teacher(conn, TG_ID)["full_name"] == FIO
    assert "Заявка отправлена" in "\n".join(callback.edited)
    assert state.state is None


async def test_pick_notifies_admin(conn, state) -> None:
    """Админ получает уведомление о новой заявке."""
    state.data["teacher_matches"] = [FIO]
    callback = FakeCallback(f"{teacher_apply.CB_PICK_PREFIX}0")

    await teacher_apply.cb_pick_name(callback, state, conn,
                                     settings=FakeSettings())

    sent = callback.bot.sent
    assert sent, "уведомление должно уйти админу"
    chat_id, text = sent[0]
    assert chat_id == 111222333
    assert FIO in text
    assert str(TG_ID) in text


async def test_pick_without_admins_does_not_crash(conn, state) -> None:
    """Без ADMIN_IDS заявка всё равно создаётся, просто без уведомления."""
    state.data["teacher_matches"] = [FIO]
    callback = FakeCallback(f"{teacher_apply.CB_PICK_PREFIX}0")

    await teacher_apply.cb_pick_name(callback, state, conn,
                                     settings=FakeSettings(admin_ids=()))

    assert db.get_teacher(conn, TG_ID) is not None
    assert callback.bot.sent == []


async def test_pick_stale_index(conn, state) -> None:
    """Устаревший индекс: заявка не создаётся, показываем предупреждение."""
    state.data["teacher_matches"] = [FIO]
    callback = FakeCallback(f"{teacher_apply.CB_PICK_PREFIX}5")

    await teacher_apply.cb_pick_name(callback, state, conn,
                                     settings=FakeSettings())

    assert db.get_teacher(conn, TG_ID) is None
    assert any("устарел" in a[0] for a in callback.answers), callback.answers


async def test_pick_wrong_fio_reports_error(conn, state) -> None:
    """ФИО, которого нет в справочнике, отклоняется с понятным текстом."""
    state.data["teacher_matches"] = ["Пупкин Василий Иванович"]
    callback = FakeCallback(f"{teacher_apply.CB_PICK_PREFIX}0")

    await teacher_apply.cb_pick_name(callback, state, conn,
                                     settings=FakeSettings())

    assert db.get_teacher(conn, TG_ID) is None
    assert "справочник" in "\n".join(callback.edited)


# --- список кнопкой и отмена ---

async def test_cb_list_shows_names(conn, state) -> None:
    """Кнопка «Показать список» отдаёт ФИО кнопками."""
    callback = FakeCallback(teacher_apply.CB_LIST)

    await teacher_apply.cb_list_names(callback, state)

    assert callback.message.answers, "должен прийти список"
    _text, kwargs = callback.message.answers[0]
    markup = kwargs.get("reply_markup")
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any(PLACEHOLDER_MARK not in label for label in labels)
    assert not any(PLACEHOLDER_MARK in label for label in labels), \
        "ФИО «уточняется» не предлагаем"


async def test_cb_cancel_clears_state(conn, state) -> None:
    """Отмена по кнопке очищает состояние."""
    state.state = teacher_apply.TeacherApply.waiting_name
    callback = FakeCallback(teacher_apply.CB_CANCEL)

    await teacher_apply.cb_cancel(callback, state)

    assert state.state is None
    assert "Отменено" in "\n".join(callback.edited)


# --- статус и отмена командами ---

async def test_status_without_application(conn) -> None:
    """/teacher_status без заявки подсказывает подать."""
    message = FakeMessage("/teacher_status")

    await teacher_apply.cmd_teacher_status(message, conn)

    assert "нет заявки" in "\n".join(message.texts)


async def test_status_pending(conn) -> None:
    """/teacher_status показывает статус ожидания."""
    db.apply_teacher(conn, TG_ID, FIO)
    message = FakeMessage("/teacher_status")

    await teacher_apply.cmd_teacher_status(message, conn)

    text = "\n".join(message.texts)
    assert FIO in text
    assert "ожидает" in text


async def test_status_approved(conn) -> None:
    """/teacher_status показывает одобрение."""
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, 111222333)
    message = FakeMessage("/teacher_status")

    await teacher_apply.cmd_teacher_status(message, conn)

    assert "одобрена" in "\n".join(message.texts)


async def test_status_rejected(conn) -> None:
    """/teacher_status показывает отклонение."""
    db.apply_teacher(conn, TG_ID, FIO)
    db.reject_teacher(conn, TG_ID, 111222333)
    message = FakeMessage("/teacher_status")

    await teacher_apply.cmd_teacher_status(message, conn)

    assert "отклонена" in "\n".join(message.texts)


async def test_cancel_command_removes_pending(conn) -> None:
    """/teacher_cancel удаляет нерассмотренную заявку."""
    db.apply_teacher(conn, TG_ID, FIO)
    message = FakeMessage("/teacher_cancel")

    await teacher_apply.cmd_teacher_cancel(message, conn)

    assert "отменена" in "\n".join(message.texts)
    assert db.get_teacher(conn, TG_ID) is None


async def test_cancel_command_without_application(conn) -> None:
    """/teacher_cancel без заявки — понятный ответ."""
    message = FakeMessage("/teacher_cancel")

    await teacher_apply.cmd_teacher_cancel(message, conn)

    assert "нет активной заявки" in "\n".join(message.texts)


async def test_cancel_command_refuses_approved(conn) -> None:
    """/teacher_cancel не снимает одобренную роль."""
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, 111222333)
    message = FakeMessage("/teacher_cancel")

    await teacher_apply.cmd_teacher_cancel(message, conn)

    text = "\n".join(message.texts)
    assert "нельзя отменить" in text
    assert db.is_teacher(conn, TG_ID) is True