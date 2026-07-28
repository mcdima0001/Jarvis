"""Скиллы: автозагрузка из каталога, изоляция сбоев, отзыв scope, перезагрузка."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import SkillsConfig
from jarvis.core.skills import SkillManager
from jarvis.core.tools import ToolRegistry

_GOOD_SKILL = '''
from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class DemoSkill(Skill):
    """Демонстрационный скилл."""

    meta = SkillMeta(name="demo", description="Демо", version="{version}")

    async def on_setup(self) -> None:
        self.context.scope.subscribe("test.*", self._on_event)

    async def _on_event(self, event) -> None:
        pass

    @tool(phrases=["сделай красиво"])
    async def do_it(self) -> ToolResult:
        """Сделать красиво."""
        return ToolResult.success("{version}")
'''

_BROKEN_SKILL = '''
from jarvis.core.skills import Skill, SkillMeta


class BrokenSkill(Skill):
    """Скилл, который падает при инициализации."""

    meta = SkillMeta(name="broken", description="Ломается")

    async def on_setup(self) -> None:
        raise RuntimeError("не смог подняться")
'''


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Каталог скиллов с одним рабочим модулем."""
    directory = tmp_path / "skills"
    directory.mkdir()
    (directory / "demo.py").write_text(_GOOD_SKILL.format(version="1"), encoding="utf-8")
    return directory


def _manager(skills_dir: Path, events, registry, memory, llm, tts) -> SkillManager:
    """Собрать менеджер скиллов поверх временного каталога."""
    return SkillManager(
        config=SkillsConfig(paths=(skills_dir,)),
        events=events,
        tools=registry,
        memory=memory,
        llm=llm,
        tts=tts,
        root=skills_dir.parent,
    )


async def test_skill_loads_without_registration(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Файл в каталоге становится рабочим скиллом без правок ядра и конфига."""
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    assert manager.loaded == ("demo",)
    assert registry.has("demo.do_it")
    assert "сделай красиво" in registry.phrase_index()

    result = await registry.invoke("demo.do_it")
    assert result.value == "1"

    await manager.stop()


async def test_unload_revokes_everything(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """После выгрузки не остаётся ни инструментов, ни подписок."""
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()
    assert len(registry) == 1

    await manager.unload("demo")

    assert manager.loaded == ()
    assert len(registry) == 0
    assert registry.phrase_index() == {}


async def test_reload_picks_up_changes(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Перезагрузка подхватывает новую версию файла без перезапуска приложения."""
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    first = await registry.invoke("demo.do_it")
    assert first.value == "1"

    (skills_dir / "demo.py").write_text(_GOOD_SKILL.format(version="2"), encoding="utf-8")
    await manager.reload("demo")

    second = await registry.invoke("demo.do_it")
    assert second.value == "2"
    assert len(registry) == 1, "старая регистрация не должна остаться"

    await manager.stop()


async def test_broken_skill_does_not_block_others(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Сбойный скилл пропускается, остальные загружаются."""
    (skills_dir / "broken.py").write_text(_BROKEN_SKILL, encoding="utf-8")

    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    assert manager.loaded == ("demo",)
    assert registry.has("demo.do_it")

    await manager.stop()


async def test_disabled_skill_is_skipped(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Скилл из списка disabled не грузится."""
    manager = SkillManager(
        config=SkillsConfig(paths=(skills_dir,), disabled=frozenset({"demo"})),
        events=events,
        tools=registry,
        memory=memory,
        llm=llm,
        tts=tts,
        root=skills_dir.parent,
    )
    await manager.start()

    assert manager.loaded == ()
    await manager.stop()
