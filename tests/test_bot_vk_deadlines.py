"""Тесты раздела «📝 Дедлайны» в VK-боте (подшаг 2 зеркала TG).

E2E через настоящий диспетчер: апдейты ``message_new`` и ``message_event``
прогоняются через ``bot.process_event``, подменяется только транспорт.

Главное, что проверяется: дедлайны ОБЩИЕ с Telegram. Строки таблицы
``deadlines`` привязаны к ``tg_id``, поэтому VK-бот работает через связку
vk_id → tg_id. Без связки раздел честно просит её оформить.
"""

from datetime import date, timedelta

import pytest

from bot.db import create_link_code, get_connection, use_link_code
from bot.migrations import apply_migrations
from bot.services import deadline_service as dl
from bot_vk import keyboards, storage, texts
from bot_vk.handlers import DeadlineState, register_handlers
from bot_vk.view import render_deadline_line, render_deadlines

GROUP_ID = 987654321
VK_ID = 777
TG_ID = 908084777

# База отсчёта для срочности: фиксируем «сегодня», чтобы тест не зависел
# от дня прогона (иначе «завтра» станет «просрочено» после полуночи).
TODAY = date(2026, 10, 6)


class FakeMessagesAPI:
    """Мини-``api.messages``: собирает отправленные сообщения и правки."""

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

    async def edit(self, **kwargs) -> int:
        """Правка сообщения из callback (edit_message)."""
        self.sent.append({**kwargs, "edited": True})
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
    """Сырой апдейт ``message_event`` — нажатие inline-кнопки."""
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
    """БД с миграциями и зарегистрированным Telegram-пользователем.

    Запись в ``users`` обязательна: ``deadlines.tg_id`` ссылается на неё
    внешним ключом, без пользователя вставка падает с IntegrityError.
    """
    connection = get_connection(tmp_path / "vk_deadlines.db")
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO users (tg_id, group_name, created_at)"
        " VALUES (?, '25КАД', '2026-10-01T00:00:00+07:00')",
        (TG_ID,),
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
    return _answers(api)


async def press(vk, payload: dict) -> list[str]:
    """Нажать inline-кнопку и вернуть ответы (включая тексты snackbar)."""
    bot, api, _conn = vk
    api.messages.sent.clear()
    await bot.process_event(make_callback(payload))
    return _answers(api)


def _answers(api) -> list[str]:
    """Тексты из отправленных сообщений, правок и snackbar."""
    result: list[str] = []
    for item in api.messages.sent:
        if item.get("event_answer"):
            result.append(str(item["event_answer"]))
        elif item.get("message"):
            result.append(str(item["message"]))
    return result


def link(conn, vk_id: int = VK_ID, tg_id: int = TG_ID) -> None:
    """Создать связку VK ↔ TG прямо в БД (без кодов и сети)."""
    code = create_link_code(conn, tg_id)
    result = use_link_code(conn, code, vk_id)
    assert result["ok"] is True, result


def click(action: str, deadline_id: int | None = None) -> dict:
    """Payload кнопки раздела дедлайнов."""
    payload = {keyboards.DEADLINE_CB_FIELD: keyboards.DEADLINE_CB_VALUE,
               keyboards.DEADLINE_CB_ACTION: action}
    if deadline_id is not None:
        payload[keyboards.DEADLINE_CB_ID] = deadline_id
    return payload


def keyboard_labels(keyboard: str) -> list[str]:
    """Подписи кнопок из JSON-строки клавиатуры VK."""
    import json

    data = json.loads(keyboard)
    return [
        button["action"]["label"]
        for row in data.get("buttons", [])
        for button in row
    ]
# --- доступ: только через связку с TG ---

async def test_deadlines_without_link_asks_to_link(vk) -> None:
    """Без связки раздел просит привязать TG — как рекомендовано в задании."""
    _bot, _api, _conn = vk

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert any(texts.DEADLINES_NO_LINK == a for a in answers), answers
    assert "/link" in joined, answers


