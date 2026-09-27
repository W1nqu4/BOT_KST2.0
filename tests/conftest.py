"""Общие фикстуры тестов: пути к образцам источников и их URL для скачивания.

Образцы (`data/sample_schedule.docx`, `data/sample_substitutions.html`) в git
НЕ коммитятся — они в `.gitignore`. Тесты, которым нужен файл, скипаются через
``pytest.mark.skipif`` (см. фикстуры ниже), а не падают.

Чтобы получить образцы локально, выполните::

    python -c "import asyncio, tests.conftest as c; asyncio.run(c.download_samples())"

или просто откройте страницы в браузере и сохраните файлы под нужными именами.
"""

import asyncio
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

SAMPLE_SCHEDULE = DATA_DIR / "sample_schedule.docx"
SAMPLE_SUBSTITUTIONS = DATA_DIR / "sample_substitutions.html"

# Страницы-источники (те же, что в bot/config.py: SCHEDULE_PAGE_URL и
# SUBSTITUTIONS_PAGE_URL). DOCX лежит на странице расписания ссылкой.
SCHEDULE_PAGE_URL = "https://24kst.ru/студенту/расписание-занятий/"
SUBSTITUTIONS_PAGE_URL = "https://24kst.ru/студенту/лист-замен/"

# Прямая ссылка на DOCX расписания 1 семестра 2026/2027 (актуальна на сентябрь
# 2026). Название файла меняется каждый семестр — надёжнее искать ссылку на
# странице, см. bot.services.cache_service.extract_docx_url.
SAMPLE_DOCX_URL = (
    "https://24kst.ru/wp-content/uploads/2026/09/"
    "Расписание-занятий-1-семестр-2026-2027-уч.-год.docx"
)


@pytest.fixture()
def sample_schedule_path() -> Path:
    """Путь к образцу расписания; тест скипается, если файла нет."""
    if not SAMPLE_SCHEDULE.exists():
        pytest.skip(f"нет образца {SAMPLE_SCHEDULE.name} (см. tests/conftest.py)")
    return SAMPLE_SCHEDULE


@pytest.fixture()
def sample_substitutions_path() -> Path:
    """Путь к образцу листа замен; тест скипается, если файла нет."""
    if not SAMPLE_SUBSTITUTIONS.exists():
        pytest.skip(
            f"нет образца {SAMPLE_SUBSTITUTIONS.name} (см. tests/conftest.py)"
        )
    return SAMPLE_SUBSTITUTIONS


@pytest.fixture(scope="session")
def parsed_schedule() -> list[dict]:
    """Разбор образца расписания — один раз на сессию тестов.

    Разбор 400-КБ DOCX занимает около секунды; без кэша прогон тестов
    сервиса расписания растягивается на десятки секунд.
    """
    if not SAMPLE_SCHEDULE.exists():
        pytest.skip(f"нет образца {SAMPLE_SCHEDULE.name} (см. tests/conftest.py)")
    from bot.parsers.schedule import parse_docx

    lessons = parse_docx(SAMPLE_SCHEDULE)
    assert lessons, "образец расписания разобран пусто"
    return lessons


async def download_samples() -> None:
    """Скачать оба образца в ``data/`` (для ручного запуска, не для тестов)."""
    import aiohttp

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(SUBSTITUTIONS_PAGE_URL) as resp:
            SAMPLE_SUBSTITUTIONS.write_bytes(await resp.read())
        async with session.get(SAMPLE_DOCX_URL) as resp:
            SAMPLE_SCHEDULE.write_bytes(await resp.read())


if __name__ == "__main__":
    asyncio.run(download_samples())