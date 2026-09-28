"""Minecraft через Prism: запускается сама сборка, а не лаунчер (28.09.2026)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("prism_skill_test", _ROOT / "skills" / "prism" / "skill.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prism = _load()


def _instance(root: Path, folder: str, name: str, last: int | None) -> None:
    path = root / "instances" / folder
    path.mkdir(parents=True)
    lines = ["[General]", f"name={name}"] + ([f"lastLaunchTime={last}"] if last is not None else [])
    (path / "instance.cfg").write_text("\n".join(lines), "utf-8")


def test_instances_are_read_from_prism(tmp_path: Path) -> None:
    _instance(tmp_path, "1.12.2", "1.12.2", 1790163992078)
    _instance(tmp_path, "pvp-duo-1.21.8-voxy-v2", "pvp-duo-1.21.8-voxy-v2", 1790590018237)
    _instance(tmp_path, "Fabulously Optimized", "Fabulously Optimized 12.2.2 for 1.21.11", None)
    found = prism.read_instances(tmp_path)
    assert [item.folder for item in found] == ["1.12.2", "Fabulously Optimized", "pvp-duo-1.21.8-voxy-v2"]
    assert found[1].last_launch == 0


def test_without_a_name_the_last_played_instance_starts(tmp_path: Path) -> None:
    """«Запусти майн» — та сборка, в которую играли последней."""
    _instance(tmp_path, "1.12.2", "1.12.2", 1790163992078)
    _instance(tmp_path, "pvp-duo-1.21.8-voxy-v2", "pvp-duo-1.21.8-voxy-v2", 1790590018237)
    chosen = prism.pick(prism.read_instances(tmp_path), "")
    assert chosen.folder == "pvp-duo-1.21.8-voxy-v2"


def test_a_named_instance_is_found_by_ear(tmp_path: Path) -> None:
    _instance(tmp_path, "Create Aero", "Create Aero", 1)
    _instance(tmp_path, "pvp-duo-1.21.8-voxy-v2", "pvp-duo-1.21.8-voxy-v2", 2)
    instances = prism.read_instances(tmp_path)
    assert prism.pick(instances, "create aero").folder == "Create Aero"
    assert prism.pick(instances, "фортнайт") is None
    assert prism.pick([], "") is None