async def test_deadlines_without_link_creates_nothing(vk) -> None:
    """Без связки дедлайнов не появляется — владельца нет."""
    _bot, _api, conn = vk

    await send(vk, keyboards.BTN_DEADLINES)

    assert dl.list_active(conn, TG_ID) == []


async def test_deadlines_button_in_menu(vk) -> None:
    """Кнопка «📝 Дедлайны» есть в главном меню — иначе раздел не открыть.

    Проверяем саму клавиатуру, а не ответ /start: у зарегистрированного
    пользователя приветствие уходит без клавиатуры (она уже стоит на экране).
    """
    _bot, _api, _conn = vk

    labels = keyboard_labels(keyboards.main_kb())

    assert keyboards.BTN_DEADLINES in labels, labels


# --- список ---

async def test_empty_list_shows_placeholder(vk) -> None:
    """Пустой список: «Пока пусто» и кнопки действий."""
    _bot, api, conn = vk
    link(conn)

    answers = await send(vk, keyboards.BTN_DEADLINES)

    assert any(texts.DEADLINES_EMPTY == a for a in answers), answers
    keyboard = next(m["keyboard"] for m in api.messages.sent if m.get("keyboard"))
    labels = keyboard_labels(keyboard)
    assert texts.BTN_DL_ADD in labels, labels
    assert texts.BTN_DL_DELETE in labels, labels


async def test_list_shows_added_deadline(vk) -> None:
    """Список показывает созданный дедлайн с задачей и датой."""
    _bot, _api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "История", "", "Сдать реферат",
           (TODAY + timedelta(days=5)).isoformat())

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert "Сдать реферат" in joined, answers
    assert "История" in joined, answers


async def test_list_groups_by_urgency(vk) -> None:
    """Дедлайны разложены по срочности, как в TG."""
    _bot, _api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "Физика", "", "Просроченная",
           (date.today() - timedelta(days=3)).isoformat())
    dl.add(conn, TG_ID, "Химия", "", "На сегодня", date.today().isoformat())
    dl.add(conn, TG_ID, "МДК", "", "На завтра",
           (date.today() + timedelta(days=1)).isoformat())

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert "🔴 Просрочено" in joined, answers
    assert "🟠 Сегодня" in joined, answers
    assert "🟡 Завтра" in joined, answers


async def test_list_has_no_html_tags(vk) -> None:
    """VK-формат — plain text: HTML-тегов в списке быть не должно."""
    _bot, _api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "История", "", "Реферат", TODAY.isoformat())

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert "<b>" not in joined and "</b>" not in joined, joined
    assert "<i>" not in joined and "</i>" not in joined, joined
# --- добавление: три шага FSM ---

async def test_add_starts_fsm(vk) -> None:
    """«➕ Добавить» переводит в шаг ввода названия."""
    bot, api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))

    peer = await bot.state_dispenser.get(VK_ID)
    assert peer is not None
    assert peer.state == DeadlineState.waiting_subject


async def test_add_full_flow_creates_deadline(vk) -> None:
    """Три шага FSM создают дедлайн в БД (общий с TG)."""
    bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "Математика")          # шаг 1: предмет
    await send(vk, "Сдать реферат")       # шаг 2: задача
    answers = await send(vk, "15.10.2026")  # шаг 3: дата

    items = dl.list_active(conn, TG_ID)
    assert len(items) == 1, items
    assert items[0]["subject"] == "Математика"
    assert items[0]["task"] == "Сдать реферат"
    assert items[0]["deadline_date"] == "2026-10-15"

    assert any("Дедлайн добавлен" in a for a in answers), answers
    assert await bot.state_dispenser.get(VK_ID) is None, "состояние снято"


