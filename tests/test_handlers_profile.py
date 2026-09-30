"""Тесты переноса «Моя посещаемость» в «Профиль» (Части 1 и 2).

Проверяется раскладка клавиатур, вход из профиля, период (текущий/прошлый
месяц) и кнопки возврата. Bot подменяется заглушкой из
``tests.test_handlers_dispatch``.
"""

from datetime import date
from pathlib import Path

import pytest

from bot.attendance import attestation_service as atts
from bot.attendance import keyboards as kb
from bot.attendance import service
from bot.db import get_connection, transaction
from bot.keyboards import reply as rk
from bot.main import build_dispatcher
from bot.migrations import apply_migrations
from tests.test_handlers_dispatch import FakeBot

GROUP = "25КАД"
STAROSTA = 3001
STUDENT = 3002

SUBJECTS = {1: "История", 2: "Литература", 3: "Физика"}


@pytest.fixture()
def conn(tmp_path: Path):
    """БД с группой, старостой и студентом."""
    c = get_connection(tmp_path / "profile.db")
    apply_migrations(c)
    with transaction(c):
        for para, subject in SUBJECTS.items():
            c.execute(
                "INSERT INTO schedule_cache (group_name, day_of_week,"
                " para_number, subject, teacher, room, week_type, updated_at)"
                " VALUES (?, 1, ?, ?, 'Т.Т.', '', '', 'x')",
                (GROUP, para, subject),
            )
        code = service.create_group(c, GROUP, STAROSTA, "Абрамчик С.Г.")
        service.join_group(c, STUDENT, code, "Иванов И.И.")
    yield c
    c.close()


@pytest.fixture(scope="module")
def shared_dp():
    """Единственный диспетчер на модуль (роутеры — синглтоны)."""
    seed = get_connection(":memory:")
    apply_migrations(seed)
    return build_dispatcher(seed)


@pytest.fixture()
def dp(shared_dp, conn):
    """Диспетчер со свежим соединением и очищенной FSM."""
    shared_dp.workflow_data["conn"] = conn
    shared_dp.fsm.storage.storage.clear()
    return shared_dp


def _update(text: str, tg_id: int = STUDENT):
    """Апдейт с текстовым сообщением."""
    from aiogram.types import Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=1, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text=text,
    )
    return Update(update_id=1, message=message)


def _callback(data: str, tg_id: int = STUDENT):
    """Апдейт с нажатием inline-кнопки."""
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=tg_id, is_bot=False, first_name="Тест Студент")
    message = Message(
        message_id=2, date=date.today(),
        chat=Chat(id=tg_id, type="private"), from_user=user, text="",
    )
    query = CallbackQuery(id="1", from_user=user, chat_instance="ci",
                          data=data, message=message)
    return Update(update_id=2, callback_query=query)


def _texts(bot: FakeBot) -> list[str]:
    """Тексты отправленных сообщений."""
    return [m["text"] for m in bot.sent if m["text"]]


