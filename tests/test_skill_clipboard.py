"""Буфер обмена: картинка, а не только текст.

`Get-Clipboard -Raw` видит только текст, и скриншот в буфере выглядел пустотой:
план «отправь скриншот из буфера в Избранное» отвечал «картинки нет» (живой лог
14.09.2026, 11:04). Сам буфер тут не трогается — подменяется то, что он отдал.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "powershell" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_clipboard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clipboard = _load()


def _skill(monkeypatch: pytest.MonkeyPatch, *, text: str, image: Any) -> Any:
    skill = clipboard.ClipboardSkill()
    skill._backend = clipboard._Backend(name="fake", read_cmd=[], write_cmd=[])
    monkeypatch.setattr(clipboard, "_read_sync", lambda backend: text)
    monkeypatch.setattr(clipboard, "_grab_image_sync", lambda: image)
    return skill


def test_screenshot_becomes_png() -> None:
    data, width, height = clipboard._png(Image.new("RGB", (40, 30), "cyan"))
    assert (width, height) == (40, 30)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_copied_image_file_is_read(tmp_path: Path) -> None:
    picture = tmp_path / "фото.jpg"
    Image.new("RGB", (12, 8), "red").save(picture)
    grabbed = clipboard._png([str(tmp_path / "notes.txt"), str(picture)])
    assert grabbed is not None and grabbed[1:] == (12, 8)


@pytest.mark.parametrize("grabbed", [None, "просто текст", [], ["notes.txt"]])
def test_no_image_is_none(grabbed: Any) -> None:
    assert clipboard._png(grabbed) is None


async def test_read_reports_image_instead_of_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = _skill(monkeypatch, text="", image=(b"png", 1920, 1080))
    result = await skill.read_clipboard()
    assert result.ok and result.value["image"] is True
    assert result.speech_for("ru") == "В буфере обмена картинка 1920 на 1080."
    # Байты картинки в ответ не идут: его пересказывают модели в плане.
    assert b"png" not in repr(result.value).encode()


async def test_read_still_says_empty_when_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = _skill(monkeypatch, text="", image=None)
    result = await skill.read_clipboard()
    assert result.value["empty"] is True


async def test_image_tool_hands_png_to_other_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = _skill(monkeypatch, text="", image=(b"\x89PNGdata", 2, 3))
    result = await skill.image()
    assert result.ok and result.value == {"png": b"\x89PNGdata", "width": 2, "height": 3}
    empty = await _skill(monkeypatch, text="", image=None).image()
    assert not empty.ok


def test_image_tool_is_not_in_the_model_catalog() -> None:
    from jarvis.core.tools import collect_tools

    tools = {item.spec.name: item.spec for item in collect_tools(clipboard.ClipboardSkill(), namespace="clipboard")}
    assert tools["clipboard.image"].routable is False
