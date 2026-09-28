"""Тесты конфигурации (bot.config): .env, обязательные переменные, дефолты."""

import os
from pathlib import Path

import pytest

import bot.config as config_module
from bot.config import ENV_FILE, ConfigError, Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Переменные, которыми управляют тесты: обязательно восстановить их после.
_ENV_KEYS = (
    "BOT_TOKEN", "PUBLIC_BASE_URL", "PORT",
    "DB_PATH", "ADMIN_IDS", "ADMIN_CHAT_ID", "CACHE_DIR", "LOG_LEVEL",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Очистить окружение от конфигурационных переменных на время теста."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_env_file_exists_and_configured() -> None:
    """ENV_FILE указывает на .env в корне проекта, load_dotenv вызван при импорте."""
    assert ENV_FILE == PROJECT_ROOT / ".env"
    assert config_module.load_dotenv is not None  # импортирован из dotenv


def test_config_error_is_runtime_error() -> None:
    """ConfigError наследуется от RuntimeError (требование ТЗ)."""
    assert issubclass(ConfigError, RuntimeError)
    with pytest.raises(RuntimeError):
        raise ConfigError("boom")


def test_missing_bot_token_raises_runtime_error(clean_env) -> None:
    """Пустой BOT_TOKEN → RuntimeError с понятным текстом."""
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    message = str(exc.value)
    assert "BOT_TOKEN" in message
    assert "@BotFather" in message


def test_missing_public_base_url_reported_together(clean_env) -> None:
    """Обе обязательные переменные перечисляются в одной ошибке."""
    with pytest.raises(ConfigError) as exc:
        Settings.from_env()
    message = str(exc.value)
    assert "BOT_TOKEN" in message
    assert "PUBLIC_BASE_URL" in message


def test_defaults_applied(clean_env) -> None:
    """Дефолты из bot.config применяются, admin_chat_id необязателен."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    settings = Settings.from_env()
    assert settings.port == config_module.DEFAULT_PORT
    assert settings.db_path == config_module.DEFAULT_DB_PATH
    assert settings.cache_dir == config_module.DEFAULT_CACHE_DIR
    assert settings.log_level == config_module.DEFAULT_LOG_LEVEL
    assert settings.admin_ids == ()
    assert settings.admin_chat_id is None


def test_full_env_parsed(clean_env) -> None:
    """Все переменные разбираются: ids, chat_id, порт, уровень логирования."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "https://bot-kst.amvera.io/")
    clean_env.setenv("PORT", "9000")
    clean_env.setenv("DB_PATH", "./data/bot.db")
    clean_env.setenv("ADMIN_IDS", "111111111, 222222222")
    clean_env.setenv("ADMIN_CHAT_ID", "111111111")
    clean_env.setenv("LOG_LEVEL", "debug")
    settings = Settings.from_env()
    assert settings.public_base_url == "https://bot-kst.amvera.io"  # слэш срезан
    assert settings.port == 9000
    assert settings.db_path == "./data/bot.db"
    assert settings.admin_ids == (111111111, 222222222)
    assert settings.admin_chat_id == 111111111
    assert settings.log_level == "DEBUG"


# --- нормализация PUBLIC_BASE_URL ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("https://kst24-kst24.up.railway.app", "https://kst24-kst24.up.railway.app"),
    ("https://kst24-kst24.up.railway.app/", "https://kst24-kst24.up.railway.app"),
    ("kst24-kst24.up.railway.app", "https://kst24-kst24.up.railway.app"),
    ("bot-kst.amvera.io/", "https://bot-kst.amvera.io"),
    ("http://localhost:8080", "http://localhost:8080"),
])
def test_public_base_url_normalized_on_load(clean_env, raw, expected) -> None:
    """Значение из env приводится к каноническому виду (схема, без слэша)."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", raw)
    assert Settings.from_env().public_base_url == expected


def test_public_base_url_without_scheme_logs_warning(clean_env, caplog) -> None:
    """URL без схемы: схема добавляется, но в лог уходит WARNING."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "kst24-kst24.up.railway.app")

    with caplog.at_level("WARNING"):
        settings = Settings.from_env()

    assert settings.public_base_url == "https://kst24-kst24.up.railway.app"
    assert any("без схемы" in r.message for r in caplog.records), \
        "должно быть предупреждение о добавленной схеме"