async def test_add_without_date(vk) -> None:
    """На шаге даты «нет» создаёт дедлайн без даты."""
    _bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "Физика")
    await send(vk, "Лабораторная")
    answers = await send(vk, "нет")

    items = dl.list_active(conn, TG_ID)
    assert len(items) == 1, items
    assert items[0]["deadline_date"] is None, items[0]
    assert any("Дедлайн добавлен" in a for a in answers), answers


async def test_add_bad_date_asks_again(vk) -> None:
    """Непонятная дата: просим повторить, дедлайн не создаётся."""
    _bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "Химия")
    await send(vk, "Курсовая")
    answers = await send(vk, "не дата")

    assert any(texts.DEADLINE_BAD_DATE == a for a in answers), answers
    assert dl.list_active(conn, TG_ID) == []


async def test_add_cancel_at_first_step(vk) -> None:
    """«Отмена» на первом шаге прерывает добавление."""
    bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    answers = await send(vk, "Отмена")

    assert any(texts.DEADLINE_CANCELLED == a for a in answers), answers
    assert dl.list_active(conn, TG_ID) == []
    assert await bot.state_dispenser.get(VK_ID) is None


async def test_add_cancel_at_third_step(vk) -> None:
    """«Отмена» на шаге даты тоже прерывает добавление."""
    bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "История")
    await send(vk, "Доклад")
    answers = await send(vk, "Отмена")

    assert any(texts.DEADLINE_CANCELLED == a for a in answers), answers
    assert dl.list_active(conn, TG_ID) == []
    assert await bot.state_dispenser.get(VK_ID) is None


async def test_add_keeps_subject_from_first_step(vk) -> None:
    """Предмет из шага 1 не теряется к шагу 3 (хранится в payload состояния)."""
    bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "Информатика")
    peer = await bot.state_dispenser.get(VK_ID)
    assert peer.payload.get("dl_subject") == "Информатика"

    await send(vk, "Проект")
    peer = await bot.state_dispenser.get(VK_ID)
    assert peer.payload.get("dl_subject") == "Информатика"
    assert peer.payload.get("dl_task") == "Проект"


async def test_add_short_date_format(vk) -> None:
    """Короткий формат «ДД.ММ» тоже принимается (как в TG)."""
    _bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "МДК")
    await send(vk, "Проект")
    await send(vk, "15.10")

    items = dl.list_active(conn, TG_ID)
    assert len(items) == 1, items
    assert items[0]["deadline_date"] is not None
    assert items[0]["deadline_date"].endswith("-10-15")
# --- удаление ---

async def test_delete_button_lists_deadlines(vk) -> None:
    """«❌ Удалить» показывает выбор дедлайна кнопками."""
    _bot, api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "История", "", "Реферат", TODAY.isoformat())

    await press(vk, click(keyboards.DEADLINE_ACTION_DELETE))

    edited = [m for m in api.messages.sent if m.get("edited")]
    assert edited, "должна быть правка сообщения со списком"
    keyboard = edited[0].get("keyboard")
    assert keyboard, "кнопки выбора дедлайна обязательны"
    labels = keyboard_labels(keyboard)
    assert any("Реферат" in label for label in labels), labels
    assert texts.BTN_DL_LIST in labels, labels


async def test_delete_empty_shows_snackbar(vk) -> None:
    """Удалять нечего: показываем подсказку, а не пустой список."""
    _bot, _api, conn = vk
    link(conn)

    answers = await press(vk, click(keyboards.DEADLINE_ACTION_DELETE))

    assert any(texts.DEADLINE_DELETE_EMPTY in a for a in answers), answers


