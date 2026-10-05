"""Тесты заявки преподавателя в VK-боте (подшаг 4.1).

E2E через настоящий диспетчер, как в ``tests/test_bot_vk_handlers.py``:
подменяется только транспорт (``api.messages``), апдейты ``message_new`` и
``message_event`` прогоняются через ``bot.process_event``.

Ключевое отличие VK от Telegram: заявка живёт в таблице ``teachers`` по
``tg_id``, поэтому подать её можно только со связанного аккаунта — это и
проверяется в первую очередь.

Сети нет: связка создаётся прямо в БД, уведомление админа — заглушка.
"""

import pytest

from bot import db
from bot.db import get_connection
from bot.migrations import apply_migrations
from bot.parsers.teachers import TEACHERS
from bot.services import teacher_notify
from bot_vk import keyboards, storage, texts, tg_bridge
from bot_vk.handlers import TeacherApplyState, register_handlers

GROUP_ID = 987654321
VK_ID = 777
TG_ID = 908084777
ADMIN_ID = 111222333

GROUP = "25КАД"

# День недели, на который кладём пары в кэш (понедельник есть в любой неделе).
CACHED_WEEKDAY = 1

# Реальные ФИО из справочника — не выдуманные (проверяется ниже).
FIO = "Богатырева Ирина Павловна"
SURNAME = "Богатырева"

# Фамилия, дающая больше одного совпадения: «Абрам» → Абрамов и Абрамчик.
# Нужна для проверки, что индекс выбирает правильное ФИО, а не первое.
AMBIGUOUS = "Абрам"
AMBIGUOUS_FIO = "Абрамчик Светлана Геннадьевна"


