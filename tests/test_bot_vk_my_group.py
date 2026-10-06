"""Тесты раздела «📊 Моя группа» в VK-боте (подшаг 1 зеркала TG).

E2E через настоящий диспетчер, как в ``tests/test_bot_vk_handlers.py``:
подменяется только транспорт, апдейты прогоняются через ``bot.process_event``.

Ключевая особенность VK: учебная группа живёт в Telegram (``students.tg_id``),
поэтому «свой» студент определяется через связку vk_id → tg_id. Без связки
старостой быть нельзя — это и проверяется в первую очередь.

Данные группы создаются тем же ``bot.attendance.db``, что и в TG: общие
таблицы, значит раздел действительно зеркалит Telegram, а не живёт отдельно.
"""

import pytest

from bot.attendance import db as att_db
from bot.db import create_link_code, get_connection, use_link_code
from bot.migrations import apply_migrations
from bot_vk import keyboards, storage, texts
from bot_vk.handlers import register_handlers
from bot_vk.view import render_my_group

GROUP_ID = 987654321
VK_ID = 777
TG_ID = 908084777
STAROSTA_TG = 111222333
DEPUTY_TG = 222333444

GROUP = "25КАД"
INVITE_CODE = "482931"

# День недели, на который кладём пары в кэш (понедельник есть в любой неделе).
CACHED_WEEKDAY = 1


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


@pytest.fixture()
def conn(tmp_path):
    """БД с миграциями, кэшем расписания и учебной группой."""
    connection = get_connection(tmp_path / "vk_my_group.db")
    apply_migrations(connection)

    connection.execute(
        "INSERT INTO schedule_cache"
        " (group_name, day_of_week, para_number, subject, teacher, room,"
        "  week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00')",
        (GROUP, CACHED_WEEKDAY, 1, "Математика", "Соколова Е.В.", "204", ""),
    )

    # Учебная группа: староста, зам и обычный студент.
    att_db.insert_group(connection, GROUP, INVITE_CODE, created_by=STAROSTA_TG,
                        starosta_tg_id=STAROSTA_TG)
    att_db.insert_student(connection, STAROSTA_TG, GROUP, "Абрамчик С.Г.",
                          role="starosta")
    att_db.insert_student(connection, DEPUTY_TG, GROUP, "Богатырева И.П.",
                          role="deputy")
    att_db.insert_student(connection, TG_ID, GROUP, "Виссарионова А.С.")
    # Зама записываем в study_groups: insert_group принимает только старосту,
    # а именно оттуда карточка берёт «Зам: …».
    att_db.set_group_deputy(connection, GROUP, DEPUTY_TG)

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


def link(conn, vk_id: int = VK_ID, tg_id: int = TG_ID) -> None:
    """Создать связку VK ↔ TG прямо в БД (без кодов и сети)."""
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
# --- клавиатура ---

async def test_menu_has_my_group_button(vk) -> None:
    """После /start в reply-клавиатуре есть «📊 Моя группа» — как в TG."""
    _bot, api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    await send(vk, "/start")

    keyboard = next(m["keyboard"] for m in api.messages.sent if m.get("keyboard"))
    labels = keyboard_labels(keyboard)
    assert keyboards.BTN_MY_GROUP in labels, labels


async def test_menu_layout_matches_telegram(vk) -> None:
    """Раскладка меню 1-в-1 как в TG: 2×2 и тот же порядок подписей."""
    import json

    from bot.keyboards import reply as tg_reply

    _bot, _api, _conn = vk
    data = json.loads(keyboards.main_kb())
    labels = [b["action"]["label"] for row in data["buttons"] for b in row]

    assert labels == [
        tg_reply.BTN_SCHEDULE, tg_reply.BTN_DEADLINES,
        tg_reply.BTN_MY_GROUP, tg_reply.BTN_PROFILE,
    ]
    assert len(data["buttons"]) == 2, "ровно два ряда, как в TG"
    assert all(len(row) == 2 for row in data["buttons"])


# --- без группы ---

async def test_my_group_without_link_shows_hint(vk) -> None:
    """Без связки с TG: объясняем про /link и код приглашения."""
    _bot, _api, _conn = vk

    answers = await send(vk, keyboards.BTN_MY_GROUP)

    assert any(texts.MY_GROUP_NO_LINK == a for a in answers), answers
    assert any("/link" in a for a in answers), answers


async def test_my_group_with_link_but_no_group(vk) -> None:
    """Связка есть, но код приглашения не введён: зовём вступить в TG."""
    _bot, _api, conn = vk
    link(conn, vk_id=VK_ID, tg_id=TG_ID + 1)  # связанный TG не в группе

    answers = await send(vk, keyboards.BTN_MY_GROUP)

    assert any(texts.MY_GROUP_NOT_REGISTERED == a for a in answers), answers


