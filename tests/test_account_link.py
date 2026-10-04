"""Тесты связки аккаунтов Telegram ↔ VK (миграция 16).

Проверяется жизненный цикл одноразового кода и синхронизация группы между
платформами. Сети нет: работаем с БД в памяти.
"""

from datetime import datetime, timedelta

import pytest

from bot.config import TIMEZONE
from bot.db import (
    LINK_CODE_LENGTH,
    LINK_CODE_TTL_MINUTES,
    create_link_code,
    get_link_info,
    get_tg_id_by_vk,
    get_vk_id_by_tg,
    unlink_account,
    unlink_account_by_vk,
    use_link_code,
)
from bot.db import get_connection
from bot.migrations import apply_migrations

TG_ID = 908084777
VK_ID = 123456789
OTHER_VK = 555000111
OTHER_TG = 111222333


@pytest.fixture()
def conn(tmp_path):
    """БД со всеми миграциями (включая 16)."""
    connection = get_connection(tmp_path / "link.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def now() -> datetime:
    """Фиксированный момент, чтобы сроки жизни кодов были детерминированы."""
    return datetime(2026, 10, 4, 12, 0, tzinfo=TIMEZONE)
# --- миграция ---

def test_migration_creates_tables(conn) -> None:
    """Миграция 16 создаёт account_links и link_codes."""
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "account_links" in tables
    assert "link_codes" in tables


def test_account_links_columns(conn) -> None:
    """У связки есть обе стороны, время и признак, откуда связывали."""
    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(account_links)")}
    assert columns == {"id", "tg_id", "vk_id", "linked_at", "linked_via"}


def test_link_codes_columns(conn) -> None:
    """У кода есть срок жизни и отметка использования."""
    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(link_codes)")}
    assert columns == {"code", "tg_id", "created_at", "expires_at", "used_at"}


def test_users_table_untouched(conn) -> None:
    """Telegram-таблица users не менялась — TG-бот работает как раньше."""
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    assert columns == [
        "tg_id", "group_name", "full_name", "is_active", "created_at",
        "notifications_enabled",
    ]


# --- create_link_code ---

def test_create_link_code_returns_six_chars(conn, now) -> None:
    """Код — 6 символов из безопасного алфавита, в верхнем регистре."""
    code = create_link_code(conn, TG_ID, now=now)

    assert len(code) == LINK_CODE_LENGTH
    assert code == code.upper()
    # Без неоднозначных символов: их легко переврать при переписывании.
    assert not set(code) & set("OI01")


def test_create_link_code_saves_row(conn, now) -> None:
    """Код сохранён со сроком жизни 30 минут и без отметки использования."""
    code = create_link_code(conn, TG_ID, now=now)
    row = conn.execute(
        "SELECT * FROM link_codes WHERE code = ?", (code,)
    ).fetchone()

    assert row["tg_id"] == TG_ID
    assert row["used_at"] is None
    expires = datetime.fromisoformat(row["expires_at"])
    assert expires == now + timedelta(minutes=LINK_CODE_TTL_MINUTES)


def test_create_link_code_deletes_previous_unused(conn, now) -> None:
    """Повторный вызов удаляет прежний неиспользованный код."""
    first = create_link_code(conn, TG_ID, now=now)
    second = create_link_code(conn, TG_ID, now=now)

    assert first != second
    still_there = conn.execute(
        "SELECT COUNT(*) FROM link_codes WHERE code = ?", (first,)
    ).fetchone()[0]
    assert still_there == 0, "старый код должен быть удалён"

    active = conn.execute(
        "SELECT COUNT(*) FROM link_codes WHERE tg_id = ? AND used_at IS NULL",
        (TG_ID,),
    ).fetchone()[0]
    assert active == 1, "у пользователя ровно один действующий код"


def test_create_link_code_keeps_used_codes(conn, now) -> None:
    """Использованный код не удаляется — по нему видно историю связки."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)
    create_link_code(conn, TG_ID, now=now)

    assert conn.execute(
        "SELECT COUNT(*) FROM link_codes WHERE code = ?", (code,)
    ).fetchone()[0] == 1
# --- use_link_code ---

def test_use_link_code_success(conn, now) -> None:
    """Верный код связывает аккаунты и возвращает tg_id."""
    code = create_link_code(conn, TG_ID, now=now)
    result = use_link_code(conn, code, VK_ID, now=now)

    assert result["ok"] is True
    assert result["tg_id"] == TG_ID
    assert result["error"] is None
    assert get_vk_id_by_tg(conn, TG_ID) == VK_ID
    assert get_tg_id_by_vk(conn, VK_ID) == TG_ID


def test_use_link_code_accepts_lowercase(conn, now) -> None:
    """Регистр кода не важен: его переписывают руками."""
    code = create_link_code(conn, TG_ID, now=now)
    assert use_link_code(conn, code.lower(), VK_ID, now=now)["ok"] is True


def test_use_link_code_strips_spaces(conn, now) -> None:
    """Пробелы вокруг кода не мешают."""
    code = create_link_code(conn, TG_ID, now=now)
    assert use_link_code(conn, f"  {code}  ", VK_ID, now=now)["ok"] is True


def test_use_link_code_unknown(conn, now) -> None:
    """Несуществующий код — ошибка, связки не появляется."""
    result = use_link_code(conn, "ZZZZZZ", VK_ID, now=now)

    assert result["ok"] is False
    assert "не найден" in result["error"]
    assert get_vk_id_by_tg(conn, TG_ID) is None


def test_use_link_code_empty(conn, now) -> None:
    """Пустой код — ошибка."""
    assert use_link_code(conn, "", VK_ID, now=now)["ok"] is False


def test_use_link_code_twice(conn, now) -> None:
    """Код одноразовый: второй раз им связаться нельзя."""
    code = create_link_code(conn, TG_ID, now=now)
    assert use_link_code(conn, code, VK_ID, now=now)["ok"] is True

    second = use_link_code(conn, code, OTHER_VK, now=now)
    assert second["ok"] is False
    assert "использован" in second["error"]


def test_use_link_code_expired(conn, now) -> None:
    """Просроченный код не принимается."""
    code = create_link_code(conn, TG_ID, now=now - timedelta(minutes=31))
    result = use_link_code(conn, code, VK_ID, now=now)

    assert result["ok"] is False
    assert "истёк" in result["error"]
    assert get_vk_id_by_tg(conn, TG_ID) is None


def test_use_link_code_exactly_at_expiry(conn, now) -> None:
    """На границе срока код уже не действует."""
    code = create_link_code(conn, TG_ID, now=now)
    result = use_link_code(
        conn, code, VK_ID, now=now + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    )
    assert result["ok"] is False


def test_vk_already_linked_to_other_tg(conn, now) -> None:
    """VK, связанный с другим Telegram, не перепривязывается."""
    first = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, first, VK_ID, now=now)

    second = create_link_code(conn, OTHER_TG, now=now)
    result = use_link_code(conn, second, VK_ID, now=now)

    assert result["ok"] is False
    assert "другим Telegram" in result["error"]
    assert get_tg_id_by_vk(conn, VK_ID) == TG_ID, "прежняя связка цела"
    assert get_vk_id_by_tg(conn, OTHER_TG) is None


def test_tg_already_linked_to_other_vk(conn, now) -> None:
    """Telegram, связанный с другим VK, не перепривязывается."""
    first = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, first, VK_ID, now=now)

    second = create_link_code(conn, TG_ID, now=now)
    result = use_link_code(conn, second, OTHER_VK, now=now)

    assert result["ok"] is False
    assert "уже связан с другим VK" in result["error"]
    assert get_vk_id_by_tg(conn, TG_ID) == VK_ID


def test_use_link_code_idempotent_for_same_pair(conn, now) -> None:
    """Повторная связка той же пары не считается ошибкой."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)

    again = create_link_code(conn, TG_ID, now=now)
    result = use_link_code(conn, again, VK_ID, now=now)

    assert result["ok"] is True
    assert result["tg_id"] == TG_ID


# --- геттеры ---

def test_getters_return_none_when_not_linked(conn) -> None:
    """Без связки оба геттера возвращают None."""
    assert get_vk_id_by_tg(conn, TG_ID) is None
    assert get_tg_id_by_vk(conn, VK_ID) is None
    assert get_link_info(conn, tg_id=TG_ID) is None


def test_link_info_content(conn, now) -> None:
    """Информация о связке содержит обе стороны и способ связки."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)

    info = get_link_info(conn, tg_id=TG_ID)
    assert info["tg_id"] == TG_ID
    assert info["vk_id"] == VK_ID
    assert info["linked_via"] == "from_vk"
    assert info["linked_at"]


def test_link_info_by_vk(conn, now) -> None:
    """Ту же связку видно и со стороны VK."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)

    assert get_link_info(conn, vk_id=VK_ID) == get_link_info(conn, tg_id=TG_ID)


# --- отвязка ---

def test_unlink_account(conn, now) -> None:
    """Отвязка удаляет связку и возвращает True один раз."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)

    assert unlink_account(conn, TG_ID) is True
    assert get_vk_id_by_tg(conn, TG_ID) is None
    assert get_tg_id_by_vk(conn, VK_ID) is None
    assert unlink_account(conn, TG_ID) is False, "повторно удалять нечего"


def test_unlink_by_vk(conn, now) -> None:
    """Отвязаться можно и со стороны VK."""
    code = create_link_code(conn, TG_ID, now=now)
    use_link_code(conn, code, VK_ID, now=now)

    assert unlink_account_by_vk(conn, VK_ID) is True
    assert get_vk_id_by_tg(conn, TG_ID) is None


def test_unlink_unknown(conn) -> None:
    """Отвязка неизвестного аккаунта — False, без падения."""
    assert unlink_account(conn, TG_ID) is False
    assert unlink_account_by_vk(conn, VK_ID) is False