"""Мышь: направление прокрутки, координаты без угадывания, названия кнопок.

Настоящий SendInput тут не зовётся: подменяется то, что трогает систему.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "mouse" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_mouse", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mouse = _load()


def _skill(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[int, bool]]]:
    scrolled: list[tuple[int, bool]] = []
    monkeypatch.setattr(mouse, "_is_windows", lambda: True)
    monkeypatch.setattr(mouse, "_scroll", lambda notches, horizontal: scrolled.append((notches, horizontal)))
    return object.__new__(mouse.MouseSkill), scrolled


async def test_scroll_up_goes_up_and_down_goes_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Проскролль вверх» раньше крутило вниз: фраза вела в общий инструмент с -3."""
    skill, scrolled = _skill(monkeypatch)
    await skill.scroll_up()
    await skill.scroll_down()
    assert scrolled == [(3, False), (-3, False)]


def test_move_and_drag_do_not_guess_coordinates() -> None:
    """Без чисел курсор уезжал в угол, а перетаскивание тянуло туда же с зажатой кнопкой."""
    for method in (mouse.MouseSkill.move_cursor, mouse.MouseSkill.drag):
        parameters = inspect.signature(method).parameters
        assert parameters["x"].default is inspect.Parameter.empty, method.__name__
        assert parameters["y"].default is inspect.Parameter.empty, method.__name__


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [("", "left"), ("left", "left"), ("Правая", "right"), ("средняя", "middle"), ("боковая", None)],
)
def test_button_names(spoken: str, expected: str | None) -> None:
    assert mouse._normalize_button(spoken) == expected


def test_russian_plural() -> None:
    forms = ("щелчок", "щелчка", "щелчков")
    assert [mouse._plural_ru(n, *forms) for n in (1, 3, 5, 11, 21)] == ["щелчок", "щелчка", "щелчков", "щелчков", "щелчок"]