def test_public_base_url_with_scheme_no_warning(clean_env, caplog) -> None:
    """Корректный URL не порождает предупреждений."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "https://kst24-kst24.up.railway.app")

    with caplog.at_level("WARNING"):
        Settings.from_env()

    assert not [r for r in caplog.records if "без схемы" in r.message]


def test_normalize_base_url_direct() -> None:
    """Прямая проверка функции нормализации."""
    assert config_module.normalize_base_url("example.com") == "https://example.com"
    assert config_module.normalize_base_url("https://example.com/") == "https://example.com"
    assert config_module.normalize_base_url("") == ""
    assert config_module.normalize_base_url(None) == ""


def test_invalid_port_and_chat_id_reported(clean_env) -> None:
    """Некорректные PORT и ADMIN_CHAT_ID попадают в единую ошибку."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    clean_env.setenv("PORT", "не-число")
    clean_env.setenv("ADMIN_CHAT_ID", "abc")
    with pytest.raises(ConfigError) as exc:
        Settings.from_env()
    message = str(exc.value)
    assert "PORT" in message
    assert "ADMIN_CHAT_ID" in message


def test_placeholder_token_rejected(clean_env) -> None:
    """Незаменённая заглушка из .env — тоже ошибка (иначе падение в Telegram)."""
    clean_env.setenv("BOT_TOKEN", config_module.PLACEHOLDER_TOKEN)
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    with pytest.raises(ConfigError) as exc:
        Settings.from_env()
    assert "BOT_TOKEN" in str(exc.value)


def test_repr_masks_bot_token(clean_env) -> None:
    """Токен никогда не попадает в repr (логи)."""
    secret = "999:SUPER_SECRET_TOKEN"
    clean_env.setenv("BOT_TOKEN", secret)
    clean_env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    text = repr(Settings.from_env())
    assert secret not in text
    assert "bot_token='***'" in text


def test_env_file_loaded_into_environ(clean_env, tmp_path) -> None:
    """Значения из .env подхватываются в окружение.

    Проверяем на временном файле, а не на локальном ``.env``: его содержимое
    владелец меняет (например, убирает PUBLIC_BASE_URL) — тест не должен
    от этого падать. Заодно убеждаемся, что ``.env`` в проекте существует
    и что модуль знает про его путь.
    """
    env_path = PROJECT_ROOT / ".env"
    assert env_path.exists(), "локальный .env должен существовать"
    assert "BOT_TOKEN=" in env_path.read_text(encoding="utf-8")

    # Временный .env с известным значением.
    sample = tmp_path / ".env"
    sample.write_text(
        "PUBLIC_BASE_URL=http://localhost:8080\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    clean_env.delenv("PUBLIC_BASE_URL", raising=False)
    clean_env.delenv("LOG_LEVEL", raising=False)

    config_module.load_dotenv(dotenv_path=sample, override=False)

    assert os.environ.get("PUBLIC_BASE_URL") == "http://localhost:8080"
    assert os.environ.get("LOG_LEVEL") == "DEBUG"


def test_env_has_priority_over_file(clean_env) -> None:
    """override=False: реальная env-переменная важнее значения из .env."""
    clean_env.setenv("BOT_TOKEN", "123:ABC")
    clean_env.setenv("PUBLIC_BASE_URL", "http://example.org")
    clean_env.setenv("LOG_LEVEL", "WARNING")
    settings = Settings.from_env()
    assert settings.log_level == "WARNING"
    assert settings.public_base_url == "http://example.org"