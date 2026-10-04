"""E2E-тесты хендлеров VK-бота: реальный диспетчер, своя БД, без сети.

Подменяется только транспорт (``api.messages``): роутер, правила, состояния и
мини-типы — настоящие. Апдейты ``message_new`` прогоняются через
``bot.process_event``, поэтому проверяется фактическое поведение бота:
на что он отвечает и что сохраняет.

БД — временный файл с реальными миграциями и наполненным ``schedule_cache``,
чтобы расписание собиралось тем же кодом, что и в проде.
"""

from datetime import date

import pytest

from bot.db import get_connection
from bot.migrations import apply_migrations
from bot_vk import storage, texts
from bot_vk.handlers import UserState, register_handlers

GROUP_ID = 987654321
VK_ID = 777

# Группа, которую «находит» бот. Вторая нужна для проверки подсказок.
GROUP = "25КАД"

# День недели, на который кладём пары в кэш. Понедельник (1) есть в любой
# неделе, поэтому расписание найдётся независимо от дня прогона тестов.
CACHED_WEEKDAY = 1


class FakeMessagesAPI:
    """Мини-``api.messages``: ``Message.answer`` собирает параметры через
    ``get_set_params`` и шлёт их в ``send``."""

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
    """Сырой апдейт ``message_new`` с заданным текстом.

    В личном диалоге ``peer_id`` равен ``from_id`` — как у настоящего VK.
    """
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
    """БД с применёнными миграциями и заполненным кэшем расписания."""
    connection = get_connection(tmp_path / "vk.db")
    apply_migrations(connection)

    rows = [
        (GROUP, CACHED_WEEKDAY, 1, "Математика", "Соколова Е.В.", "204", ""),
        (GROUP, CACHED_WEEKDAY, 2, "Физика", "Тауснев В.Н.", "316А", ""),
        ("26ИМС1", CACHED_WEEKDAY, 1, "История", "Петров П.П.", "101", ""),
    ]
    connection.executemany(
        "INSERT INTO schedule_cache"
        " (group_name, day_of_week, para_number, subject, teacher, room,"
        "  week_type, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00')",
        rows,
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def monday() -> date:
    """Ближайший понедельник — день, на который в кэше есть пары.

    Даты в тестах считаем от него, а не от «сегодня»: иначе прогон в
    воскресенье или в другой день недели менял бы ожидаемый результат.
    """
    from datetime import timedelta

    today = date.today()
    return today + timedelta(days=(1 - today.isoweekday()) % 7)


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


def keyboard_labels(keyboard: str) -> list[str]:
    """Подписи кнопок из JSON-строки клавиатуры VK.

    В сыром виде эмодзи экранированы (``\\ud83d\\udcc6``), поэтому разбираем
    JSON, а не сравниваем подстроки в строке.
    """
    import json

    data = json.loads(keyboard)
    return [
        button["action"]["label"]
        for row in data.get("buttons", [])
        for button in row
    ]
# --- /start без группы ---

async def test_start_without_group_asks_and_sets_state(vk) -> None:
    """Новый пользователь: бот просит группу и переводит в waiting_group."""
    bot, _api, conn = vk

    answers = await send(vk, "/start")

    assert any(texts.ASK_GROUP == answer for answer in answers), answers
    # Состояние выставлено: следующий текст пойдёт в process_group.
    peer_state = await bot.state_dispenser.get(VK_ID)
    assert peer_state is not None
    assert peer_state.state == UserState.waiting_group
    assert storage.get_user_group(conn, VK_ID) is None


# --- регистрация группы ---

async def test_group_input_saves_and_shows_schedule(vk) -> None:
    """Ввод корректной группы: сохранение + расписание.

    Пары в кэше лежат на понедельник, поэтому тест не зависит от дня прогона:
    проверяем, что после сохранения бот присылает расписание (заголовок с
    группой и датой), а сами пары ищем в отдельном тесте — он смотрит
    понедельник явно.
    """
    _bot, _api, conn = vk

    await send(vk, "/start")
    answers = await send(vk, GROUP)

    assert any(texts.GROUP_SAVED.format(group=GROUP) in a for a in answers), answers
    assert storage.get_user_group(conn, VK_ID) == GROUP
    joined = "\n".join(answers)
    assert GROUP in joined and "🎓" in joined, answers


async def test_group_input_is_normalized(vk) -> None:
    """«25 кад» с пробелами и другим регистром тоже принимается."""
    _bot, _api, conn = vk

    await send(vk, "/start")
    await send(vk, "25 кад")

    assert storage.get_user_group(conn, VK_ID) == GROUP


async def test_wrong_group_shows_suggestions(vk) -> None:
    """Опечатка: группа не сохраняется, бот предлагает похожие варианты.

    «25КД» слишком похоже на «25КАД» (difflib ≈ 0.89), поэтому нечёткое
    совпадение намеренно НЕ сохраняется — иначе студент получал бы чужое
    расписание. Вместо этого приходят подсказки.
    """
    _bot, _api, conn = vk

    await send(vk, "/start")
    answers = await send(vk, "25КД")

    joined = "\n".join(answers)
    assert "не найдена" in joined, answers
    assert "Возможно" in joined, answers
    assert storage.get_user_group(conn, VK_ID) is None


async def test_phrase_input_asks_again(vk) -> None:
    """Фраза вместо номера: просим ввести номер, в поиск не уходим."""
    _bot, _api, conn = vk

    await send(vk, "/start")
    answers = await send(vk, "не знаю")

    assert any(texts.GROUP_INVALID == a for a in answers), answers
    assert storage.get_user_group(conn, VK_ID) is None


async def test_cancel_leaves_no_group(vk) -> None:
    """«Отмена» прерывает ввод и снимает состояние."""
    bot, _api, conn = vk

    await send(vk, "/start")
    answers = await send(vk, "Отмена")

    assert any(texts.GROUP_CANCELLED == a for a in answers), answers
    assert storage.get_user_group(conn, VK_ID) is None
    assert await bot.state_dispenser.get(VK_ID) is None


# --- /start с уже сохранённой группой ---

async def test_start_with_group_shows_menu(vk) -> None:
    """Повторный /start: приветствие с группой и клавиатура меню."""
    _bot, api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    answers = await send(vk, "/start")

    assert any(GROUP in a for a in answers), answers
    keyboard = api.messages.sent[0].get("keyboard")
    assert keyboard, "должна прийти клавиатура главного меню"
    # Клавиатура приходит JSON-строкой; разбираем её и смотрим подписи кнопок.
    labels = keyboard_labels(keyboard)
    assert any("Сегодня" in label for label in labels), labels
    assert any("Профиль" in label for label in labels), labels


# --- расписание ---

async def test_today_shows_schedule(vk) -> None:
    """«📆 Сегодня» показывает пары из кэша (или дружелюбную строку)."""
    _bot, _api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    answers = await send(vk, "📆 Сегодня")
    joined = "\n".join(answers)

    assert GROUP in joined, answers
    assert ("Математика" in joined) or ("Пар нет" in joined), answers


async def test_today_without_group_asks_for_it(vk) -> None:
    """Без группы расписание не показываем — просим указать группу."""
    bot, _api, _conn = vk

    answers = await send(vk, "📆 Сегодня")

    assert any(texts.NO_GROUP_HINT == a for a in answers), answers
    assert any(texts.ASK_GROUP == a for a in answers), answers
    peer_state = await bot.state_dispenser.get(VK_ID)
    assert peer_state is not None and peer_state.state == UserState.waiting_group


async def test_week_shows_multiple_days(vk) -> None:
    """«📅 Неделя» отдаёт расписание на несколько дней подряд."""
    _bot, _api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    answers = await send(vk, "📅 Неделя")
    joined = "\n".join(answers)

    assert texts.WEEK_HEADER.format(group=GROUP) in joined, answers
    # 7 = заголовок недели + шесть шапок дней.
    assert joined.count("🎓") >= 7, answers


# --- профиль ---

async def test_profile_shows_group(vk) -> None:
    """«👤 Профиль» показывает группу и платформу."""
    _bot, _api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    answers = await send(vk, "👤 Профиль")
    joined = "\n".join(answers)

    assert GROUP in joined, answers
    assert "VK" in joined, answers


async def test_profile_without_group(vk) -> None:
    """Профиль без группы: подсказка, что группа не указана."""
    _bot, _api, _conn = vk

    answers = await send(vk, "👤 Профиль")

    assert any("не указана" in a for a in answers), answers


# --- fallback ---

async def test_fallback_on_unknown(vk) -> None:
    """Неизвестный текст — подсказка со списком команд."""
    _bot, _api, _conn = vk

    answers = await send(vk, "абракадабра")

    assert any(texts.FALLBACK == a for a in answers), answers


# --- рендеринг расписания и замен (детерминированно, от понедельника) ---

def test_render_day_shows_lessons(conn, monday) -> None:
    """День с парами: карточки с предметом, преподавателем, кабинетом и временем."""
    from bot_vk import view

    lessons = view.lessons_with_substitutions(conn, GROUP, monday)
    text = view.render_day(GROUP, monday, lessons)

    assert "Математика" in text
    assert "Соколова Е.В." in text
    assert "каб. 204" in text
    assert "⏰" in text, "время пары должно быть в карточке"
    assert GROUP in text


def test_render_day_icons_for_planned(conn, monday) -> None:
    """Плановая пара помечается 📚."""
    from bot_vk import view

    lessons = view.lessons_with_substitutions(conn, GROUP, monday)
    text = view.render_day(GROUP, monday, lessons)

    assert "📚 1 пара" in text


def test_render_day_marks_substitution(conn, monday) -> None:
    """Замена помечается 🔁 и показывает исходный предмет."""
    from bot_vk import view

    conn.execute(
        "INSERT INTO substitutions_cache"
        " (group_name, date_iso, para, old_subject, new_subject, teacher, room,"
        "  is_cancelled, fetched_at, is_self_study)"
        " VALUES (?, ?, 1, 'Математика', 'Физика', 'Тауснев В.Н.', '316А',"
        "         0, '2026-10-01T00:00:00', 0)",
        (GROUP, monday.isoformat()),
    )
    conn.commit()

    lessons = view.lessons_with_substitutions(conn, GROUP, monday)
    text = view.render_day(GROUP, monday, lessons)

    assert "🔁 1 пара" in text
    assert "Физика" in text
    assert "было Математика" in text


def test_render_day_marks_cancelled(conn, monday) -> None:
    """Отменённая пара помечается ❌ и подписывается «отменена»."""
    from bot_vk import view

    conn.execute(
        "INSERT INTO substitutions_cache"
        " (group_name, date_iso, para, old_subject, new_subject, teacher, room,"
        "  is_cancelled, fetched_at, is_self_study)"
        " VALUES (?, ?, 1, 'Математика', 'Математика', '', '',"
        "         1, '2026-10-01T00:00:00', 0)",
        (GROUP, monday.isoformat()),
    )
    conn.commit()

    lessons = view.lessons_with_substitutions(conn, GROUP, monday)
    text = view.render_day(GROUP, monday, lessons)

    assert "❌ 1 пара" in text
    assert "отменена" in text


def test_render_week_covers_six_days(conn, monday) -> None:
    """Неделя: шесть дней, каждый со своей датой и чётностью числа."""
    from bot_vk import view

    chunks = view.render_week(conn, GROUP, monday, days=6)
    text = "\n".join(chunks)

    assert texts.WEEK_HEADER.format(group=GROUP) in text
    # 7 = заголовок недели (🎓 один раз) + 6 шапок дней.
    assert text.count("🎓") == 7, text


def test_render_week_respects_message_limit(conn, monday) -> None:
    """Неделя режется по лимиту длины сообщения VK."""
    from bot_vk import view
    from bot_vk.config import VK_MESSAGE_LIMIT

    chunks = view.render_week(conn, GROUP, monday, days=6)

    assert chunks, "должно быть хотя бы одно сообщение"
    for chunk in chunks:
        assert len(chunk) <= VK_MESSAGE_LIMIT, len(chunk)


def test_empty_day_message(conn) -> None:
    """День без пар — дружелюбная строка, а не пустота."""
    from bot_vk import view

    text = view.render_day(GROUP, date(2026, 10, 4), [])

    assert texts.DAY_EMPTY in text


# --- хранилище ---

def test_storage_saves_and_reads_group(conn) -> None:
    """Группа сохраняется и читается; повторная запись обновляет её."""
    assert storage.get_user_group(conn, VK_ID) is None

    storage.save_user_group(conn, VK_ID, GROUP, full_name="Иван")
    assert storage.get_user_group(conn, VK_ID) == GROUP
    assert storage.get_user(conn, VK_ID)["full_name"] == "Иван"

    storage.save_user_group(conn, VK_ID, "26ИМС1")
    assert storage.get_user_group(conn, VK_ID) == "26ИМС1"
    # Имя не затёрто пустой строкой.
    assert storage.get_user(conn, VK_ID)["full_name"] == "Иван"


def test_storage_exact_and_fuzzy(conn) -> None:
    """Точное совпадение находит группу, нечёткое — только предлагает."""
    assert storage.find_exact_group(conn, "25КАД") == GROUP
    assert storage.find_exact_group(conn, "25 кад") == GROUP
    assert storage.find_exact_group(conn, "25КД") is None

    # Нечёткий поиск возвращает кандидата — для подсказок этого достаточно.
    assert storage.find_group(conn, "25КД") is not None
    assert storage.suggest_groups(conn, "25КД")


def test_storage_suggestions_limited(conn) -> None:
    """Подсказок не больше трёх."""
    from bot_vk.storage import MAX_SUGGESTIONS

    assert len(storage.suggest_groups(conn, "2")) <= MAX_SUGGESTIONS


# --- связка с Telegram (/link) ---

async def test_link_without_code_shows_instructions(vk) -> None:
    """/link без кода: бот объясняет, где взять код, и не связывает."""
    _bot, _api, conn = vk

    answers = await send(vk, "/link")

    joined = "\n".join(answers)
    assert "Telegram" in joined, answers
    assert "/link" in joined, answers
    assert storage.get_tg_id_by_vk(conn, VK_ID) is None


async def test_link_with_invalid_code_reports_error(vk) -> None:
    """/link с несуществующим кодом: ошибка, связки нет."""
    _bot, _api, conn = vk

    answers = await send(vk, "/link ZZZZZZ")

    joined = "\n".join(answers)
    assert "не найден" in joined, answers
    assert storage.get_tg_id_by_vk(conn, VK_ID) is None


async def test_link_with_valid_code_creates_link(vk) -> None:
    """/link с верным кодом создаёт связку."""
    from bot.db import create_link_code

    _bot, _api, conn = vk
    tg_id = 908084777
    code = create_link_code(conn, tg_id)

    answers = await send(vk, f"/link {code}")

    joined = "\n".join(answers)
    assert "связаны" in joined.lower(), answers
    assert storage.get_tg_id_by_vk(conn, VK_ID) == tg_id


async def test_link_lowercase_code_accepted(vk) -> None:
    """Код в нижнем регистре тоже принимается (его переписывают руками)."""
    from bot.db import create_link_code

    _bot, _api, conn = vk
    tg_id = 908084777
    code = create_link_code(conn, tg_id)

    await send(vk, f"/link {code.lower()}")

    assert storage.get_tg_id_by_vk(conn, VK_ID) == tg_id


async def test_link_takes_group_from_telegram(vk) -> None:
    """После связки VK видит ту же группу, что выбрана в Telegram."""
    from bot.db import create_link_code, update_user_group_only

    _bot, _api, conn = vk
    tg_id = 908084777
    update_user_group_only(conn, tg_id, GROUP, "Иван Петров")
    code = create_link_code(conn, tg_id)

    answers = await send(vk, f"/link {code}")

    assert storage.get_user_group(conn, VK_ID) == GROUP
    assert any(GROUP in a for a in answers), answers


async def test_link_twice_reminds_about_existing(vk) -> None:
    """Повторный /link напоминает о существующей связке."""
    from bot.db import create_link_code

    _bot, _api, conn = vk
    tg_id = 908084777
    code = create_link_code(conn, tg_id)
    await send(vk, f"/link {code}")

    answers = await send(vk, "/link")

    assert any("уже связан" in a for a in answers), answers


async def test_unlink_removes_link(vk) -> None:
    """/unlink удаляет связку и не трогает группу."""
    from bot.db import create_link_code

    _bot, _api, conn = vk
    tg_id = 908084777
    code = create_link_code(conn, tg_id)
    await send(vk, f"/link {code}")
    storage.save_user_group(conn, VK_ID, GROUP)

    answers = await send(vk, "/unlink")

    assert any("удалена" in a for a in answers), answers
    assert storage.get_tg_id_by_vk(conn, VK_ID) is None
    assert storage.get_user_group(conn, VK_ID) == GROUP


async def test_unlink_without_link(vk) -> None:
    """/unlink без связки: сообщаем, что отвязывать нечего."""
    _bot, _api, _conn = vk

    answers = await send(vk, "/unlink")

    assert any("нет связанного" in a for a in answers), answers


async def test_profile_shows_link_status(vk) -> None:
    """Профиль показывает статус связки."""
    from bot.db import create_link_code

    _bot, _api, conn = vk
    storage.save_user_group(conn, VK_ID, GROUP)

    # Без связки.
    answers = await send(vk, "👤 Профиль")
    assert any("только VK" in a for a in answers), answers

    # Со связкой.
    tg_id = 908084777
    code = create_link_code(conn, tg_id)
    await send(vk, f"/link {code}")

    answers = await send(vk, "👤 Профиль")
    joined = "\n".join(answers)
    assert "с Telegram" in joined, answers
    assert str(tg_id) in joined, answers


# --- изоляция идентификаторов ---

async def test_vk_group_not_written_to_users(vk) -> None:
    """Группа VK-пользователя НЕ попадает в таблицу users.

    Иначе Telegram-рассылки приняли бы VK-id за tg_id и пытались отправить
    расписание в Telegram — потенциально постороннему человеку.
    """
    _bot, _api, conn = vk

    await send(vk, "/start")
    await send(vk, GROUP)

    in_users = conn.execute(
        "SELECT COUNT(*) FROM users WHERE tg_id = ?", (VK_ID,)
    ).fetchone()[0]
    assert in_users == 0, "VK-пользователь не должен попадать в users"
    assert storage.get_user_group(conn, VK_ID) == GROUP