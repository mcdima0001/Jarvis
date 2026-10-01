"""Общие фикстуры тестов."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import MemoryConfig, TaskProfile
from jarvis.core.llm import LLMService, NullProvider, ProfileRegistry
from jarvis.core.memory import build_memory
from jarvis.core.tools import ToolRegistry
from jarvis.core.tts import NullTTS

# Отдельный прогон pytest внутри теста: так проверяются хуки этого файла.
pytest_plugins = ("pytester",)


@pytest.fixture
def events() -> LocalEventBus:
    """Чистая шина событий."""
    return LocalEventBus()


@pytest.fixture
def registry(events: LocalEventBus) -> ToolRegistry:
    """Пустой реестр инструментов с коротким таймаутом."""
    return ToolRegistry(events=events, default_timeout=1.0)


@pytest.fixture
def memory(tmp_path: Path):
    """Файловая память во временном каталоге."""
    return build_memory(
        MemoryConfig(
            dir=tmp_path / "memory",
            documents=("profile", "preferences"),
            journals=("today",),
            context_budget_tokens=500,
        )
    )


@pytest.fixture
def llm() -> LLMService:
    """Сервис LLM на заглушке — сеть в тестах не нужна."""
    profile = TaskProfile(task="dialog", provider="null", model="stub")
    return LLMService(
        providers={"null": NullProvider()},
        profiles=ProfileRegistry(
            {"dialog": profile, "intent": profile},
            default_task="dialog",
        ),
    )


@pytest.fixture
def tts() -> NullTTS:
    """Синтез-заглушка."""
    return NullTTS()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Модуль, пропущенный целиком, — ошибка сбора, а не буква «s».

    `importorskip` наверху файла убирает из прогона все его тесты разом, а в
    отчёте от них остаётся одна буква: без Pillow так выпадали 98 тестов «где
    снято», без websockets — семь тестов потокового распознавания, и прогон
    оставался зелёным (аудит 01.10.2026). Всё, что нужно тестам, входит в набор
    `dev`, поэтому пропущенный модуль значит неполную установку или неполный
    `dev` — и узнать об этом надо так же громко, как о недостающем numpy.
    Тяжёлое и необязательное (faster-whisper) пропускается внутри теста, поштучно.
    """
    report = yield
    if report.skipped and isinstance(collector, pytest.Module):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
        reason = str(reason).removeprefix("Skipped: ")
        report.outcome = "failed"
        report.longrepr = (
            f"модуль пропущен целиком: {reason}\n"
            'Всё, что нужно тестам, входит в набор dev: pip install -e ".[dev]". '
            "Необязательное и тяжёлое пропускают внутри теста, а не наверху файла."
        )
    return report
