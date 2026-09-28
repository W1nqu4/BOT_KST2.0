"""Тесты слоя данных (bot.services.cache_service): скачивание и кэш.

Реальная сеть не используется: подменные сессии отдают заранее
подготовленные байты. Образцы источников берутся из tests/conftest.py
(если файла нет — тест скипается).
"""

import json
from pathlib import Path

import pytest

from bot.db import get_connection, transaction
from bot.migrations import apply_migrations
from bot.services import cache_service as cs


@pytest.fixture()
def conn(tmp_path: Path):
    """Соединение к временной БД с применёнными миграциями."""
    c = get_connection(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()


class FakeResponse:
    """Подменный ответ aiohttp: статус и байты."""

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self.body = body
        self.status = status

    async def read(self) -> bytes:
        return self.body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None


class FakeSession:
    """Подменная aiohttp-сессия: отдаёт ответ по URL и считает вызовы."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, timeout=None):
        """Вернуть ответ; список в маршруте — очередь (для проверки retry).

        URL приходит percent-кодированным (источники содержат кириллицу),
        поэтому сравниваем по раскодированному виду.
        """
        from urllib.parse import unquote

        self.calls.append(url)
        decoded = unquote(url)
        value = None
        for key, val in self.routes.items():
            if key in url or key in decoded:
                value = val
                break
        if value is None:
            return FakeResponse(b"", 404)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list):
            return value.pop(0)
        return value


HTML_WITH_DOCX = (
    '<html><body><a href="/wp-content/uploads/2026/09/'
    'Расписание-занятий-1-семестр-2026-2027-уч.-год.docx">скачать</a>'
    "</body></html>"
)

PAGE_URL = "https://24kst.ru/студенту/расписание-занятий/"
SUBS_URL = "https://24kst.ru/студенту/лист-замен/"


# --- extract_docx_url ---

def test_extract_docx_url_finds_cyrillic_link() -> None:
    assert cs.extract_docx_url(HTML_WITH_DOCX) == (
        "/wp-content/uploads/2026/09/"
        "Расписание-занятий-1-семестр-2026-2027-уч.-год.docx"
    )


def test_extract_docx_url_none_when_absent() -> None:
    assert cs.extract_docx_url("<a href='page.html'>x</a>") is None


def test_extract_docx_url_picks_docx_only() -> None:
    """PDF-ссылки игнорируются — нужен именно .docx."""
    html = "<a href='/files/old.pdf'>pdf</a><a href='/files/new.docx'>docx</a>"
# --- save_schedule / save_substitutions ---

def test_save_schedule_replaces_cache(conn) -> None:
    """Полная замена: второй вызов не оставляет старых строк."""
    first = [
        {"group_name": "26КАД", "day_of_week": 1, "para_number": 1,
         "subject": "Математика", "teacher": "Т1", "room": "307А",
         "week_type": ""},
        {"group_name": "26КАД", "day_of_week": 2, "para_number": 2,
         "subject": "Физика", "teacher": "Т2", "room": "308А",
         "week_type": "Чет"},
    ]
    assert cs.save_schedule(conn, first) == 2
    assert conn.execute("SELECT COUNT(*) FROM schedule_cache").fetchone()[0] == 2

    cs.save_schedule(conn, first[:1])
    rows = conn.execute("SELECT * FROM schedule_cache").fetchall()
    assert len(rows) == 1
    assert rows[0]["subject"] == "Математика"
    assert rows[0]["updated_at"]


def test_save_substitutions_keeps_self_study_flag(conn) -> None:
    """Флаг «самостоятельная работа» не теряется при записи (миграция 2)."""
    rows = [
        {"group": "26КАД", "date_iso": "2026-09-22", "para": 1,
         "old_subject": "ОД.07 Математика", "new_subject": "ОД.07 Математика",
         "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
         "is_cancelled": False, "is_self_study": True},
        {"group": "26МЭГ", "date_iso": "2026-09-22", "para": 1,
         "old_subject": "ОД.07 Математика", "new_subject": "",
         "teacher": "", "room": "",
         "is_cancelled": True, "is_self_study": False},
    ]
    assert cs.save_substitutions(conn, rows) == 2
    stored = conn.execute(
        "SELECT * FROM substitutions_cache ORDER BY group_name"
    ).fetchall()
    assert len(stored) == 2
    self_study = next(r for r in stored if r["group_name"] == "26КАД")
    cancelled = next(r for r in stored if r["group_name"] == "26МЭГ")
    assert self_study["is_self_study"] == 1
    assert self_study["is_cancelled"] == 0
    assert cancelled["is_cancelled"] == 1
    assert cancelled["is_self_study"] == 0


def test_save_substitutions_replaces_cache(conn) -> None:
    base = {"group": "26КАД", "date_iso": "2026-09-22", "para": 1,
            "old_subject": "A", "new_subject": "B", "teacher": "",
            "room": "", "is_cancelled": False, "is_self_study": False}
    cs.save_substitutions(conn, [base, dict(base, para=2)])
    assert conn.execute("SELECT COUNT(*) FROM substitutions_cache").fetchone()[0] == 2
    cs.save_substitutions(conn, [base])
    assert conn.execute("SELECT COUNT(*) FROM substitutions_cache").fetchone()[0] == 1


# --- meta и возраст кэша ---

def test_meta_roundtrip(conn) -> None:
    assert cs.get_meta(conn, "last_successful_fetch") is None
    with transaction(conn):
        cs._set_meta(conn, "last_successful_fetch", "2026-09-26T10:00:00+00:00")
    assert cs.get_meta(conn, "last_successful_fetch") == "2026-09-26T10:00:00+00:00"
    with transaction(conn):
        cs._set_meta(conn, "last_successful_fetch", "2026-09-27T10:00:00+00:00")
    assert cs.get_meta(conn, "last_successful_fetch") == "2026-09-27T10:00:00+00:00"


def test_cache_age_none_without_meta(conn) -> None:
    assert cs.cache_age_seconds(conn, "last_successful_fetch") is None


def test_cache_age_positive_and_broken_value(conn) -> None:
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    with transaction(conn):
        cs._set_meta(conn, "k", past.isoformat())
        cs._set_meta(conn, "bad", "не-дата")
    age = cs.cache_age_seconds(conn, "k")
    assert age is not None and age > 60 * 60
    assert cs.cache_age_seconds(conn, "bad") is None


# --- _fetch: таймауты, retry, HTTP 4xx без повтора ---

async def test_fetch_returns_body_and_status(tmp_path: Path) -> None:
    session = FakeSession({PAGE_URL: FakeResponse(b"hello", 200)})
    body, status = await cs._fetch(session, PAGE_URL)
    assert body == b"hello"
    assert status == 200
    assert len(session.calls) == 1


async def test_fetch_retries_once_on_network_error(monkeypatch) -> None:
    """Сетевая ошибка → одна повторная попытка, потом успех."""
    monkeypatch.setattr(cs, "DOWNLOAD_RETRY_DELAY_SECONDS", 0)
    session = FakeSession({
        PAGE_URL: [OSError("connection reset"), FakeResponse(b"ok", 200)],
    })
    body, status = await cs._fetch(session, PAGE_URL)
    assert body == b"ok"
    assert status == 200
    assert len(session.calls) == 2


async def test_fetch_two_failures_raise(monkeypatch) -> None:
    """Две сетевые ошибки подряд → DownloadError."""
    monkeypatch.setattr(cs, "DOWNLOAD_RETRY_DELAY_SECONDS", 0)
    session = FakeSession({PAGE_URL: [OSError("reset"), OSError("reset")]})
    with pytest.raises(cs.DownloadError):
        await cs._fetch(session, PAGE_URL)
    assert len(session.calls) == 2


async def test_fetch_4xx_no_retry() -> None:
    """HTTP 4xx — без повтора (повторять нечего)."""
    session = FakeSession({PAGE_URL: FakeResponse(b"", 404)})
    with pytest.raises(cs.DownloadError) as exc:
        await cs._fetch(session, PAGE_URL)
    assert "404" in str(exc.value)
    assert len(session.calls) == 1


async def test_fetch_5xx_retries(monkeypatch) -> None:
    """5xx считается временной ошибкой: одна повторная попытка."""
    monkeypatch.setattr(cs, "DOWNLOAD_RETRY_DELAY_SECONDS", 0)
    session = FakeSession({
        PAGE_URL: [FakeResponse(b"", 503), FakeResponse(b"ok", 200)],
    })
    body, _ = await cs._fetch(session, PAGE_URL)
    assert body == b"ok"
# --- refresh_schedule: правила кэша 2-4 ---

DOCX_URL_FRAGMENT = "Расписание-занятий"

SCHEDULE_HTML = (
    '<html><body><a href="https://24kst.ru/wp-content/uploads/2026/09/'
    'Расписание-занятий-1-семестр-2026-2027-уч.-год.docx">ссылка</a>'
    "</body></html>"
)

LESSON = {"group_name": "26КАД", "day_of_week": 1, "para_number": 1,
          "subject": "Математика", "teacher": "Т", "room": "307А",
          "week_type": ""}


def _seed_schedule_cache(conn) -> None:
    """Положить в кэш «прошлую» версию расписания."""
    cs.save_schedule(conn, [dict(LESSON, subject="СТАРЫЙ ПРЕДМЕТ")])


async def test_refresh_schedule_success_writes_cache_and_meta(
    conn, tmp_path: Path, sample_schedule_path: Path
) -> None:
    """Успешное обновление: кэш заменён, мета обновлена, файлы сохранены."""
    docx_bytes = sample_schedule_path.read_bytes()
    session = FakeSession({
        PAGE_URL: FakeResponse(SCHEDULE_HTML.encode("utf-8"), 200),
        DOCX_URL_FRAGMENT: FakeResponse(docx_bytes, 200),
    })
    count = await cs.refresh_schedule(conn, session=session, directory=tmp_path)

    assert count > 1000, "расписание из образца должно быть большим"
    assert conn.execute(
        "SELECT COUNT(*) FROM schedule_cache"
    ).fetchone()[0] == count
    assert cs.get_meta(conn, cs.META_LAST_SCHEDULE) is not None

    # Файлы кэша под фиксированными именами + мета для отладки (правило 5).
    assert (tmp_path / cs.SCHEDULE_FILE).exists()
    meta_file = tmp_path / f"{cs.SCHEDULE_FILE}.meta.json"
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["status"] == 200
    assert meta["size"] == len(docx_bytes)
    assert DOCX_URL_FRAGMENT in meta["url"]


async def test_refresh_schedule_download_failure_keeps_cache(
    conn, tmp_path: Path, caplog
) -> None:
    """Правило 2: падение скачивания НЕ затирает кэш, пишется WARNING с возрастом."""
    import logging as _logging

    _seed_schedule_cache(conn)
    session = FakeSession({PAGE_URL: OSError("нет сети")})

    with caplog.at_level(_logging.WARNING, logger="bot.services.cache_service"):
        result = await cs.refresh_schedule(conn, session=session, directory=tmp_path)

    assert result == -1
    rows = conn.execute("SELECT subject FROM schedule_cache").fetchall()
    assert len(rows) == 1 and rows[0]["subject"] == "СТАРЫЙ ПРЕДМЕТ"
    assert cs.get_meta(conn, cs.META_LAST_SCHEDULE) is None, "мета только при успехе"
    messages = [r.getMessage() for r in caplog.records]
    assert any("не обновлён" in m and "возраст кэша" in m for m in messages)


async def test_refresh_schedule_no_docx_link_keeps_cache(
    conn, tmp_path: Path, caplog
) -> None:
    """Ссылка на .docx не найдена → кэш не трогаем."""
    import logging as _logging

    _seed_schedule_cache(conn)
    session = FakeSession({
        PAGE_URL: FakeResponse("<html>нет ссылки</html>".encode("utf-8"), 200),
    })

    with caplog.at_level(_logging.WARNING, logger="bot.services.cache_service"):
        result = await cs.refresh_schedule(conn, session=session, directory=tmp_path)

    assert result == -1
    assert conn.execute(
        "SELECT subject FROM schedule_cache"
    ).fetchone()["subject"] == "СТАРЫЙ ПРЕДМЕТ"
    assert any(".docx" in r.getMessage() for r in caplog.records)
async def test_refresh_schedule_empty_parse_keeps_cache(
    conn, tmp_path: Path, caplog, monkeypatch
) -> None:
    """Правило 3: parse вернул [] → это ОШИБКА, кэш НЕ перезаписывается.

    Пустой разбор обычно значит изменение формата источника, а не «занятий
    нет»: затирание кэша в этом случае оставило бы студентов без расписания.
    """
    import logging as _logging

    _seed_schedule_cache(conn)
    # Пустой разбор имитируем (файл-образец есть, но парсер «сломался»).
    monkeypatch.setattr(cs, "parse_docx", lambda path: [])

    session = FakeSession({
        PAGE_URL: FakeResponse(SCHEDULE_HTML.encode("utf-8"), 200),
        DOCX_URL_FRAGMENT: FakeResponse(b"PK\x03\x04 fake", 200),
    })
    with caplog.at_level(_logging.WARNING, logger="bot.services.cache_service"):
        result = await cs.refresh_schedule(conn, session=session, directory=tmp_path)

    assert result == -1
    assert conn.execute(
        "SELECT subject FROM schedule_cache"
    ).fetchone()["subject"] == "СТАРЫЙ ПРЕДМЕТ", "кэш не должен быть затёрт"
    assert cs.get_meta(conn, cs.META_LAST_SCHEDULE) is None
    assert any("пустой список" in r.getMessage() for r in caplog.records)


async def test_refresh_schedule_empty_body_keeps_cache(
    conn, tmp_path: Path
) -> None:
    """Пустой файл от сервера → кэш не трогаем."""
    _seed_schedule_cache(conn)
    session = FakeSession({
        PAGE_URL: FakeResponse(SCHEDULE_HTML.encode("utf-8"), 200),
        DOCX_URL_FRAGMENT: FakeResponse(b"", 200),
    })
    result = await cs.refresh_schedule(conn, session=session, directory=tmp_path)
    assert result == -1
    assert conn.execute(
        "SELECT subject FROM schedule_cache"
    ).fetchone()["subject"] == "СТАРЫЙ ПРЕДМЕТ"


async def test_refresh_schedule_4xx_keeps_cache(conn, tmp_path: Path) -> None:
    """HTTP 4xx на странице → кэш не трогаем."""
    _seed_schedule_cache(conn)
    session = FakeSession({PAGE_URL: FakeResponse(b"", 404)})
    result = await cs.refresh_schedule(conn, session=session, directory=tmp_path)
    assert result == -1
    assert conn.execute("SELECT COUNT(*) FROM schedule_cache").fetchone()[0] == 1


async def test_refresh_schedule_saves_meta_file_for_debug(
    conn, tmp_path: Path, sample_schedule_path: Path
) -> None:
    """meta.json содержит timestamp и HTTP-статус (правило 5)."""
    session = FakeSession({
        PAGE_URL: FakeResponse(SCHEDULE_HTML.encode("utf-8"), 200),
        DOCX_URL_FRAGMENT: FakeResponse(sample_schedule_path.read_bytes(), 200),
    })
    await cs.refresh_schedule(conn, session=session, directory=tmp_path)

    meta = json.loads(
        (tmp_path / f"{cs.SCHEDULE_FILE}.meta.json").read_text(encoding="utf-8")
    )
    assert set(meta) >= {"url", "status", "size", "fetched_at", "fetched_at_local"}
    assert meta["status"] == 200
# --- refresh_substitutions ---

def _seed_substitutions_cache(conn) -> None:
    """Положить в кэш «прошлую» версию листа замен."""
    cs.save_substitutions(conn, [{
        "group": "26МЭГ", "date_iso": "2026-09-21", "para": 9,
        "old_subject": "СТАРАЯ ОТМЕНА", "new_subject": "", "teacher": "",
        "room": "", "is_cancelled": True, "is_self_study": False,
    }])


async def test_refresh_substitutions_success(
    conn, tmp_path: Path, sample_substitutions_path: Path
) -> None:
    """Успех: кэш заменён, сохранены файл и мета, данные записаны."""
    html_bytes = sample_substitutions_path.read_bytes()
    session = FakeSession({SUBS_URL: FakeResponse(html_bytes, 200)})

    count = await cs.refresh_substitutions(conn, session=session,
                                           directory=tmp_path)

    assert count > 0
    assert (tmp_path / cs.SUBSTITUTIONS_FILE).exists()
    assert cs.get_meta(conn, cs.META_LAST_SUBSTITUTIONS) is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache"
    ).fetchone()[0] == count
    # На реальной странице есть отмены (тире) — флаг должен быть записан.
    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache WHERE is_cancelled = 1"
    ).fetchone()[0] >= 1


async def test_refresh_substitutions_writes_self_study_from_fixture(
    conn, tmp_path: Path
) -> None:
    """Флаг «самостоятельная работа» доходит до БД (синтетическая фикстура).

    В живом листе замен этого случая сейчас нет (проверено: self_study = 0),
    поэтому берём фикстуру, где строка есть, — иначе миграция 2 не проверяется.
    """
    fixture = (
        Path(__file__).resolve().parent / "fixtures" / "sample_substitutions.html"
    )
    if not fixture.exists():
        pytest.skip("нет синтетической фикстуры листа замен")

    session = FakeSession({SUBS_URL: FakeResponse(fixture.read_bytes(), 200)})
    count = await cs.refresh_substitutions(conn, session=session,
                                           directory=tmp_path)

    assert count > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache WHERE is_self_study = 1"
    ).fetchone()[0] >= 1


# --- история замен (шаг 3): пишем только для групп с пользователями ---

def _add_user(conn, tg_id: int, group: str) -> None:
    """Вставить зарегистрированного пользователя."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO users (tg_id, group_name, created_at)"
            " VALUES (?, ?, 'x')",
            (tg_id, group),
        )


