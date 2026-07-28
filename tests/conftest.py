"""Общие фикстуры тестов."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import MemoryConfig, TaskProfile
from jarvis.core.llm import LLMService, NullProvider, ProfileRegistry
from jarvis.core.memory import build_memory
from jarvis.core.tools import ToolRegistry
from jarvis.core.tts import NullTTS


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