async def test_delete_soft_deletes(vk) -> None:
    """Кнопка дедлайна мягко удаляет запись (она остаётся в БД)."""
    _bot, _api, conn = vk
    link(conn)
    deadline_id = dl.add(conn, TG_ID, "История", "", "Реферат", None)

    await press(vk, click(keyboards.DEADLINE_ACTION_DELETE, deadline_id))

    assert dl.list_active(conn, TG_ID) == [], "из активных пропал"
    row = conn.execute(
        "SELECT deleted_at FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    assert row["deleted_at"] is not None, "мягкое удаление: deleted_at выставлен"


async def test_delete_other_users_deadline_refused(vk) -> None:
    """Чужой дедлайн удалить нельзя: id из payload проверяется по владельцу."""
    _bot, _api, conn = vk
    link(conn)
    # Дедлайн другого пользователя (FK требует его в users).
    conn.execute(
        "INSERT INTO users (tg_id, group_name, created_at)"
        " VALUES (555000111, '25КАД', 'x')"
    )
    conn.commit()
    foreign_id = dl.add(conn, 555000111, "Физика", "", "Чужое", None)

    answers = await press(vk, click(keyboards.DEADLINE_ACTION_DELETE,
                                    foreign_id))

    assert any(texts.DEADLINE_DELETE_NOT_FOUND in a for a in answers), answers
    assert dl.list_active(conn, 555000111), "чужой дедлайн не тронут"


async def test_delete_malformed_id(vk) -> None:
    """Мусор в id не роняет бота: отвечаем подсказкой."""
    _bot, _api, conn = vk
    link(conn)
    payload = click(keyboards.DEADLINE_ACTION_DELETE)
    payload[keyboards.DEADLINE_CB_ID] = "abc"

    answers = await press(vk, payload)

    assert any(texts.DEADLINE_STALE in a for a in answers), answers


async def test_list_button_returns_to_list(vk) -> None:
    """«🔙 К списку» возвращает список дедлайнов."""
    _bot, _api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "Химия", "", "Курсовая", TODAY.isoformat())

    answers = await press(vk, click(keyboards.DEADLINE_ACTION_LIST))

    assert any("Курсовая" in a for a in answers), answers
# --- общность с Telegram ---

async def test_deadline_from_telegram_visible_in_vk(vk) -> None:
    """Дедлайн, созданный «со стороны Telegram», виден в VK.

    Это и есть смысл связки: список один на обе платформы.
    """
    _bot, _api, conn = vk
    link(conn)
    dl.add(conn, TG_ID, "История", "Петров П.П.", "Сдать доклад",
           (TODAY + timedelta(days=2)).isoformat())

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert "Сдать доклад" in joined, answers
    assert "История" in joined, answers


async def test_deadline_created_in_vk_visible_in_telegram(vk) -> None:
    """Дедлайн из VK появляется в общем списке по tg_id (виден в TG).

    Проверяем через сервис: именно его читает Telegram-хендлер, поэтому
    запись в БД по нужному tg_id и есть доказательство общности.
    """
    _bot, _api, conn = vk
    link(conn)

    await press(vk, click(keyboards.DEADLINE_ACTION_ADD))
    await send(vk, "Физика")
    await send(vk, "Лабораторная")
    await send(vk, "20.10.2026")

    items = dl.list_active(conn, TG_ID)
    assert any(i["task"] == "Лабораторная" for i in items), items


async def test_deadlines_use_linked_tg_id(vk) -> None:
    """Дедлайны читаются по tg_id из связки, а не по vk_id."""
    _bot, _api, conn = vk
    other_tg = 111222333
    # FK требует запись в users для владельца дедлайна.
    conn.execute(
        "INSERT INTO users (tg_id, group_name, created_at)"
        " VALUES (?, '25КАД', 'x')",
        (other_tg,),
    )
    conn.commit()
    link(conn, vk_id=VK_ID, tg_id=other_tg)
    dl.add(conn, other_tg, "МДК", "", "Моё дело", TODAY.isoformat())
    dl.add(conn, TG_ID, "История", "", "Чужое дело", TODAY.isoformat())

    answers = await send(vk, keyboards.BTN_DEADLINES)
    joined = "\n".join(answers)

    assert "Моё дело" in joined, answers
    assert "Чужое дело" not in joined, answers