def _sub_row(group: str, date_iso: str = "2026-09-28",
             para: int = 2) -> dict:
    """Строка листа замен в формате парсера."""
    return {
        "group": group, "date_iso": date_iso, "para": para,
        "old_subject": "ОД.03 История", "new_subject": "ОД.07 Математика",
        "teacher": "Кудрявцева Полина Алексеевна", "room": "307А",
        "is_cancelled": False, "is_self_study": False,
    }


def test_history_written_only_for_known_groups(conn) -> None:
    """История пишется только для групп из users; чужие игнорируются."""
    from bot import db

    _add_user(conn, 111, "26КАД")
    rows = [_sub_row("26КАД"), _sub_row("26МЭГ"), _sub_row("25КАД")]

    saved = cs.save_history_for_known_groups(conn, rows)

    assert saved == {"26КАД": 1}
    assert db.count_substitution_history(conn) == 1
    row = conn.execute("SELECT * FROM substitution_history").fetchone()
    assert row["group_name"] == "26КАД"


def test_history_not_written_without_users(conn) -> None:
    """Нет зарегистрированных пользователей → история не пишется вообще."""
    from bot import db

    saved = cs.save_history_for_known_groups(conn, [_sub_row("26КАД")])

    assert saved == {}
    assert db.count_substitution_history(conn) == 0