class FakeMessagesAPI:
    """Мини-``api.messages``: собирает отправленные сообщения."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def get_set_params(self, params: dict) -> dict:
        return {
            key: value
            for key, value in params.items()
            if value is not None and key not in ("self", "message", "ctx_api")
        }

    async def send(self, peer_ids=None, **kwargs) -> list:
        self.sent.append({**kwargs, "peer_ids": peer_ids})
        return [1]

    async def send_message_event_answer(self, **kwargs) -> int:
        """Ответ на inline-callback (snackbar и т.п.)."""
        self.sent.append({"event_answer": kwargs})
        return 1


class FakeAPI:
    """Заглушка API: вместо сети — запись отправленных сообщений."""

    def __init__(self) -> None:
        self.messages = FakeMessagesAPI()

    async def request(self, method: str, params: dict):
        raise AssertionError(f"неожиданный вызов VK API: {method}")


def make_event(text: str, vk_id: int = VK_ID) -> dict:
    """Сырой апдейт ``message_new`` с заданным текстом."""
    return {
        "type": "message_new",
        "group_id": GROUP_ID,
        "event_id": "e1",
        "v": "5.199",
        "object": {
            "message": {
                "id": 1,
                "date": 1700000000,
                "from_id": vk_id,
                "peer_id": vk_id,
                "conversation_message_id": 1,
                "text": text,
                "out": 0,
                "version": 0,
                "fwd_messages": [],
            },
            "client_info": {
                "button_actions": [],
                "keyboard": False,
                "inline_keyboard": False,
                "carousel": False,
                "lang_id": 0,
            },
        },
    }
def make_callback(payload: dict, vk_id: int = VK_ID) -> dict:
    """Сырой апдейт ``message_event`` — нажатие inline-кнопки.

    Так VK присылает нажатие callback-кнопки: индекс ФИО приходит в
    ``payload``, а не в тексте сообщения.
    """
    return {
        "type": "message_event",
        "group_id": GROUP_ID,
        "event_id": "e2",
        "v": "5.199",
        "object": {
            "user_id": vk_id,
            "peer_id": vk_id,
            "event_id": "ev1",
            "conversation_message_id": 5,
            "payload": payload,
        },
    }


@pytest.fixture()
def conn(tmp_path):
    """БД с миграциями и заполненным кэшем расписания."""
    connection = get_connection(tmp_path / "vk_teacher.db")
    apply_migrations(connection)

    connection.execute(
        "INSERT INTO schedule_cache"
        " (group_name, day_of_week, para_number, subject, teacher, room,"
        "  week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00')",
        (GROUP, CACHED_WEEKDAY, 1, "Математика", "Соколова Е.В.", "204", ""),
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def vk(conn):
    """Настоящий Bot с заглушкой транспорта и хендлерами VK."""
    from vkbottle.bot import Bot

    api = FakeAPI()
    bot = Bot(api=api)
    register_handlers(bot, conn)
    return bot, api, conn


async def send(vk, text: str) -> list[str]:
    """Прогнать текст через бота и вернуть список ответов."""
    bot, api, _conn = vk
    api.messages.sent.clear()
    await bot.process_event(make_event(text))
    return [item.get("message", "") for item in api.messages.sent]


async def press(vk, payload: dict) -> list[str]:
    """Нажать inline-кнопку и вернуть ответы (вместе с текстами snackbar)."""
    bot, api, _conn = vk
    api.messages.sent.clear()
    await bot.process_event(make_callback(payload))
    answers = []
    for item in api.messages.sent:
        if "message" in item:
            answers.append(item["message"])
        else:
            # snackbar: текст лежит внутри event_data (JSON-строка).
            answers.append(str(item.get("event_answer") or ""))
    return answers


def link(conn, vk_id: int = VK_ID, tg_id: int = TG_ID) -> None:
    """Создать связку VK ↔ TG прямо в БД (без кодов и сети)."""
    from bot.db import create_link_code, use_link_code

    code = create_link_code(conn, tg_id)
    result = use_link_code(conn, code, vk_id)
    assert result["ok"] is True, result


def keyboard_labels(keyboard: str) -> list[str]:
    """Подписи кнопок из JSON-строки клавиатуры VK."""
    import json

    data = json.loads(keyboard)
    return [
        button["action"]["label"]
        for row in data.get("buttons", [])
        for button in row
    ]


def button_payloads(keyboard: str) -> list[dict]:
    """Payload всех callback-кнопок клавиатуры (в порядке следования)."""
    import json

    data = json.loads(keyboard)
    return [
        button["action"].get("payload") or {}
        for row in data.get("buttons", [])
        for button in row
    ]
# --- справочник и предпосылки ---

def test_reference_names_exist() -> None:
    """ФИО для тестов есть в справочнике — тесты не выдумывают людей."""
    assert FIO in TEACHERS.values()
    assert AMBIGUOUS_FIO in TEACHERS.values()


def test_ambiguous_surname_matches_several() -> None:
    """«Абрам» даёт больше одного совпадения — нужно для проверки индекса."""
    from bot.services.teacher_names import match_names

    matches = match_names(AMBIGUOUS)
    assert len(matches) >= 2, matches
    assert AMBIGUOUS_FIO in matches


# --- /teacher_apply без связки ---

async def test_apply_without_link_asks_to_link(vk) -> None:
    """Без связки с TG заявка не создаётся: просим сначала /link."""
    _bot, _api, conn = vk

    answers = await send(vk, "/teacher_apply")

    assert any(texts.TEACHER_NEED_LINK == a for a in answers), answers
    assert db.list_pending_teachers(conn) == []


async def test_apply_without_link_does_not_set_state(vk) -> None:
    """Без связки состояние не выставляется: заявку подавать не с кем."""
    bot, _api, _conn = vk

    await send(vk, "/teacher_apply")

    assert await bot.state_dispenser.get(VK_ID) is None


# --- /teacher_apply со связкой ---

async def test_apply_with_link_starts_fsm(vk) -> None:
    """Со связкой и без заявки бот просит фамилию и переводит в FSM."""
    bot, _api, conn = vk
    link(conn)

    answers = await send(vk, "/teacher_apply")

    assert any(texts.TEACHER_ASK_NAME == a for a in answers), answers
    peer_state = await bot.state_dispenser.get(VK_ID)
    assert peer_state is not None
    assert peer_state.state == TeacherApplyState.waiting_name


async def test_surname_shows_inline_choices(vk) -> None:
    """Ввод фамилии: показываем совпадения кнопками и кладём список в FSM."""
    bot, api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, SURNAME)

    assert any(texts.TEACHER_CHOOSE_FIO == a for a in answers), answers
    keyboard = next(
        (item["keyboard"] for item in api.messages.sent if item.get("keyboard")),
        None,
    )
    assert keyboard is not None, "должна уйти клавиатура с ФИО"
    labels = keyboard_labels(keyboard)
    assert FIO in labels, labels
    assert keyboards.BTN_TEACHER_CANCEL in labels, labels

    peer_state = await bot.state_dispenser.get(VK_ID)
    assert FIO in (peer_state.payload.get("teacher_matches") or [])


async def test_single_match_creates_application(vk) -> None:
    """Выбор ФИО кнопкой: заявка создаётся с tg_id связанного аккаунта."""
    _bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    answers = await press(vk, {keyboards.TEACHER_CB_FIELD:
                               keyboards.TEACHER_CB_VALUE,
                               keyboards.TEACHER_CB_INDEX: 0})

    assert any(texts.TEACHER_APPLIED.format(fio=FIO) == a
               for a in answers), answers

    teacher = db.get_teacher(conn, TG_ID)
    assert teacher is not None
    assert teacher["full_name"] == FIO
    assert teacher["status"] == db.TEACHER_PENDING


async def test_pick_clears_state(vk) -> None:
    """После выбора ФИО состояние снимается — шаг завершён."""
    bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    await press(vk, {keyboards.TEACHER_CB_FIELD: keyboards.TEACHER_CB_VALUE,
                     keyboards.TEACHER_CB_INDEX: 0})

    assert await bot.state_dispenser.get(VK_ID) is None
async def test_pick_uses_linked_tg_id(vk) -> None:
    """Индекс выбирает нужное ФИО, а заявка уходит именно на связанный tg_id.

    «Абрам» даёт два совпадения: проверяем, что индекс 1 создаёт заявку с ФИО
    второго кандидата, а не всегда с первого.
    """
    _bot, _api, conn = vk
    link(conn, vk_id=VK_ID, tg_id=TG_ID)

    await send(vk, "/teacher_apply")
    await send(vk, AMBIGUOUS)
    await press(vk, {keyboards.TEACHER_CB_FIELD: keyboards.TEACHER_CB_VALUE,
                     keyboards.TEACHER_CB_INDEX: 1})

    teacher = db.get_teacher(conn, TG_ID)
    assert teacher is not None
    assert teacher["full_name"] == AMBIGUOUS_FIO


async def test_stale_index_rejected(vk) -> None:
    """Устаревшая кнопка (индекс вне списка): заявка не создаётся."""
    _bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    answers = await press(vk, {keyboards.TEACHER_CB_FIELD:
                               keyboards.TEACHER_CB_VALUE,
                               keyboards.TEACHER_CB_INDEX: 5})

    assert db.get_teacher(conn, TG_ID) is None
    assert any(texts.TEACHER_STALE_CHOICE in a for a in answers), answers


async def test_cancel_button_discards_step(vk) -> None:
    """«🔙 Отмена» на шаге выбора ФИО: заявки нет, состояние снято."""
    bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    answers = await press(vk, {keyboards.TEACHER_CB_FIELD:
                               keyboards.TEACHER_CB_CANCEL_VALUE})

    assert any(texts.TEACHER_APPLY_CANCELLED == a for a in answers), answers
    assert db.get_teacher(conn, TG_ID) is None
    assert await bot.state_dispenser.get(VK_ID) is None


async def test_cancel_word_discards_step(vk) -> None:
    """Текст «Отмена» на шаге ввода фамилии тоже прерывает заявку."""
    bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, "Отмена")

    assert any(texts.TEACHER_APPLY_CANCELLED == a for a in answers), answers
    assert db.get_teacher(conn, TG_ID) is None
    assert await bot.state_dispenser.get(VK_ID) is None


async def test_not_found_surname(vk) -> None:
    """Фамилия вне справочника: подсказываем и не создаём заявку."""
    _bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, "Пупкинехко")

    assert any(texts.TEACHER_NOT_FOUND.format(query="Пупкинехко") == a
               for a in answers), answers
    assert db.get_teacher(conn, TG_ID) is None


async def test_command_in_step_not_swallowed(vk) -> None:
    """Команда внутри шага ввода фамилии уходит своему хендлеру.

    Шаг перехватывает любой текст, но не команды (``func=is_group_input``):
    «/teacher_status» должен вернуть статус заявки, а не попасть в поиск по
    справочнику — иначе пользователь получал бы «не нашёл ФИО» на команду.
    """
    _bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, "/teacher_status")

    assert any(texts.TEACHER_STATUS_NONE == a for a in answers), answers
    assert not any("Не нашёл ФИО" in a for a in answers), answers


async def test_cancel_command_in_step_clears_state(vk) -> None:
    """«/teacher_cancel» в шаге ввода фамилии снимает состояние.

    Это важный случай: заявка ещё не создана, но шаг нужно прервать, и команда
    должна обрабатываться своим хендлером, а не поиском по справочнику.
    """
    bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, "/teacher_cancel")

    assert any(texts.TEACHER_CANCEL_NONE == a for a in answers), answers
    assert await bot.state_dispenser.get(VK_ID) is None


async def test_empty_text_asks_surname_again(vk) -> None:
    """Стикер или фото без подписи: просим фамилию текстом.

    Такое сообщение приходит с пустым ``text``, но состояние ввода фамилии
    активно — без этой ветки пользователь остался бы вообще без ответа.
    """
    bot, _api, conn = vk
    link(conn)

    await send(vk, "/teacher_apply")
    answers = await send(vk, "")

    assert any(texts.TEACHER_ASK_SURNAME_AGAIN == a for a in answers), answers
    peer_state = await bot.state_dispenser.get(VK_ID)
    assert peer_state is not None
    assert peer_state.state == TeacherApplyState.waiting_name


async def test_broad_query_shows_capped_choices(vk) -> None:
    """Широкий запрос («ов»): список обрезается до MAX_CHOICES, шаг продолжается.

    ``match_names`` ограничивает подборку, поэтому «слишком много совпадений»
    показывать не нужно — пользователь просто выбирает из показанных кнопок.
    Клавиатура обязана уложиться в лимиты VK (ряды ≤ 10).
    """
    import json

    bot, api, conn = vk
    link(conn)

    from bot.services.teacher_names import MAX_CHOICES, match_names

    # Запрос, дающий больше MAX_CHOICES совпадений, иначе тест беспредметен.
    assert len(match_names("ов", limit=MAX_CHOICES + 100)) > MAX_CHOICES

    await send(vk, "/teacher_apply")
    await send(vk, "ов")

    keyboard = next(
        (item["keyboard"] for item in api.messages.sent if item.get("keyboard")),
        None,
    )
    assert keyboard is not None, "должна уйти клавиатура с ФИО"
    buttons = sum(len(row) for row in json.loads(keyboard)["buttons"])
    assert buttons == MAX_CHOICES + 1, "ФИО обрезаны до MAX_CHOICES + отмена"

    peer_state = await bot.state_dispenser.get(VK_ID)
    assert len(peer_state.payload.get("teacher_matches") or []) == MAX_CHOICES


# --- повторная заявка ---

async def test_apply_when_pending(vk) -> None:
    """Повторный /teacher_apply при pending: «заявка уже отправлена»."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)

    answers = await send(vk, "/teacher_apply")

    assert any(texts.TEACHER_ALREADY_PENDING.format(fio=FIO) == a
               for a in answers), answers