# --- рендер (чистые функции) ---

def test_render_empty_list() -> None:
    """Пустой список — дружелюбная строка, а не пустота."""
    assert render_deadlines([]) == texts.DEADLINES_EMPTY


def test_render_line_without_date() -> None:
    """Дедлайн без даты помечается «без даты»."""
    line = render_deadline_line(
        {"task": "Реферат", "subject": "История", "deadline_date": None}
    )

    assert "Реферат" in line, line
    assert "без даты" in line, line


def test_render_line_overdue() -> None:
    """Просроченный дедлайн показывает, сколько дней назад истёк."""
    line = render_deadline_line(
        {"task": "Долг", "subject": "", "deadline_date": TODAY.isoformat(),
         "days_left": -3}
    )

    assert "3 дн. назад" in line, line


def test_render_line_today_and_tomorrow() -> None:
    """Пометки «сегодня» и «завтра» ставятся по days_left."""
    today_line = render_deadline_line(
        {"task": "A", "deadline_date": TODAY.isoformat(), "days_left": 0}
    )
    tomorrow_line = render_deadline_line(
        {"task": "B", "deadline_date": TODAY.isoformat(), "days_left": 1}
    )

    assert "сегодня" in today_line, today_line
    assert "завтра" in tomorrow_line, tomorrow_line


def test_render_line_far_future_has_no_marker() -> None:
    """Далёкий срок печатается без пометки срочности."""
    line = render_deadline_line(
        {"task": "Проект", "subject": "Информатика",
         "deadline_date": TODAY.isoformat(), "days_left": 30}
    )

    assert "Проект" in line, line
    assert "сегодня" not in line and "завтра" not in line, line


def test_render_groups_only_non_empty_sections() -> None:
    """Печатаются только непустые группы срочности (как в TG).

    Даты берём относительно ``today``, а не готовый ``days_left``: сервис
    пересчитывает срочность по дате (:func:`deadline_service.group_by_urgency`),
    потому что одна и та же запись должна попадать в правильную группу и после
    смены суток.
    """
    items = [
        {"task": "Просрочено", "deadline_date": (TODAY - timedelta(days=1)).isoformat(),
         "subject": "", "teacher": ""},
        {"task": "Сегодня", "deadline_date": TODAY.isoformat(),
         "subject": "", "teacher": ""},
    ]

    text = render_deadlines(items, today=TODAY)

    assert "🔴 Просрочено" in text, text
    assert "🟠 Сегодня" in text, text
    assert "🟡 Завтра" not in text, text
    assert "⚪ Позже" not in text, text
# --- клавиатуры ---

def test_deadlines_kb_has_add_and_delete() -> None:
    """Клавиатура списка: «Добавить» и «Удалить», обе callback-типа."""
    import json

    data = json.loads(keyboards.deadlines_kb())
    actions = [b["action"] for row in data["buttons"] for b in row]

    assert [a["label"] for a in actions] == [texts.BTN_DL_ADD,
                                             texts.BTN_DL_DELETE]
    assert all(a["type"] == "callback" for a in actions), actions


def test_delete_kb_payload_carries_id() -> None:
    """В кнопке удаления id дедлайна уходит в payload, а не в подпись."""
    import json

    items = [{"id": 42, "task": "Реферат"}, {"id": 43, "task": "Курсовая"}]
    data = json.loads(keyboards.deadline_delete_kb(items))
    payloads = [b["action"]["payload"] for row in data["buttons"] for b in row]

    assert payloads[0][keyboards.DEADLINE_CB_ID] == 42, payloads
    assert payloads[1][keyboards.DEADLINE_CB_ID] == 43, payloads