def test_history_normalizes_group_names(conn) -> None:
    """«26 кад» из листа и «26КАД» из users — одна группа."""
    from bot import db

    _add_user(conn, 111, "26КАД")
    saved = cs.save_history_for_known_groups(conn, [_sub_row("26 кад")])

    assert saved == {"26КАД": 1}
    assert db.count_substitution_history(conn) == 1


def test_history_groups_by_date(conn) -> None:
    """Замены разных дат пишутся отдельными записями."""
    from bot import db

    _add_user(conn, 111, "26КАД")
    rows = [
        _sub_row("26КАД", "2026-09-28", para=2),
        _sub_row("26КАД", "2026-09-28", para=3),
        _sub_row("26КАД", "2026-09-29", para=2),
    ]

    saved = cs.save_history_for_known_groups(conn, rows)

    assert saved == {"26КАД": 3}
    assert db.count_substitution_history(conn) == 3
    assert db.earliest_substitution_history_date(conn) == "2026-09-28"


async def test_refresh_substitutions_writes_history_for_users(
    conn, tmp_path: Path, sample_substitutions_path: Path
) -> None:
    """refresh_substitutions пишет историю для групп с пользователями.

    Группу берём из реального листа замен: сначала смотрим, какие группы там
    есть, затем регистрируем пользователя одной из них.
    """
    from bot import db

    html_bytes = sample_substitutions_path.read_bytes()
    session = FakeSession({SUBS_URL: FakeResponse(html_bytes, 200)})
    count = await cs.refresh_substitutions(conn, session=session,
                                           directory=tmp_path)
    assert count > 0

    # До регистрации пользователей истории нет.
    assert db.count_substitution_history(conn) == 0

    groups = [r[0] for r in conn.execute(
        "SELECT DISTINCT group_name FROM substitutions_cache"
        " ORDER BY group_name LIMIT 1"
    )]
    target = groups[0]
    _add_user(conn, 999, target)

    await cs.refresh_substitutions(conn, session=FakeSession(
        {SUBS_URL: FakeResponse(html_bytes, 200)}), directory=tmp_path)

    assert db.count_substitution_history(conn) > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM substitution_history WHERE group_name = ?",
        (target,),
    ).fetchone()[0] > 0