def _buttons(bot: FakeBot) -> list[str]:
    """Подписи кнопок последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.text for row in markup.inline_keyboard for b in row]
    return []


def _button_data(bot: FakeBot) -> list[str]:
    """Callback-данные последней inline-клавиатуры."""
    for message in reversed(bot.sent):
        markup = message["reply_markup"]
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [b.callback_data for row in markup.inline_keyboard
                    for b in row]
    return []
# --- клавиатуры (Часть 1) ---

def test_profile_kb_has_my_attendance() -> None:
    """В «Профиле» появилась «📊 Моя посещаемость»."""
    labels = [b.text for row in kb.profile_inline_kb().inline_keyboard
              for b in row]
    assert "📊 Моя посещаемость" in labels


def test_profile_kb_layout() -> None:
    """Раскладка профиля — как в ТЗ."""
    labels = [b.text for row in kb.profile_inline_kb().inline_keyboard
              for b in row]
    assert labels == [
        "✏️ Изменить данные",
        "📊 Моя посещаемость",
        "📆 Интеграция с календарём",
        "🐛 Сообщить о проблеме",
        "🏠 Меню",
    ]


def test_profile_kb_attendance_callback() -> None:
    """Кнопка ведёт на callback profile:my_attendance."""
    data = [b.callback_data for row in kb.profile_inline_kb().inline_keyboard
            for b in row]
    assert "profile:my_attendance" in data


def test_student_kb_has_no_my_attendance() -> None:
    """«Моя посещаемость» убрана из «Моей группы» (студент)."""
    labels = [b.text for row in kb.my_group_student_kb().inline_keyboard
              for b in row]
    assert "📊 Моя посещаемость" not in labels
    assert labels == ["✏️ Отметиться на паре", "📋 Список группы", "🏠 Меню"]


def test_starosta_kb_has_no_my_attendance() -> None:
    """У старосты в «Моей группе» тоже нет личной посещаемости."""
    labels = [b.text for row in kb.my_group_starosta_kb().inline_keyboard
              for b in row]
    assert "📊 Моя посещаемость" not in labels


def test_starosta_kb_layout() -> None:
    """Раскладка старосты — как в ТЗ (с кнопкой голосования)."""
    labels = [b.text for row in kb.my_group_starosta_kb().inline_keyboard
              for b in row]
    assert labels == [
        "✏️ Отметить вручную",
        "📣 Запустить голосование",
        "📊 Отчёт за неделю",
        "⚙️ Управление группой",
        "📋 Список группы",
        "🏠 Меню",
    ]


# --- кнопки экрана посещаемости (Часть 2) ---

def test_period_kb_current_month() -> None:
    """Текущий месяц: «Обновить», «Прошлый месяц», «Назад», «Главное меню»."""
    labels = [b.text for row in kb.my_attendance_period_kb(0).inline_keyboard
              for b in row]
    assert labels == ["🔄 Обновить", "📅 Прошлый месяц",
                      "🔙 Назад", "🏠 Главное меню"]


def test_period_kb_past_month_hides_prev_button() -> None:
    """На прошлом месяце листать некуда — кнопки «Прошлый месяц» нет."""
    labels = [b.text for row in kb.my_attendance_period_kb(-1).inline_keyboard
              for b in row]
    assert "📅 Прошлый месяц" not in labels
    assert labels == ["🔄 Обновить", "🔙 Назад", "🏠 Главное меню"]


def test_period_kb_callback_data() -> None:
    """Callback-данные кнопок: period с offset, возврат, меню."""
    data = [b.callback_data for row in kb.my_attendance_period_kb(0).inline_keyboard
            for b in row]
    assert data == ["att:per:0", "att:per:-1", "profile:back", "menu:home"]


def test_week_report_kb_layout() -> None:
    """Под отчётом старосты — «Обновить» и «Главное меню»."""
    data = [b.callback_data for row in kb.week_report_kb().inline_keyboard
            for b in row]
    assert data == ["att:week_refresh", "menu:home"]
    labels = [b.text for row in kb.week_report_kb().inline_keyboard for b in row]
    assert labels == ["🔄 Обновить", "🏠 Главное меню"]
# --- сквозные сценарии через диспетчер ---

async def _open_attendance(dp, bot):
    """Открыть экран посещаемости из профиля."""
    await dp.feed_update(bot, _update(rk.BTN_PROFILE))
    await dp.feed_update(bot, _callback(kb.CB_MY_ATTENDANCE))


async def test_profile_shows_attendance_button(dp, conn) -> None:
    """Кнопка «👤 Профиль» показывает экран с «📊 Моя посещаемость»."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET group_name = ? WHERE tg_id = ?",
            (GROUP, STUDENT),
        )
    bot = FakeBot()

    await dp.feed_update(bot, _update(rk.BTN_PROFILE))

    assert "Моя посещаемость" in " | ".join(_buttons(bot))


async def test_profile_attendance_callback_shows_report(dp, conn) -> None:
    """callback profile:my_attendance показывает отчёт с аттестацией."""
    bot = FakeBot()

    await _open_attendance(dp, bot)

    body = " ".join(_texts(bot))
    assert "📊 <b>Моя посещаемость</b>" in body
    assert GROUP in body
    assert "Аттестация по предметам" in body
    assert "Минимум 3 пары" in body


async def test_attendance_report_lists_all_subjects(dp, conn) -> None:
    """В блоке аттестации видны все предметы группы (в том числе 0/3)."""
    bot = FakeBot()

    await _open_attendance(dp, bot)

    body = " ".join(_texts(bot))
    assert "История — 0/3" in body
    assert "Литература — 0/3" in body
    assert "Физика — 0/3" in body