async def test_apply_when_approved(vk) -> None:
    """Повторный /teacher_apply при approved: показываем команды преподавателя."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, ADMIN_ID)

    answers = await send(vk, "/teacher_apply")

    assert any(texts.TEACHER_ALREADY_APPROVED.format(fio=FIO) == a
               for a in answers), answers
    joined = "\n".join(answers)
    assert "/my_lessons" in joined and "/my_groups" in joined


async def test_apply_when_rejected(vk) -> None:
    """Повторный /teacher_apply при rejected: отсылаем к админу."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)
    db.reject_teacher(conn, TG_ID, ADMIN_ID)

    answers = await send(vk, "/teacher_apply")

    assert any(texts.TEACHER_REJECTED == a for a in answers), answers
# --- /teacher_status ---

async def test_status_without_application(vk) -> None:
    """Без заявки: подсказываем, как подать."""
    _bot, _api, _conn = vk

    answers = await send(vk, "/teacher_status")

    assert any(texts.TEACHER_STATUS_NONE == a for a in answers), answers


async def test_status_pending(vk) -> None:
    """Статус pending виден вместе с ФИО и статусом по-русски."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)

    answers = await send(vk, "/teacher_status")

    joined = "\n".join(answers)
    assert FIO in joined, answers
    assert "ожидает проверки" in joined, answers


async def test_status_without_link(vk) -> None:
    """Без связки статус недоступен: заявки у этого VK попросту нет."""
    _bot, _api, _conn = vk

    answers = await send(vk, "/teacher_status")

    assert any(texts.TEACHER_STATUS_NONE == a for a in answers), answers


async def test_status_approved(vk) -> None:
    """Одобренная заявка показывается как «одобрена»."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, ADMIN_ID)

    answers = await send(vk, "/teacher_status")

    assert any("одобрена" in a for a in answers), answers