async def test_refresh_substitutions_history_ignores_unregistered_groups(
    conn, tmp_path: Path, sample_substitutions_path: Path
) -> None:
    """Группы листа замен без пользователей в историю не попадают."""
    from bot import db

    html_bytes = sample_substitutions_path.read_bytes()
    session = FakeSession({SUBS_URL: FakeResponse(html_bytes, 200)})
    await cs.refresh_substitutions(conn, session=session, directory=tmp_path)

    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache"
    ).fetchone()[0] > 0, "лист замен должен быть в кэше"
    assert db.count_substitution_history(conn) == 0, "история не должна писаться"


async def test_refresh_substitutions_history_does_not_replace_cache(
    conn, tmp_path: Path, sample_substitutions_path: Path
) -> None:
    """История — ДОПОЛНЕНИЕ: текущий лист замен по-прежнему перезаписывается."""
    from bot import db

    html_bytes = sample_substitutions_path.read_bytes()
    await cs.refresh_substitutions(
        conn, session=FakeSession({SUBS_URL: FakeResponse(html_bytes, 200)}),
        directory=tmp_path,
    )
    first_cache = conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache"
    ).fetchone()[0]

    # Регистрируем пользователя и обновляем лист ещё раз.
    groups = [r[0] for r in conn.execute(
        "SELECT DISTINCT group_name FROM substitutions_cache LIMIT 1"
    )]
    _add_user(conn, 999, groups[0])

    await cs.refresh_substitutions(
        conn, session=FakeSession({SUBS_URL: FakeResponse(html_bytes, 200)}),
        directory=tmp_path,
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache"
    ).fetchone()[0] == first_cache, "кэш замен не должен накапливаться"
    assert db.count_substitution_history(conn) > 0, "история должна накопиться"