async def test_attendance_report_marks_attested(dp, conn) -> None:
    """Аттестованный предмет помечен ✅, неаттестованный — ❌ или ⚠️.

    Даты — понедельники текущего месяца (7, 14, 21 сентября 2026):
    расписание группы заведено на понедельник, а период текущего месяца
    считается «1 число → сегодня».
    """
    with transaction(conn):
        for day in ("2026-09-07", "2026-09-14", "2026-09-21"):
            conn.execute(
                "INSERT INTO attendance (group_name, date_iso, para, tg_id,"
                " full_name, status, marked_by, marked_at, method, subject)"
                " VALUES (?, ?, 1, ?, 'Иванов И.И.', 'present', ?, 'x',"
                " 'self', 'История')",
                (GROUP, day, STUDENT, STUDENT),
            )
    bot = FakeBot()

    await _open_attendance(dp, bot)

    body = " ".join(_texts(bot))
    assert "✅ История" in body


async def test_prev_month_button_switches_period(dp, conn) -> None:
    """«📅 Прошлый месяц» пересчитывает на прошлый период."""
    bot = FakeBot()
    await _open_attendance(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _callback("att:per:-1"))

    body = " ".join(_texts(bot))
    prev_month = atts.period_for_month(offset_months=-1)[0]
    from bot.attendance import attendance_texts as atext

    assert atext.month_title(prev_month) in body, "заголовок прошлого месяца"


async def test_current_and_prev_month_titles_differ(dp, conn) -> None:
    """Заголовки текущего и прошлого месяца различаются."""
    bot = FakeBot()
    await _open_attendance(dp, bot)
    current_body = " ".join(_texts(bot))
    bot.sent.clear()

    await dp.feed_update(bot, _callback("att:per:-1"))

    prev_body = " ".join(_texts(bot))
    assert current_body != prev_body


async def test_back_button_returns_to_profile(dp, conn) -> None:
    """«🔙 Назад» открывает «Профиль» с его клавиатурой."""
    bot = FakeBot()
    await _open_attendance(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _callback(kb.CB_PROFILE_BACK))

    texts = _texts(bot)
    assert any("👤 <b>Профиль</b>" in t for t in texts)
    assert "Моя посещаемость" in " | ".join(_buttons(bot))


async def test_menu_button_from_attendance(dp, conn) -> None:
    """«🏠 Главное меню» возвращает reply-клавиатуру главного меню."""
    bot = FakeBot()
    await _open_attendance(dp, bot)
    bot.sent.clear()

    await dp.feed_update(bot, _callback("menu:home"))

    markup = next(m["reply_markup"] for m in bot.sent
                  if m["reply_markup"] is not None
                  and getattr(m["reply_markup"], "keyboard", None))
    assert [b.text for b in markup.keyboard[0]] == [
        rk.BTN_SCHEDULE, rk.BTN_DEADLINES,
    ]


async def test_my_attendance_command_still_works(dp, conn) -> None:
    """/my_attendance остаётся алиасом того же экрана."""
    bot = FakeBot()

    await dp.feed_update(bot, _update("/my_attendance"))

    body = " ".join(_texts(bot))
    assert "📊 <b>Моя посещаемость</b>" in body
    assert "Аттестация по предметам" in body


async def test_student_kb_after_move_has_no_attendance(dp, conn) -> None:
    """«📊 Моя группа» у студента больше не показывает посещаемость."""
    bot = FakeBot()

    await dp.feed_update(bot, _update(kb.BTN_MY_GROUP))

    labels = " | ".join(_buttons(bot))
    assert "Отметиться на паре" in labels
    assert "Моя посещаемость" not in labels


async def test_attendance_without_group_prompts(dp, conn) -> None:
    """Без группы экран просит ввести код от старосты."""
    bot = FakeBot()

    # 9999 в группу не вступал — студента в БД нет.
    await dp.feed_update(bot, _callback(kb.CB_MY_ATTENDANCE, 9999))

    body = " ".join(_texts(bot))
    assert "код от старосты" in body or "не в группе" in body


async def test_legacy_callback_from_my_group(dp, conn) -> None:
    """Старый callback ``grp:my_att`` из отправленных ранее сообщений работает."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback("grp:my_att"))

    body = " ".join(_texts(bot))
    assert "📊 <b>Моя посещаемость</b>" in body


async def test_legacy_att_my_callback(dp, conn) -> None:
    """Callback ``att:my`` (сводка этапа 2) тоже продолжает работать."""
    bot = FakeBot()

    await dp.feed_update(bot, _callback("att:my"))

    body = " ".join(_texts(bot))
    assert "📊 <b>Моя посещаемость</b>" in body