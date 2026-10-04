"""Тесты синхронизации группы между связанными TG- и VK-аккаунтами.

Проверяется, что смена группы на одной платформе видна на другой, и что
несвязанные аккаунты друг на друга не влияют.
"""

import pytest

from bot.db import (
    create_link_code,
    get_effective_group,
    get_user_group,
    get_vk_group,
    update_user_group_only,
    update_vk_user_group,
    use_link_code,
)
from bot.db import get_connection
from bot.migrations import apply_migrations

TG_ID = 908084777
VK_ID = 123456789
OTHER_VK = 555000111

GROUP_TG = "25КАД"
GROUP_VK = "26ИМС1"


@pytest.fixture()
def conn(tmp_path):
    """БД со всеми миграциями."""
    connection = get_connection(tmp_path / "sync.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def linked(conn):
    """Связанная пара TG ↔ VK: TG уже выбрал группу."""
    update_user_group_only(conn, TG_ID, GROUP_TG, "Иван Петров")
    code = create_link_code(conn, TG_ID)
    assert use_link_code(conn, code, VK_ID)["ok"] is True
    return conn
# --- связка переносит группу ---

def test_link_copies_tg_group_to_vk(linked) -> None:
    """После связки обе стороны видят одну группу.

    Перенос делает VK-хендлер ``/link``; здесь проверяется уровень данных: если
    в VK записана группа из TG, обе стороны согласованы.
    """
    conn = linked
    update_vk_user_group(conn, VK_ID, get_user_group(conn, TG_ID) or "")

    assert get_user_group(conn, TG_ID) == GROUP_TG
    assert get_vk_group(conn, VK_ID) == GROUP_TG
    assert get_effective_group(conn, tg_id=TG_ID) == GROUP_TG
    assert get_effective_group(conn, vk_id=VK_ID) == GROUP_TG


# --- смена в TG отражается в VK ---

def test_tg_group_change_syncs_to_vk(linked) -> None:
    """Смена группы в TG меняет её и в VK."""
    conn = linked

    update_user_group_only(conn, TG_ID, GROUP_VK, "Иван Петров")

    assert get_user_group(conn, TG_ID) == GROUP_VK
    assert get_vk_group(conn, VK_ID) == GROUP_VK, "VK обязан догнать TG"


def test_tg_group_change_without_link_does_not_touch_vk(conn) -> None:
    """Без связки смена группы в TG не создаёт VK-запись."""
    update_user_group_only(conn, TG_ID, GROUP_TG, "Иван")

    assert get_user_group(conn, TG_ID) == GROUP_TG
    assert get_vk_group(conn, VK_ID) is None


# --- смена в VK ---

def test_vk_group_change_reflected_for_vk_side(linked) -> None:
    """Смена группы в VK видна со стороны VK."""
    conn = linked

    update_vk_user_group(conn, VK_ID, GROUP_VK)

    assert get_vk_group(conn, VK_ID) == GROUP_VK


# --- приоритет при расхождении ---

def test_effective_group_prefers_tg(linked) -> None:
    """Если значения разошлись, приоритет у Telegram (ведущая платформа)."""
    conn = linked
    # Прямая запись в vk_users в обход синхронизации — имитация расхождения.
    update_vk_user_group(conn, VK_ID, GROUP_VK)

    assert get_effective_group(conn, tg_id=TG_ID) == GROUP_TG
    assert get_effective_group(conn, vk_id=VK_ID) == GROUP_TG


def test_effective_group_without_any_group(conn) -> None:
    """Если группы нет нигде — None, а не пустая строка."""
    assert get_effective_group(conn, tg_id=TG_ID) is None
    assert get_effective_group(conn, vk_id=VK_ID) is None


def test_effective_group_only_vk(conn) -> None:
    """Пользователь только в VK: группа берётся из vk_users."""
    update_vk_user_group(conn, VK_ID, GROUP_VK)

    assert get_effective_group(conn, vk_id=VK_ID) == GROUP_VK
    assert get_effective_group(conn, tg_id=TG_ID) is None


def test_effective_group_no_arguments(conn) -> None:
    """Без аргументов функция ничего не находит и не падает."""
    assert get_effective_group(conn) is None


# --- отвязка не ломает группы ---

def test_unlink_keeps_both_groups(linked) -> None:
    """После отвязки у каждой платформы остаётся своя группа."""
    from bot.db import unlink_account

    conn = linked
    # Группа VK: её записывает VK-хендлер /link, здесь имитируем результат.
    update_vk_user_group(conn, VK_ID, GROUP_TG)

    assert unlink_account(conn, TG_ID) is True

    assert get_user_group(conn, TG_ID) == GROUP_TG
    assert get_vk_group(conn, VK_ID) == GROUP_TG, "VK-группа сохраняется"
    # Связки больше нет — стороны независимы.
    update_vk_user_group(conn, VK_ID, GROUP_VK)
    assert get_vk_group(conn, VK_ID) == GROUP_VK
    assert get_user_group(conn, TG_ID) == GROUP_TG, "TG не поехал за VK"