async def test_refresh_substitutions_failure_keeps_cache(
    conn, tmp_path: Path, caplog
) -> None:
    """Правило 2: падение скачивания → старые замены остаются."""
    import logging as _logging

    _seed_substitutions_cache(conn)
    session = FakeSession({SUBS_URL: OSError("нет сети")})

    with caplog.at_level(_logging.WARNING, logger="bot.services.cache_service"):
        result = await cs.refresh_substitutions(conn, session=session,
                                                directory=tmp_path)

    assert result == -1
    assert conn.execute(
        "SELECT old_subject FROM substitutions_cache"
    ).fetchone()["old_subject"] == "СТАРАЯ ОТМЕНА"
    assert cs.get_meta(conn, cs.META_LAST_SUBSTITUTIONS) is None
    assert any("возраст кэша" in r.getMessage() for r in caplog.records)


async def test_refresh_substitutions_empty_parse_keeps_cache(
    conn, tmp_path: Path, monkeypatch
) -> None:
    """Пустой разбор (нет таблицы замен) → старые данные не трогаем."""
    _seed_substitutions_cache(conn)
    monkeypatch.setattr(cs, "parse_html", lambda path: [])

    session = FakeSession({
        SUBS_URL: FakeResponse("<html>нет таблицы</html>".encode("utf-8"), 200),
    })
    result = await cs.refresh_substitutions(conn, session=session,
                                           directory=tmp_path)

    assert result == -1
    assert conn.execute(
        "SELECT old_subject FROM substitutions_cache"
    ).fetchone()["old_subject"] == "СТАРАЯ ОТМЕНА"


async def test_refresh_substitutions_empty_body_keeps_cache(
    conn, tmp_path: Path
) -> None:
    _seed_substitutions_cache(conn)
    session = FakeSession({SUBS_URL: FakeResponse(b"", 200)})
    result = await cs.refresh_substitutions(conn, session=session,
                                           directory=tmp_path)
    assert result == -1
    assert conn.execute(
        "SELECT COUNT(*) FROM substitutions_cache"
    ).fetchone()[0] == 1