# --- с группой ---

async def test_my_group_shows_group_card(vk) -> None:
    """Студент в группе: карточка с названием, счётчиком и ролями."""
    _bot, _api, conn = vk
    link(conn)

    answers = await send(vk, keyboards.BTN_MY_GROUP)
    joined = "\n".join(answers)

    assert GROUP in joined, answers
    assert "Студентов: 3" in joined, answers
    # Староста и зам попадают в карточку по tg_id из study_groups.
    assert "Абрамчик С.Г." in joined, answers
    assert "Богатырева И.П." in joined, answers


async def test_my_group_shows_student_list(vk) -> None:
    """В ответе есть список группы с ФИО студентов."""
    _bot, _api, conn = vk
    link(conn)

    answers = await send(vk, keyboards.BTN_MY_GROUP)
    joined = "\n".join(answers)

    assert "Список группы:" in joined, answers
    assert "• Виссарионова А.С." in joined, answers
    assert joined.count("• ") == 3, answers


async def test_my_group_comes_with_keyboard(vk) -> None:
    """Карточка группы приходит с главным меню — вернуться можно кнопкой."""
    _bot, api, conn = vk
    link(conn)

    await send(vk, keyboards.BTN_MY_GROUP)

    keyboard = next(m["keyboard"] for m in api.messages.sent if m.get("keyboard"))
    assert keyboards.BTN_MY_GROUP in keyboard_labels(keyboard)
# --- код приглашения: только староста и зам ---

async def test_student_does_not_see_invite_code(vk) -> None:
    """Обычный студент код приглашения не видит."""
    _bot, _api, conn = vk
    link(conn, vk_id=VK_ID, tg_id=TG_ID)  # TG_ID — обычный студент

    answers = await send(vk, keyboards.BTN_MY_GROUP)
    joined = "\n".join(answers)

    assert INVITE_CODE not in joined, answers
    assert "Код приглашения" not in joined, answers


async def test_starosta_sees_invite_code(vk) -> None:
    """Староста (связанный с TG) видит код приглашения."""
    _bot, _api, conn = vk
    link(conn, vk_id=VK_ID, tg_id=STAROSTA_TG)

    answers = await send(vk, keyboards.BTN_MY_GROUP)
    joined = "\n".join(answers)

    assert INVITE_CODE in joined, answers
    assert "Код приглашения" in joined, answers


async def test_deputy_sees_invite_code(vk) -> None:
    """Зам тоже видит код — как в TG (MANAGE_ROLES включает deputy)."""
    _bot, _api, conn = vk
    link(conn, vk_id=VK_ID, tg_id=DEPUTY_TG)

    answers = await send(vk, keyboards.BTN_MY_GROUP)

    assert INVITE_CODE in "\n".join(answers), answers


async def test_starosta_flag_from_storage(vk) -> None:
    """storage.is_group_admin согласован с ролью в БД.

    Связку пересоздаём: один VK-аккаунт связан ровно с одним Telegram, поэтому
    перед сменой TG старую связку нужно снять (как это делает /unlink).
    """
    _bot, _api, conn = vk

    link(conn, vk_id=VK_ID, tg_id=STAROSTA_TG)
    assert storage.is_group_admin(conn, VK_ID) is True

    # Снимаем связку со стороны VK и привязываем обычного студента.
    storage.unlink_account(conn, VK_ID)
    link(conn, vk_id=VK_ID, tg_id=TG_ID)
    assert storage.is_group_admin(conn, VK_ID) is False


async def test_mygroup_command_works_like_button(vk) -> None:
    """Команда /mygroup делает то же, что кнопка (как в TG)."""
    _bot, _api, conn = vk
    link(conn)

    answers = await send(vk, "/mygroup")

    assert any(GROUP in a for a in answers), answers


# --- длинный список ---

def test_long_list_is_truncated() -> None:
    """20+ студентов: показываем первые 15 и «и ещё N»."""
    snapshot = {
        "group_name": GROUP,
        "count": 22,
        "students": [
            {"full_name": f"Студент Номер {index}", "role": "student"}
            for index in range(22)
        ],
        "starosta_name": "Абрамчик С.Г.",
        "deputy_name": "",
        "invite_code": INVITE_CODE,
    }

    text = render_my_group(snapshot)

    assert text.count("• ") == texts.MY_GROUP_LIST_LIMIT, text
    assert "и ещё 7" in text, text


