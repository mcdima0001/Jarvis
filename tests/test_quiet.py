"""Тихий режим: не мешать игре (просьба владельца 23.09.2026).

Замер, с которого всё началось: процессора ассистент ест 0.6% машины, а памяти —
почти два гигабайта, плюс гигабайт на открытую панель. Значит экономить надо
память, а не такты.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.core import builtin, thrift
from jarvis.core.builtin import CoreTools
from jarvis.core.contracts import ToolResult
from jarvis.core.state import QUIET, Modes
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Panel:
    """Панель управления: то самое окно Edge на гигабайт."""

    def __init__(self) -> None:
        self.closed = 0

    @tool(name="close_panel", routable=False, reversible=True)
    async def close_panel(self) -> ToolResult:
        """Закрыть панель."""
        self.closed += 1
        return ToolResult.success(True)


class Ears:
    """Распознавание, у которого поднята местная модель."""

    def __init__(self) -> None:
        self.released = 0

    async def release_backup(self) -> bool:
        self.released += 1
        return True


@pytest.fixture
def core(monkeypatch) -> tuple[ToolRegistry, CoreTools, Panel, Ears, list[bool]]:
    registry, panel, ears = ToolRegistry(), Panel(), Ears()
    for item in collect_tools(panel, namespace="core"):
        registry.register(item)
    priorities: list[bool] = []
    # Подменяем имя там, где им пользуются: `builtin` импортировал функцию
    # напрямую, и подмена в самом `thrift` до него бы не дошла.
    monkeypatch.setattr(builtin, "set_priority", lambda *, low: priorities.append(low) or True)
    tools = CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        registry=registry,
        skills=None,  # type: ignore[arg-type]
        modes=Modes(),
        stt=ears,
    )
    for item in collect_tools(tools, namespace="core"):
        registry.register(item)
    return registry, tools, panel, ears, priorities


async def test_quiet_gives_the_machine_its_memory_back(core: Any) -> None:
    """Рычаги настоящие: панель закрыта, модель отпущена, приоритет опущен."""
    registry, tools, panel, ears, priorities = core
    result = await registry.invoke("core.quiet")
    assert result.ok
    assert panel.closed == 1, "гигабайт Edge в игре не нужен"
    assert ears.released == 1, "полгигабайта Whisper ждали следующего обрыва"
    assert priorities == [True]


async def test_the_assistant_keeps_listening(core: Any) -> None:
    """Тихий режим — не выключение: уши и команды остаются."""
    registry, tools, *_ = core
    await registry.invoke("core.quiet")
    assert tools._modes.active(QUIET)
    assert not tools._modes.active("deaf"), "глухим он не становится"


async def test_as_usual_returns_the_priority(core: Any) -> None:
    """Оставленный низкий приоритет потом ищут в микрофоне и в сети."""
    registry, _, _, _, priorities = core
    await registry.invoke("core.quiet")
    await registry.invoke("core.as_usual")
    assert priorities == [True, False]


async def test_leaving_other_modes_does_not_touch_the_priority(core: Any) -> None:
    registry, _, _, _, priorities = core
    await registry.invoke("core.be_brief")
    await registry.invoke("core.as_usual")
    assert priorities == [], "приоритет трогает только тихий режим"


async def test_memory_is_measured_and_not_promised() -> None:
    """Правило проекта: механизм, который нельзя измерить, не ставится."""
    assert thrift.own_memory() >= 0.0