# --- /teacher_cancel ---

async def test_cancel_pending_application(vk) -> None:
    """Отмена pending-заявки: запись удаляется."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)

    answers = await send(vk, "/teacher_cancel")

    assert any(texts.TEACHER_CANCELLED == a for a in answers), answers
    assert db.get_teacher(conn, TG_ID) is None


async def test_cancel_without_application(vk) -> None:
    """Отменять нечего: отвечаем, что активной заявки нет."""
    _bot, _api, conn = vk
    link(conn)

    answers = await send(vk, "/teacher_cancel")

    assert any(texts.TEACHER_CANCEL_NONE == a for a in answers), answers


async def test_cancel_approved_is_forbidden(vk) -> None:
    """Одобренную заявку отменить нельзя — доступ уже выдан."""
    _bot, _api, conn = vk
    link(conn)
    db.apply_teacher(conn, TG_ID, FIO)
    db.approve_teacher(conn, TG_ID, ADMIN_ID)

    answers = await send(vk, "/teacher_cancel")

    assert any("нельзя отменить" in a for a in answers), answers
    teacher = db.get_teacher(conn, TG_ID)
    assert teacher is not None
    assert teacher["status"] == db.TEACHER_APPROVED


async def test_cancel_without_link(vk) -> None:
    """Без связки отменять нечего: отвечаем нейтрально."""
    _bot, _api, _conn = vk

    answers = await send(vk, "/teacher_cancel")

    assert any(texts.TEACHER_CANCEL_NONE == a for a in answers), answers
# --- уведомление админа ---

async def test_admin_notified_with_vk_source(vk, monkeypatch) -> None:
    """После подачи заявки из VK админ получает уведомление с «Источник: VK»."""
    _bot, _api, conn = vk
    link(conn)

    sent: list[dict] = []

    class FakeTgBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
            return True

    monkeypatch.setattr(tg_bridge, "get_tg_bot", lambda: FakeTgBot())
    # Настройки читались бы из env — подменяем, чтобы тест не зависел от .env.
    monkeypatch.setattr(teacher_notify, "_resolve_admin_ids",
                        lambda settings: (ADMIN_ID,))

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    await press(vk, {keyboards.TEACHER_CB_FIELD: keyboards.TEACHER_CB_VALUE,
                     keyboards.TEACHER_CB_INDEX: 0})

    assert sent, "уведомление должно уйти админу в Telegram"
    assert sent[0]["chat_id"] == ADMIN_ID
    assert FIO in sent[0]["text"]
    assert "Источник: VK" in sent[0]["text"]


async def test_application_survives_without_tg_bot(vk, monkeypatch) -> None:
    """Без ссылки на TG-бота заявка всё равно создаётся — падения нет.

    Так выглядит запуск VK-бота отдельным процессом: уведомить некого, но
    заявка обязана лечь в БД, иначе она потеряется молча.
    """
    _bot, _api, conn = vk
    link(conn)

    monkeypatch.setattr(tg_bridge, "get_tg_bot", lambda: None)

    await send(vk, "/teacher_apply")
    await send(vk, SURNAME)
    await press(vk, {keyboards.TEACHER_CB_FIELD: keyboards.TEACHER_CB_VALUE,
                     keyboards.TEACHER_CB_INDEX: 0})

    assert db.get_teacher(conn, TG_ID) is not None


async def test_admin_notification_source_tg(vk, monkeypatch) -> None:
    """Заявка из TG уведомляет админа с источником «Telegram».

    Проверяем общую функцию напрямую: её переиспользуют оба бота, и источник
    должен различаться — иначе админ не поймёт, куда отвечать человеку.
    """
    _bot, _api, conn = vk

    sent: list[dict] = []

    class FakeTgBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append({"chat_id": chat_id, "text": text})
            return True

    monkeypatch.setattr(teacher_notify, "_resolve_admin_ids",
                        lambda settings: (ADMIN_ID,))

    delivered = await teacher_notify.notify_admin_about_teacher_application(
        FakeTgBot(), conn, TG_ID, FIO, "tg",
    )

    assert delivered is True
    assert sent and "Источник: Telegram" in sent[0]["text"]
    assert sent[0]["chat_id"] == ADMIN_ID


async def test_notification_without_admins_returns_false(vk, monkeypatch) -> None:
    """Без ADMIN_IDS уведомление не уходит и не падает."""
    _bot, _api, conn = vk
    monkeypatch.setattr(teacher_notify, "_resolve_admin_ids",
                        lambda settings: ())

    class FakeTgBot:
        async def send_message(self, chat_id, text, **kwargs):
            raise AssertionError("отправки быть не должно")

    delivered = await teacher_notify.notify_admin_about_teacher_application(
        FakeTgBot(), conn, TG_ID, FIO, "vk",
    )

    assert delivered is False
# --- клавиатура выбора ФИО ---

def test_names_kb_layout_and_payload() -> None:
    """Клавиатура ФИО: inline, по 2 в ряд, payload содержит индекс и «Отмену»."""
    import json

    kb = keyboards.names_kb([FIO, AMBIGUOUS_FIO, "Абрамов Виктор Николаевич"])
    data = json.loads(kb)

    assert data["inline"] is True
    rows = data["buttons"]
    assert [len(row) for row in rows] == [2, 1, 1], rows

    payloads = button_payloads(kb)
    assert payloads[0] == {keyboards.TEACHER_CB_FIELD:
                           keyboards.TEACHER_CB_VALUE, "i": 0}
    assert payloads[1]["i"] == 1
    assert payloads[-1] == {keyboards.TEACHER_CB_FIELD:
                            keyboards.TEACHER_CB_CANCEL_VALUE}


def test_names_kb_fio_labels_intact() -> None:
    """Подписи кнопок — полные ФИО (терять их нельзя: по ним выбирают себя)."""
    labels = keyboard_labels(keyboards.names_kb([FIO, AMBIGUOUS_FIO]))

    assert labels[0] == FIO
    assert labels[1] == AMBIGUOUS_FIO


def test_names_kb_respects_vk_row_limit() -> None:
    """Рядов не больше 10 — предел inline-клавиатуры VK.

    MAX_CHOICES ФИО при раскладке по 2 в ряд дают 10 рядов, плюс ряд с
    «Отменой»; для 20 это ровно на границе, поэтому проверяем фактическую
    раскладку, а не «на глаз».
    """
    import json

    from bot.services.teacher_names import MAX_CHOICES

    names = [f"Тестовый Преподаватель Номер {i}" for i in range(MAX_CHOICES)]
    data = json.loads(keyboards.names_kb(names))

    buttons = sum(len(row) for row in data["buttons"])
    assert buttons == MAX_CHOICES + 1, "все ФИО плюс кнопка отмены"
    assert len(data["buttons"]) == MAX_CHOICES // 2 + 1


# --- изоляция от aiogram ---

def test_teacher_services_import_without_aiogram() -> None:
    """Общие сервисы заявки не тянут aiogram.

    Иначе ``bot_vk`` поднял бы весь Telegram-стек. Проверяем в отдельном
    процессе: в текущем aiogram уже мог быть загружен другими тестами сессии.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys;"
        "import bot.services.teacher_names, bot.services.teacher_notify;"
        "print('aiogram' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


