"""Тесты типов фронта Mini App: `tsc --noEmit` как защита от React error #31.

Зачем отдельный тест на TypeScript: в ``webapp/next.config.mjs`` стоит
``typescript.ignoreBuildErrors: true``, поэтому сборка Next.js **не падает** на
ошибках типов — прод-фронт собирается даже с ними. Именно так проскочил баг,
из-за которого экран падал с React error #31 («Objects are not valid as a
React child»): в ``Placeholder`` вместо React-элемента передавался объект
``{children, onClick}``, а его пропс ``action`` объявлен как ``ReactNode``.

Тест ловит это на уровне типов, а не в рантайме браузера: ``tsc`` указывает
файл и строку. Если ``node_modules`` или сам ``tsc`` недоступны (например,
чистый checkout без ``pnpm install``) — тест скипается, а не падает.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBAPP = PROJECT_ROOT / "webapp"
TSC = WEBAPP / "node_modules" / "typescript" / "bin" / "tsc"

# Сколько ждать проверку типов: на холодную tsc разбирает весь проект.
TSC_TIMEOUT_SECONDS = 300


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node в PATH")
@pytest.mark.skipif(not TSC.is_file(), reason="нет webapp/node_modules (сделайте pnpm install)")
def test_frontend_types_are_valid() -> None:
    """``tsc --noEmit`` по webapp/ не находит ошибок типов.

    Проверяется весь фронт, но ценность конкретная: пропсы, объявленные как
    ``ReactNode``, не должны получать объекты. Иначе React падает в рантайме
    с error #31 — и на живом экране вместо сообщения об ошибке появляется
    «This page couldn't load».
    """
    result = subprocess.run(
        ["node", str(TSC), "--noEmit", "-p", "tsconfig.json"],
        cwd=WEBAPP,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TSC_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        "ошибки типов во фронте:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_placeholder_action_is_not_object_literal() -> None:
    """``action`` у ``Placeholder`` не получает объект ``{children, onClick}``.

    Текстовый тест-«сторож»: он не зависит от наличия ``node_modules`` и прямо
    запрещает паттерн, который ломал Mini App. TypeScript этот случай ловит
    (см. тест выше), но здесь ошибка видна даже без установленных зависимостей.
    """
    source = (WEBAPP / "components" / "kst" / "async-state.tsx").read_text(
        encoding="utf-8"
    )

    # Ищем `action={` внутри JSX с объектным литералом — это и есть баг.
    assert "action={onRetry ? { children:" not in source, (
        "Placeholder.action должен быть ReactNode, а не объект "
        "{children, onClick}: React упадёт с error #31"
    )
    assert "action={{ children:" not in source, (
        "Placeholder.action должен быть ReactNode, а не объект "
        "{children, onClick}: React упадёт с error #31"
    )


def test_error_state_message_is_string_typed() -> None:
    """Проп ``message`` у ``ErrorState`` объявлен как ``string``.

    Если однажды он станет ``ReactNode``/``object``, экран ошибки снова начнёт
    падать: ``lib/api.ts`` отдаёт из ``errorMessage`` строку, и это должно
    оставаться согласованным.
    """
    source = (WEBAPP / "components" / "kst" / "async-state.tsx").read_text(
        encoding="utf-8"
    )

    assert "message: string" in source, (
        "ErrorState.message должен быть string: errorMessage() возвращает строку"
    )


def test_error_message_returns_string() -> None:
    """``errorMessage`` в ``lib/api.ts`` возвращает строку, а не объект.

    401 (без initData) превращается в человеческий текст «Откройте приложение
    из Telegram» — именно его показывает ErrorState.
    """
    source = (WEBAPP / "lib" / "api.ts").read_text(encoding="utf-8")

    assert "export function errorMessage(error: unknown): string" in source
    assert "Откройте приложение из Telegram" in source