def test_short_list_is_not_truncated() -> None:
    """Короткий список печатается целиком, без «и ещё»."""
    snapshot = {
        "group_name": GROUP,
        "count": 2,
        "students": [
            {"full_name": "Иванов И.И.", "role": "student"},
            {"full_name": "Петров П.П.", "role": "student"},
        ],
        "starosta_name": "",
        "deputy_name": "",
        "invite_code": "",
    }

    text = render_my_group(snapshot)

    assert "и ещё" not in text, text
    assert text.count("• ") == 2, text


def test_render_has_no_html_tags() -> None:
    """VK-формат — plain text: HTML-тегов в ответе быть не должно."""
    snapshot = {
        "group_name": GROUP,
        "count": 1,
        "students": [{"full_name": "Иванов И.И.", "role": "student"}],
        "starosta_name": "Абрамчик С.Г.",
        "deputy_name": "Богатырева И.П.",
        "invite_code": INVITE_CODE,
    }

    text = render_my_group(snapshot, is_admin=True)

    assert "<b>" not in text and "</b>" not in text, text
    assert "<code>" not in text and "</code>" not in text, text


def test_render_marks_roles_in_list() -> None:
    """В списке роль помечается иконкой, как в TG."""
    snapshot = {
        "group_name": GROUP,
        "count": 2,
        "students": [
            {"full_name": "Абрамчик С.Г.", "role": "starosta"},
            {"full_name": "Богатырева И.П.", "role": "deputy"},
        ],
        "starosta_name": "Абрамчик С.Г.",
        "deputy_name": "Богатырева И.П.",
        "invite_code": "",
    }

    text = render_my_group(snapshot)

    assert "Абрамчик С.Г. — ⭐ староста" in text, text
    assert "Богатырева И.П. — ⭐ зам" in text, text
# --- согласованность с TG ---

async def test_group_data_shared_with_telegram(vk) -> None:
    """Данные группы общие с TG: storage читает те же таблицы.

    Студент, добавленный «со стороны Telegram» (``att_db.insert_student``),
    сразу виден в VK — иначе зеркало было бы фиктивным.
    """
    _bot, _api, conn = vk
    link(conn)

    att_db.insert_student(conn, 555666777, GROUP, "Новиков Н.Н.")

    answers = await send(vk, keyboards.BTN_MY_GROUP)
    joined = "\n".join(answers)

    assert "Новиков Н.Н." in joined, answers
    assert "Студентов: 4" in joined, answers


def test_group_snapshot_matches_db(vk) -> None:
    """group_snapshot собирает данные без расхождений с БД."""
    _bot, _api, conn = vk

    snapshot = storage.group_snapshot(conn, GROUP)

    assert snapshot["group_name"] == GROUP
    assert snapshot["count"] == 3
    assert snapshot["invite_code"] == INVITE_CODE
    assert snapshot["starosta_name"] == "Абрамчик С.Г."
    assert snapshot["deputy_name"] == "Богатырева И.П."
    assert len(snapshot["students"]) == 3


def test_group_snapshot_for_unknown_group(vk) -> None:
    """Неизвестная группа: пустые значения, без исключений."""
    _bot, _api, conn = vk

    snapshot = storage.group_snapshot(conn, "99XXX")

    assert snapshot["count"] == 0
    assert snapshot["students"] == []
    assert snapshot["invite_code"] == ""


def test_students_sorted_alphabetically(vk) -> None:
    """Список группы отсортирован по ФИО — как в TG."""
    _bot, _api, conn = vk

    snapshot = storage.group_snapshot(conn, GROUP)
    names = [s["full_name"] for s in snapshot["students"]]

    assert names == sorted(names, key=str.lower), names


def test_role_of_defaults_to_student() -> None:
    """Без записи в БД роль — обычный студент, а не None."""
    assert storage.role_of(None) == storage.ROLE_STUDENT
    assert storage.role_of({}) == storage.ROLE_STUDENT
    assert storage.role_of({"role": "starosta"}) == storage.ROLE_STAROSTA


# --- изоляция от aiogram ---

def test_my_group_modules_do_not_import_aiogram() -> None:
    """storage/view/handlers VK не тянут aiogram.

    Раздел читает те же таблицы, что TG, но через прямой SQL: импорт
    ``bot.attendance`` поднял бы aiogram в процессе VK-бота (в его ``__init__``
    лежат Telegram-хендлеры).
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys;"
        "import bot_vk.handlers, bot_vk.storage, bot_vk.view,"
        " bot_vk.keyboards;"
        "print('aiogram' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout


def test_attendance_package_is_not_imported_in_vk() -> None:
    """VK-бот не импортирует пакет ``bot.attendance`` целиком.

    Именно он тянет aiogram. Проверяем в отдельном процессе: если кто-то
    вернёт импорт ``from bot.attendance import db``, тест упадёт.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys;"
        "import bot_vk.storage;"
        "print('bot.attendance' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", result.stdout