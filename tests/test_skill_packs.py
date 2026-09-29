"""Наборы команд для программ (29.09.2026): данные, фразы и то, что они не отнимают чужое."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.contracts import ToolResult

_ROOT = Path(__file__).resolve().parent.parent


def _load(relative: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


packs = _load("skills/packs/skill.py", "packs_skill_test")
power = _load("skills/windows/power.py", "windows_power_test")

SHIPPED, ERRORS = packs.load_packs([packs.PACKS])


def test_shipped_packs_read_without_errors() -> None:
    assert not ERRORS
    assert {pack.id for pack in SHIPPED} >= {"fl_studio", "minecraft", "vlc"}


def test_every_key_combination_in_the_packs_can_be_pressed() -> None:
    """Опечатка в сочетании нашлась бы только вслух — «не получилось нажать»."""
    broken = [
        f"{pack.id}.{command.id}: {keys}"
        for pack in SHIPPED for command in pack.commands for keys in command.keys
        if not power.key_codes(keys)
    ]
    assert not broken


def _declared_phrases() -> set[str]:
    """Все фразы, объявленные в `phrases=[...]` ядра и скиллов, — из исходников."""
    sources = [_ROOT / "jarvis" / "core" / "builtin.py", *_ROOT.glob("skills/**/skill.py")]
    found: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "phrases" and isinstance(node.value, (ast.List, ast.Tuple)):
                found |= {item.value.lower() for item in node.value.elts
                          if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return found


def test_phrases_without_the_program_do_not_take_other_commands() -> None:
    """Фраза без названия программы срабатывает, пока та открыта, — и в это
    время отняла бы команду у остальной системы. Общие слова — только `alone: false`."""
    taken = _declared_phrases()
    clashes = [
        f"{pack.id}.{command.id}: {phrase!r}"
        for pack in SHIPPED for command in pack.commands if command.alone
        for phrase in command.phrases if phrase in taken
    ]
    assert not clashes


def test_a_pack_without_processes_is_an_error_not_a_silent_skip() -> None:
    with pytest.raises(packs.PackError, match="processes"):
        packs.parse_pack("broken", {"commands": {"x": {"phrases": ["a"], "keys": "f1"}}})


def test_program_name_goes_after_the_phrase() -> None:
    pack = packs.parse_pack("fl", {
        "name": "FL", "processes": ["FL64.exe"], "places": ["во фл", "В ФЛ Студии"],
        "commands": {"save": {"phrases": ["Сохрани  проект"], "keys": "ctrl+s"}},
    })
    assert packs.placed(pack.commands[0], pack) == ("сохрани проект во фл", "сохрани проект в фл студии")
    assert pack.processes == frozenset({"fl64.exe"})


@pytest.mark.parametrize(("keys", "codes"), [
    ("ctrl+shift+m", [0x11, 0x10, ord("M")]), ("alt+right", [0x12, 0x27]), ("f11", [0x7A]), ("esc", [0x1B]),
    ("ctrl+щ", []), ("ctrl+nonsense", []),
])
def test_key_names(keys: str, codes: list[int]) -> None:
    assert power.key_codes(keys) == codes


def test_the_front_window_of_the_program_wins() -> None:
    pack = packs.parse_pack("vlc", {"processes": ["vlc.exe"], "commands": {}})
    windows = [
        packs.Window(title="Браузер", image="browser.exe", front=True),
        packs.Window(title="Фильм — VLC", image="vlc.exe", front=False),
    ]
    assert packs.pick_window(windows, pack).title == "Фильм — VLC"
    assert packs.pick_window(windows[:1], pack) is None


async def test_keys_go_to_the_program_and_focus_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Команда FL Studio из браузера: вывести FL, нажать, вернуть браузер."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class Tools:
        async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
            calls.append((name, arguments))
            return ToolResult.success(None)

    monkeypatch.setattr(packs, "FOCUS_SETTLE_S", 0.0)
    monkeypatch.setattr(packs, "top_windows", lambda: [
        packs.Window(title="YouTube — Браузер", image="browser.exe", front=True),
        packs.Window(title="FL Studio 21", image="fl64.exe", front=False),
    ])

    class Skill(packs.PacksSkill):
        log = logging.getLogger("test-packs")
        tools = Tools()  # type: ignore[assignment]

    skill = object.__new__(Skill)
    pack = next(pack for pack in SHIPPED if pack.id == "fl_studio")
    save = next(command for command in pack.commands if command.id == "save")
    result = await skill._run(pack, save)

    assert result.ok and result.speech_for("ru") == "Проект сохранён."
    assert calls == [
        (packs.FOCUS_TOOL, {"title": "FL Studio 21"}),
        (packs.PRESS_TOOL, {"combination": "ctrl+s"}),
        (packs.FOCUS_TOOL, {"title": "YouTube — Браузер"}),
    ]


async def test_a_closed_program_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packs, "top_windows", lambda: [])
    skill = object.__new__(packs.PacksSkill)
    pack = next(pack for pack in SHIPPED if pack.id == "vlc")
    result = await skill._run(pack, pack.commands[0])
    assert not result.ok and result.speech_for("ru") == "VLC не открыт."


async def test_tools_are_registered_from_data(tmp_path: Path) -> None:
    (tmp_path / "obs.yaml").write_text(
        "name: OBS\nprocesses: [obs64.exe]\nplaces: ['в обс']\n"
        "commands:\n  record:\n    phrases: ['начни стрим']\n    keys: ctrl+f9\n    focus: false\n"
        "  pause:\n    phrases: ['пауза']\n    keys: ctrl+f10\n    alone: false\n",
        "utf-8",
    )
    registered: list[Any] = []
    settings = {"folders": [str(tmp_path)], "disabled": ["fl_studio", "minecraft", "vlc"]}
    skill = packs.PacksSkill()
    skill._context = SimpleNamespace(
        setting=lambda key, default=None: settings.get(key, default),
        logger=logging.getLogger("test-packs"),
        scope=SimpleNamespace(register_tool=registered.append),
    )
    await skill.on_setup()
    phrases = {tool.name: tool.spec.phrases for tool in registered}
    assert phrases == {
        "packs.obs_record": ("начни стрим в обс",),
        "packs.obs_record_here": ("начни стрим",),
        "packs.obs_pause": ("пауза в обс",),
    }
    assert all(not tool.spec.routable for tool in registered)
    assert inspect.iscoroutinefunction(registered[0].handler.func)