def test_delete_kb_truncates_long_task_label() -> None:
    """Длинное название задачи обрезается в подписи, но id сохраняется."""
    import json

    long_task = "Очень длинное название задачи, которое точно не влезет в кнопку"
    data = json.loads(keyboards.deadline_delete_kb([{"id": 7,
                                                     "task": long_task}]))
    action = data["buttons"][0][0]["action"]

    assert len(action["label"]) <= 40, action["label"]
    assert action["payload"][keyboards.DEADLINE_CB_ID] == 7


def test_delete_kb_has_back_to_list() -> None:
    """В клавиатуре удаления есть «К списку» — вернуться без тупика."""
    labels = keyboard_labels(keyboards.deadline_delete_kb([{"id": 1,
                                                            "task": "A"}]))

    assert texts.BTN_DL_LIST in labels, labels


def test_delete_kb_rows_within_vk_limit() -> None:
    """Рядов не больше 10 — предел inline-клавиатуры VK.

    Каждый дедлайн занимает свой ряд, поэтому длинный список обязан урезаться:
    иначе VK отвергнет сообщение целиком, и пользователь не увидит ничего.
    Проверяем заведомо больший список (15 задач), а не «ровно по пределу».
    """
    import json

    items = [{"id": index, "task": f"Задача {index}"} for index in range(15)]
    data = json.loads(keyboards.deadline_delete_kb(items))

    assert len(data["buttons"]) <= 10, len(data["buttons"])
    # Урезали до DEADLINE_DELETE_LIMIT задач + ряд «К списку».
    buttons = sum(len(row) for row in data["buttons"])
    assert buttons == keyboards.DEADLINE_DELETE_LIMIT + 1, buttons


def test_delete_kb_truncates_extra_deadlines() -> None:
    """Лишние дедлайны не попадают в клавиатуру, но остаются в БД.

    Урезается только клавиатура: сами записи не трогаем, иначе «удаление»
    молча стирало бы данные.
    """
    import json

    items = [{"id": index, "task": f"Задача {index}"} for index in range(15)]
    data = json.loads(keyboards.deadline_delete_kb(items))
    payloads = [b["action"]["payload"] for row in data["buttons"] for b in row]

    ids = [p.get(keyboards.DEADLINE_CB_ID) for p in payloads
           if keyboards.DEADLINE_CB_ID in p]
    assert len(ids) == keyboards.DEADLINE_DELETE_LIMIT, ids
    assert ids == list(range(keyboards.DEADLINE_DELETE_LIMIT)), ids


def test_delete_kb_handles_missing_task() -> None:
    """Пустое название задачи не ломает подпись кнопки."""
    labels = keyboard_labels(keyboards.deadline_delete_kb([{"id": 1}]))

    assert any(texts.DEADLINE_NO_TASK in label for label in labels), labels


# --- изоляция от aiogram ---

def test_deadlines_modules_do_not_import_aiogram() -> None:
    """Раздел дедлайнов не тянет aiogram.

    Сервис дедлайнов общий с TG, но он aiogram-free — именно это позволяет
    переиспользовать его в VK без копии правил срочности и разбора дат.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys;"
        "import bot_vk.handlers, bot_vk.keyboards, bot_vk.view,"
        " bot_vk.storage;"
        "from bot.services import deadline_service;"
        "print('aiogram' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


def test_storage_deadlines_use_service(vk) -> None:
    """storage — тонкая обёртка над сервисом, без копии SQL.

    Проверяем, что данные, записанные через storage, видны сервису и наоборот:
    это гарантия, что правила (срочность, soft delete) не разъехались.
    """
    _bot, _api, conn = vk

    deadline_id = storage.add_deadline(conn, TG_ID, "История", "", "Реферат",
                                       TODAY.isoformat())

    assert dl.get(conn, deadline_id, TG_ID)["task"] == "Реферат"
    assert storage.list_deadlines(conn, TG_ID) == dl.list_active(conn, TG_ID)

    assert storage.delete_deadline(conn, deadline_id, TG_ID) is True
    assert storage.list_deadlines(conn, TG_ID) == []