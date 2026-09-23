"""Протоколы: одна фраза — набор действий, шаги фразами, без модели."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.contracts import ToolResult
from jarvis.core.tools import ToolRegistry, collect_tools, tool

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "protocols" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_protocols_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


protocols = _load()


class Studio:
    """Поддельные инструменты: что вызвали — записываем."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @tool(phrases=["открой {program}"], reversible=True)
    async def launch(self, program: str) -> ToolResult:
        """Открыть программу.

        :param program: что открыть.
        """
        self.calls.append(("launch", {"program": program}))
        return ToolResult.success(program)

    @tool(phrases=["заблокируй компьютер"], reversible=False)
    async def lock(self) -> ToolResult:
        """Заблокировать."""
        self.calls.append(("lock", {}))
        return ToolResult.failure("нет прав")

    @tool(reversible=True)
    async def volume(self, level: int) -> ToolResult:
        """Громкость.

        :param level: уровень.
        """
        self.calls.append(("volume", {"level": level}))
        return ToolResult.success(level)


async def _skill(settings: dict[str, Any]) -> tuple[Any, Studio, ToolRegistry]:
    registry = ToolRegistry()
    studio = Studio()
    for item in collect_tools(studio, namespace="studio"):
        registry.register(item)
    skill = protocols.ProtocolsSkill()
    skill._context = SimpleNamespace(
        setting=lambda key, default=None: settings.get(key, default),
        logger=logging.getLogger("test.protocols"),
        tools=registry,
    )
    await skill.on_setup()
    for item in collect_tools(skill, namespace="protocols"):
        registry.register(item)
    return skill, studio, registry


def test_broken_steps_are_dropped_not_fatal() -> None:
    found = protocols.parse_protocols({
        "Работа": ["открой телеграм", "", 42, {"tool": "studio.volume", "args": {"level": 30}}, {"args": {}}],
        "пусто": [],
        "строка": "открой телеграм",
    })
    assert list(found) == ["работа"]
    assert found["работа"].steps == ["открой телеграм", {"tool": "studio.volume", "args": {"level": 30}}]
    assert not found["работа"].watched, "без «когда» протокол запускают голосом"


def test_a_protocol_can_say_when_it_starts_itself() -> None:
    """Повод — запущенная программа: «игру запустил» и есть команда (23.09.2026)."""
    found = protocols.parse_protocols({
        "игра": {
            "when": {"process": ["javaw.exe", "PrismLauncher.exe"]},
            "steps": ["тихий режим", "схема питания производительность"],
            "after": ["как обычно", "верни схему питания"],
        },
    })
    game = found["игра"]
    assert game.watched and game.processes == ("javaw.exe", "prismlauncher.exe")
    assert game.steps[0] == "тихий режим"
    assert game.after[-1] == "верни схему питания"


def test_a_trigger_without_steps_is_not_a_protocol() -> None:
    """Пустой протокол с поводом — это опечатка, а не «ничего не делать»."""
    assert protocols.parse_protocols({"пусто": {"when": {"process": ["game.exe"]}}}) == {}


async def test_protocol_runs_phrases_and_tools_in_order() -> None:
    skill, studio, registry = await _skill({
        "pause_s": 0,
        "protocols": {"работа": ["открой телеграм", {"tool": "studio.volume", "args": {"level": 30}}]},
    })
    result = await registry.invoke("protocols.run", {"name": "работа"})
    assert result.ok and result.speech_for("ru") == "Протокол «работа» выполнен."
    assert studio.calls == [("launch", {"program": "телеграм"}), ("volume", {"level": 30})]


async def test_failed_and_unknown_steps_are_named_and_the_rest_still_runs() -> None:
    skill, studio, registry = await _skill({
        "pause_s": 0,
        "protocols": {"отбой": ["сделай что-нибудь непонятное", "заблокируй компьютер", "открой плеер"]},
    })
    result = await registry.invoke("protocols.run", {"name": "отбоя"})  # на слух — падеж
    assert result.ok
    said = result.speech_for("ru") or ""
    assert "выполнен не весь" in said and "сделай что-нибудь непонятное" in said and "заблокируй компьютер" in said
    assert ("launch", {"program": "плеер"}) in studio.calls, "остальные шаги выполнены"


async def test_protocol_cannot_start_a_protocol() -> None:
    skill, _, registry = await _skill({"pause_s": 0, "protocols": {"круг": ["протокол круг"]}})
    result = await registry.invoke("protocols.run", {"name": "круг"})
    assert not result.ok, "протокол внутри протокола не запускается"


@pytest.mark.parametrize(("settings", "expected"), [({}, "Протоколов пока нет"), ({"protocols": {"работа": ["открой x"]}}, "Протокола отпуск нет")])
async def test_unknown_protocol_says_what_exists(settings: dict[str, Any], expected: str) -> None:
    skill, _, registry = await _skill(settings)
    result = await registry.invoke("protocols.run", {"name": "отпуск"})
    assert not result.ok and (result.speech_for("ru") or "").startswith(expected)


async def test_a_watched_protocol_runs_itself_and_unwinds_afterwards() -> None:
    """Игру запустили — шаги пошли; игру закрыли — пошли шаги «после»."""
    skill, studio, registry = await _skill({
        "pause_s": 0,
        "protocols": {
            "игра": {
                "when": {"process": ["javaw.exe"]},
                "steps": [{"tool": "studio.volume", "args": {"level": 10}}],
                "after": [{"tool": "studio.volume", "args": {"level": 50}}],
            },
        },
    })
    game = skill._protocols["игра"]
    running = {"javaw.exe"}

    seen = lambda names: {name for name in names if name in running}  # noqa: E731

    await skill._look([game], seen)
    assert studio.calls == [("volume", {"level": 10})], "игра началась — шаги выполнились"

    await skill._look([game], seen)
    assert len(studio.calls) == 1, "пока игра идёт, протокол не перезапускается"

    running.clear()
    await skill._look([game], seen)
    assert studio.calls[-1] == ("volume", {"level": 50}), "игра закрылась — вернули как было"
