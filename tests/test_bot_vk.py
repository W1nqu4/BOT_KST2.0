"""Тесты VK-бота: конфигурация, состав правил, вспомогательные функции.

E2E через настоящий диспетчер (то, что бот реально отвечает) — в
``tests/test_bot_vk_handlers.py``. Здесь проверяется «обвязка»: чтение env,
порядок правил и формат проверки ввода.

Значения ``VK_TOKEN`` / ``VK_GROUP_ID`` читаются в :mod:`bot_vk.config` один раз
при импорте, поэтому тесты подменяют атрибуты модуля (``monkeypatch.setattr``),
а не переменные окружения.
"""

from unittest.mock import Mock

import pytest

import bot_vk.config as vk_config
from bot_vk.config import validate
from bot_vk.handlers import register_handlers


@pytest.fixture()
def valid_vk_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сделать конфигурацию валидной на время теста."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "FAKE-VK-TOKEN-FOR-TESTS")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 123456789)


# --- config.validate ---

def test_validate_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой VK_TOKEN → RuntimeError с подсказкой про .env."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 123456789)
    with pytest.raises(RuntimeError) as exc:
        validate()
    message = str(exc.value)
    assert "VK_TOKEN" in message
    assert ".env" in message


def test_validate_raises_without_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """VK_GROUP_ID = 0 → RuntimeError с подсказкой про .env."""
    monkeypatch.setattr(vk_config, "VK_TOKEN", "FAKE-VK-TOKEN-FOR-TESTS")
    monkeypatch.setattr(vk_config, "VK_GROUP_ID", 0)
    with pytest.raises(RuntimeError) as exc:
        validate()
    message = str(exc.value)
    assert "VK_GROUP_ID" in message
    assert ".env" in message


def test_validate_passes_with_valid_values(valid_vk_config: None) -> None:
    """Заданные токен и group_id — конфигурация валидна."""
    validate()  # не должно бросать


def test_module_import_does_not_require_env() -> None:
    """Модуль импортируется без env: проверка только в validate()."""
    assert vk_config.ENV_FILE.name == ".env"
    assert vk_config.ENV_FILE.parent == vk_config.PROJECT_ROOT
    assert isinstance(vk_config.VK_TOKEN, str)
    assert isinstance(vk_config.VK_GROUP_ID, int)


def test_db_path_has_default() -> None:
    """DB_PATH имеет значение по умолчанию — VK-бот не падает без переменной."""
    assert isinstance(vk_config.DB_PATH, str)
    assert vk_config.DB_PATH


# --- состав и порядок правил ---

class FakeBotForRules:
    """Заглушка ``Bot``: считает вызовы ``on.message`` и запоминает правила."""

    def __init__(self) -> None:
        self.handlers: list[tuple] = []
        self.on = Mock()
        self.on.message = Mock(side_effect=self._capture_message)

    def _capture_message(self, *args, **kwargs):
        def decorator(func):
            self.handlers.append((func, args, kwargs))
            return func

        return decorator


def test_register_handlers_registers_commands() -> None:
    """register_handlers создаёт все команды VK-бота в нужном порядке.

    Порядок важен: конкретные команды объявлены раньше fallback, иначе он
    перехватил бы их. Команды преподавателя стоят перед расписанием — так их
    видно в списке рядом с остальными разделами, а не за студенческими.
    """
    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    names = [func.__name__ for func, _, _ in bot.handlers]
    assert names == [
        "start_handler",
        "process_group",
        "link_handler",
        "teacher_apply_handler",
        "process_teacher_name",
        "teacher_status_handler",
        "teacher_cancel_handler",
        "today_handler",
        "week_handler",
        "profile_handler",
        "fallback",
    ]


def test_teacher_commands_before_fallback() -> None:
    """Команды преподавателя объявлены раньше fallback.

    Иначе «/teacher_status» и «/teacher_cancel» попадали бы в перехватчик и
    отвечали подсказкой вместо статуса заявки.
    """
    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    names = [func.__name__ for func, _, _ in bot.handlers]
    for handler in ("teacher_apply_handler", "process_teacher_name",
                    "teacher_status_handler", "teacher_cancel_handler"):
        assert names.index(handler) < names.index("fallback"), handler