def test_vk_handlers_do_not_import_aiogram() -> None:
    """Хендлеры VK-бота не тянут aiogram — правило архитектуры.

    ``bot_vk.handlers`` импортирует общий сервис уведомлений, и важно, чтобы
    даже через него aiogram не оказался в ``sys.modules``.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys;"
        "import bot_vk.handlers, bot_vk.keyboards, bot_vk.tg_bridge;"
        "print('aiogram' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


# --- доступ к меню преподавателя ---

def test_teacher_full_name_requires_approved(vk) -> None:
    """teacher_full_name отдаёт ФИО только одобренному преподавателю.

    По этому признаку подшаг 4.3 решает, показывать ли меню преподавателя:
    pending не должен давать доступ.
    """
    _bot, _api, conn = vk
    link(conn)

    assert storage.teacher_full_name(conn, VK_ID) is None

    db.apply_teacher(conn, TG_ID, FIO)
    assert storage.teacher_full_name(conn, VK_ID) is None, "pending — не доступ"

    db.approve_teacher(conn, TG_ID, ADMIN_ID)
    assert storage.teacher_full_name(conn, VK_ID) == FIO


def test_teacher_full_name_without_link(vk) -> None:
    """Без связки преподавателем быть нельзя: ФИО не определяется."""
    _bot, _api, conn = vk

    assert storage.teacher_full_name(conn, VK_ID) is None