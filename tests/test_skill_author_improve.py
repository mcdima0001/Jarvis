"""Доработка существующего скилла руками агента.

Проверяется то, что может навредить по дороге: доработка ложится черновиком по
имени **папки**, агенту нельзя сменить имя в паспорте, а отклонённый черновик
убирается, не задевая рабочий скилл. Сеть не трогается — ответ агента подменён.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "author" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_author_improve", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


author = _load()

_CLIPBOARD = '''"""Буфер обмена."""

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class ClipboardSkill(Skill):
    """Буфер."""

    meta = SkillMeta(name="clipboard", description="Буфер", spoken=("буфер обмена", "clipboard"))

    @tool(phrases=["что в буфере обмена"], reversible=True)
    async def read_clipboard(self) -> ToolResult:
        """Прочитать."""
        return ToolResult.success("", speech="Пусто.")
'''


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "skills" / "powershell").mkdir(parents=True)
    (tmp_path / "skills" / "powershell" / "skill.py").write_text(_CLIPBOARD, encoding="utf-8")
    return tmp_path


def test_installed_skills_know_folder_passport_and_spoken(root: Path) -> None:
    assert author.installed_skills(root) == {"powershell": ("clipboard", ("буфер обмена", "clipboard"))}


@pytest.mark.parametrize("said", ["powershell", "clipboard", "буфер обмена", "буфер обмена"])
def test_skill_is_found_by_any_of_its_names(root: Path, said: str) -> None:
    assert author.pick_installed(said, author.installed_skills(root)) == "powershell"


def test_unknown_skill_stays_unknown(root: Path) -> None:
    assert author.pick_installed("погода", author.installed_skills(root)) == ""


def test_improve_prompt_forbids_renaming() -> None:
    prompt = author.improve_prompt(_CLIPBOARD, "читай картинки")
    assert "читай картинки" in prompt and _CLIPBOARD in prompt
    assert "Имя в meta не меняй" in prompt


def test_diff_shows_the_change(root: Path) -> None:
    draft = root / "drafts" / "powershell"
    draft.mkdir(parents=True)
    (draft / "skill.py").write_text(_CLIPBOARD.replace("Пусто.", "Картинка."), encoding="utf-8")
    diff = author.draft_diff(root, "powershell")
    assert "-        return ToolResult.success(\"\", speech=\"Пусто.\")" in diff
    assert "+        return ToolResult.success(\"\", speech=\"Картинка.\")" in diff


def _skill(root: Path, answer: str) -> Any:
    skill = author.AuthorSkill()
    skill._context = SimpleNamespace(
        setting=lambda key, default=None: {"url": "https://panel", "api_key": "k", "review": False}.get(key, default),
        logger=logging.getLogger("test.author"),
        root=root,
    )
    return skill, answer


async def test_improvement_lands_in_drafts_under_the_folder_name(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill, _ = _skill(root, "")
    await skill.on_setup()
    improved = _CLIPBOARD.replace("Пусто.", "Картинка.")

    async def ask(prompt: str) -> str:
        return improved

    async def check(path: Path) -> str:
        return ""

    monkeypatch.setattr(skill, "_ask", ask)
    monkeypatch.setattr(skill, "_check", check)
    said = await skill._improve("powershell", "читай картинки")

    assert (root / "drafts" / "powershell" / "skill.py").read_text(encoding="utf-8") == improved
    # Рабочий скилл не тронут, пока доработку не приняли.
    assert (root / "skills" / "powershell" / "skill.py").read_text(encoding="utf-8") == _CLIPBOARD
    assert "доработка скилла «powershell» готова" in said


async def test_renamed_skill_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill, _ = _skill(root, "")
    await skill.on_setup()

    async def ask(prompt: str) -> str:
        return _CLIPBOARD.replace('name="clipboard"', 'name="clipboard2"')

    monkeypatch.setattr(skill, "_ask", ask)
    with pytest.raises(ValueError):
        await skill._improve("powershell", "переименуй")
    assert not (root / "drafts").exists()


async def test_discard_removes_only_the_draft(root: Path) -> None:
    skill, _ = _skill(root, "")
    await skill.on_setup()
    draft = root / "drafts" / "powershell"
    draft.mkdir(parents=True)
    (draft / "skill.py").write_text(_CLIPBOARD, encoding="utf-8")
    (draft / "review.md").write_text("замечание", encoding="utf-8")

    result = await skill.discard("powershell")

    assert result.ok and not draft.exists()
    assert (root / "skills" / "powershell" / "skill.py").is_file()


def test_revise_prompt_carries_the_draft_remarks_and_wishes() -> None:
    prompt = author.revise_prompt("# черновик", "1. **run** блокирует цикл", "говори короче")
    assert "# черновик" in prompt and "run** блокирует цикл" in prompt and "говори короче" in prompt
    assert "Имя в meta не меняй" in prompt
    # Без замечаний строки про них нет вовсе.
    assert "Замечания разбора" not in author.revise_prompt("# черновик", "  ", "говори короче")


async def test_revision_starts_from_the_draft_and_clears_old_remarks(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Живой случай: владелец прочёл замечания в панели и отправил черновик повторно."""
    skill, _ = _skill(root, "")
    await skill.on_setup()
    draft = root / "drafts" / "powershell"
    draft.mkdir(parents=True)
    first = _CLIPBOARD.replace("Пусто.", "Картинка.")
    (draft / "skill.py").write_text(first, encoding="utf-8")
    (draft / "review.md").write_text("1. **speech** читает путь вслух\n", encoding="utf-8")
    prompts: list[str] = []
    second = _CLIPBOARD.replace("Пусто.", "Картинка из буфера.")

    async def ask(prompt: str) -> str:
        prompts.append(prompt)
        return second

    async def check(path: Path) -> str:
        return ""

    monkeypatch.setattr(skill, "_ask", ask)
    monkeypatch.setattr(skill, "_check", check)
    await skill._improve("powershell", "и не зачитывай путь", revise=True)

    assert first in prompts[0] and "читает путь вслух" in prompts[0] and "не зачитывай путь" in prompts[0]
    assert (draft / "skill.py").read_text(encoding="utf-8") == second
    # Разбор выключен — замечания к прошлой версии висеть не должны.
    assert (draft / "review.md").read_text(encoding="utf-8") == ""