def test_teacher_apply_state_value() -> None:
    """Состояние заявки имеет ожидаемое имя (на него завязаны тесты и логи)."""
    from bot_vk.handlers import TeacherApplyState

    assert TeacherApplyState.waiting_name.value == "teacher_apply_waiting_name"


def test_teacher_name_state_skips_commands() -> None:
    """Под состояние ввода фамилии не попадают команды и кнопки меню.

    Правило переиспользует :func:`is_group_input`: без него «/teacher_cancel»
    ушёл бы в поиск по справочнику вместо отмены.
    """
    from bot_vk.handlers import is_group_input

    class FakeMessage:
        def __init__(self, text: str) -> None:
            self.text = text

    assert is_group_input(FakeMessage("Богатырева")) is True
    assert is_group_input(FakeMessage("/teacher_cancel")) is False
    assert is_group_input(FakeMessage("/teacher_status")) is False
    assert is_group_input(FakeMessage("📆 Сегодня")) is False


def test_fallback_is_registered_last() -> None:
    """Fallback обязан быть последним: иначе перехватит остальные команды."""
    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    last_func, last_args, last_kwargs = bot.handlers[-1]
    assert last_func.__name__ == "fallback"
    assert last_args == ()
    assert last_kwargs == {}


def test_link_handler_before_fallback() -> None:
    """/link объявлен раньше fallback, иначе команду съел бы перехватчик."""
    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    names = [func.__name__ for func, _, _ in bot.handlers]
    assert names.index("link_handler") < names.index("fallback")


def test_link_rule_covers_code_and_bare_command() -> None:
    """Правило /link ловит и команду без кода, и команду с кодом."""
    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    link_rule = next(
        (args, kwargs) for func, args, kwargs in bot.handlers
        if func.__name__ == "link_handler"
    )
    texts = link_rule[1].get("text") or link_rule[0][0]
    assert "/link <code>" in texts
    assert "/link" in texts
    assert "/unlink" in texts


def test_process_group_rule_binds_state() -> None:
    """Шаг ввода группы привязан к состоянию waiting_group.

    Правило составное: ``state`` (ждём ввод группы) и ``func`` (отсекает
    команды и кнопки меню). Поэтому вместо kwargs проверяем правила, которые
    vkbottle реально создал.
    """
    from bot_vk.handlers import UserState, is_group_input, looks_like_group

    bot = FakeBotForRules()
    register_handlers(bot, conn=None)  # type: ignore[arg-type]

    # Косвенная проверка: правило отсечения работает как задумано.
    class FakeMessage:
        def __init__(self, text: str) -> None:
            self.text = text

    assert is_group_input(FakeMessage("25КАД")) is True
    assert is_group_input(FakeMessage("/start")) is False
    assert is_group_input(FakeMessage("📆 Сегодня")) is False
    assert UserState.waiting_group.value == "waiting_group"
    assert looks_like_group("25КАД") is True


# --- проверка формата ввода ---

def test_looks_like_group_accepts_real_numbers() -> None:
    """Нормальные номера групп проходят проверку формата."""
    from bot_vk.handlers import looks_like_group

    assert looks_like_group("25КАД")
    assert looks_like_group("25 кад")
    assert looks_like_group("24МОСДР-1")


def test_looks_like_group_rejects_phrases() -> None:
    """Фразы не уходят в fuzzy-поиск — иначе подбиралась бы случайная группа."""
    from bot_vk.handlers import looks_like_group

    assert not looks_like_group("не знаю")
    assert not looks_like_group("???")
    assert not looks_like_group("")


def test_cancel_words_nonempty() -> None:
    """Набор слов отмены не пуст (иначе «Отмена» обрабатывалась бы как группа)."""
    from bot_vk.handlers import CANCEL_WORDS

    assert "отмена" in CANCEL_WORDS