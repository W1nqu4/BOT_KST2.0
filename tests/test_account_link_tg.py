"""Тесты TG-стороны связки аккаунтов: /link, /unlink, deep-link.

Проверяется выдача кода, инструкции и обработка ссылки
``t.me/<bot>?start=link_XXXXXX``. Сети нет: сообщение — заглушка с записью
ответов.
"""

import pytest

from bot.db import (
    create_link_code,
    get_link_info,
    get_vk_id_by_tg,
    update_user_group_only,
    use_link_code,
)
from bot.db import get_connection
from bot.handlers import account_link
from bot.migrations import apply_migrations

TG_ID = 908084777
VK_ID = 123456789


class FakeUser:
    """Пользователь Telegram (минимум, нужный хендлерам)."""

    def __init__(self, user_id: int = TG_ID) -> None:
        self.id = user_id
        self.full_name = "Иван Петров"


class FakeMessage:
    """Сообщение-заглушка: запоминает ответы бота."""

    def __init__(self, text: str = "", user_id: int = TG_ID) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.answers]


class FakeCommand:
    """Объект команды aiogram: нужен только ``args``."""

    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.fixture()
def conn(tmp_path):
    """БД со всеми миграциями."""
    connection = get_connection(tmp_path / "tg_link.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def code_from_message(text: str) -> str:
    """Вытащить код из текста инструкции (``<code>/link XXXXXX</code>``)."""
    return text.split("<code>/link ", 1)[1].split("</code>", 1)[0]


# --- /link ---

async def test_cmd_link_issues_code(conn) -> None:
    """/link выдаёт 6-символьный код и инструкцию для VK."""
    message = FakeMessage("/link")

    await account_link.cmd_link(message, conn)

    assert len(message.texts) == 1
    text, kwargs = message.answers[0]
    assert kwargs.get("parse_mode") == "HTML"
    assert "/link " in text

    # Код из сообщения действительно сохранён и готов к использованию.
    code = code_from_message(text)
    assert len(code) == 6
    result = use_link_code(conn, code, VK_ID)
    assert result["ok"] is True
    assert result["tg_id"] == TG_ID


async def test_cmd_link_mentions_ttl(conn) -> None:
    """В инструкции указан срок действия кода."""
    from bot.db import LINK_CODE_TTL_MINUTES

    message = FakeMessage("/link")
    await account_link.cmd_link(message, conn)

    assert str(LINK_CODE_TTL_MINUTES) in message.texts[0]


async def test_cmd_link_has_deeplink(conn) -> None:
    """В инструкции есть ссылка вида t.me/<bot>?start=link_XXX."""
    message = FakeMessage("/link")
    await account_link.cmd_link(message, conn)

    text = message.texts[0]
    assert f"t.me/{account_link.BOT_USERNAME}?start=link_" in text


async def test_cmd_link_twice_replaces_code(conn) -> None:
    """Повторный /link выдаёт новый код, старый перестаёт работать."""
    first = FakeMessage("/link")
    await account_link.cmd_link(first, conn)
    first_code = code_from_message(first.texts[0])

    second = FakeMessage("/link")
    await account_link.cmd_link(second, conn)
    second_code = code_from_message(second.texts[0])

    assert first_code != second_code
    assert use_link_code(conn, first_code, VK_ID)["ok"] is False
    assert use_link_code(conn, second_code, VK_ID)["ok"] is True


async def test_cmd_link_when_already_linked(conn) -> None:
    """Если связка есть, /link напоминает о ней и не выдаёт новый код."""
    code = create_link_code(conn, TG_ID)
    use_link_code(conn, code, VK_ID)

    message = FakeMessage("/link")
    await account_link.cmd_link(message, conn)

    assert "уже связан" in message.texts[0]
    assert str(VK_ID) in message.texts[0]
    assert "<code>" not in message.texts[0], "код не нужен"


# --- /unlink ---

async def test_cmd_unlink_removes(conn) -> None:
    """/unlink удаляет связку."""
    code = create_link_code(conn, TG_ID)
    use_link_code(conn, code, VK_ID)

    message = FakeMessage("/unlink")
    await account_link.cmd_unlink(message, conn)

    assert "удалена" in message.texts[0]
    assert get_vk_id_by_tg(conn, TG_ID) is None


async def test_cmd_unlink_without_link(conn) -> None:
    """/unlink без связки сообщает, что отвязывать нечего."""
    message = FakeMessage("/unlink")
    await account_link.cmd_unlink(message, conn)

    assert "нет связанного" in message.texts[0]


# --- deep-link ---

async def test_deeplink_repeats_code(conn) -> None:
    """Ссылка link_XXX повторяет код и инструкцию для VK."""
    message = FakeMessage("/start link_ABC123")

    await account_link.cmd_start_deeplink(
        message, FakeCommand("link_ABC123"), conn
    )

    text = message.texts[0]
    assert "ABC123" in text
    assert "/link ABC123" in text


async def test_deeplink_lowercase_payload(conn) -> None:
    """Префикс ссылки не зависит от регистра, код приводится к верхнему."""
    message = FakeMessage("/start link_abc123")

    await account_link.cmd_start_deeplink(
        message, FakeCommand("link_abc123"), conn
    )

    assert "ABC123" in message.texts[0]


async def test_deeplink_without_code(conn) -> None:
    """Пустая ссылка link_ — подсказка получить код."""
    message = FakeMessage("/start link_")

    await account_link.cmd_start_deeplink(message, FakeCommand("link_"), conn)

    assert "/link" in message.texts[0]


async def test_alien_deeplink_is_rejected(conn) -> None:
    """Чужая ссылка не считается связкой."""
    message = FakeMessage("/start promo")

    await account_link.cmd_start_deeplink(message, FakeCommand("promo"), conn)

    assert "Не понял" in message.texts[0]


async def test_deeplink_does_not_create_link(conn) -> None:
    """Deep-link сам связку не создаёт: её делает VK-бот, когда вводит код.

    Иначе связка появилась бы до открытия VK, а ссылка лежит в переписке и
    могла бы попасть к другому человеку — «одноразовость» кода теряла смысл.
    """
    message = FakeMessage("/start link_BBB222")

    await account_link.cmd_start_deeplink(
        message, FakeCommand("link_BBB222"), conn
    )

    assert get_link_info(conn, tg_id=TG_ID) is None


# --- синхронизация группы ---

async def test_sync_group_name_updates_vk(conn) -> None:
    """sync_group_name переносит группу в связанный VK."""
    from bot.db import get_vk_group

    update_user_group_only(conn, TG_ID, "25КАД", "Иван")
    code = create_link_code(conn, TG_ID)
    use_link_code(conn, code, VK_ID)

    account_link.sync_group_name(conn, TG_ID, "26ИМС1")

    assert get_vk_group(conn, VK_ID) == "26ИМС1"


async def test_sync_group_name_without_link_is_noop(conn) -> None:
    """Без связки синхронизация ничего не делает и не падает."""
    account_link.sync_group_name(conn, TG_ID, "25КАД")
    assert get_link_info(conn, tg_id=TG_ID) is None


async def test_group_change_in_db_syncs_via_update_user_group_only(conn) -> None:
    """Смена группы в TG автоматически доходит до VK (см. bot.db)."""
    from bot.db import get_vk_group

    code = create_link_code(conn, TG_ID)
    use_link_code(conn, code, VK_ID)

    # Обычный путь смены группы в Telegram-боте.
    update_user_group_only(conn, TG_ID, "24МОСДР1", "Иван")

    assert get_vk_group(conn, VK_ID) == "24МОСДР1"