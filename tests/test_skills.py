"""Скиллы: автозагрузка из каталога, изоляция сбоев, отзыв scope, перезагрузка."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.bus import LocalEventBus
from jarvis.core.config import SkillsConfig
from jarvis.core.errors import SkillError
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


async def test_folder_name_and_passport_name_are_interchangeable(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Папка `powershell`, а в паспорте `clipboard` (живой случай 14.09.2026).

    Панель выключала модуль по имени папки, `unload` его не находил, модуль
    оставался загруженным, и включение падало на «уже загружен».
    """
    directory = tmp_path / "skills"
    (directory / "powershell").mkdir(parents=True)
    (directory / "powershell" / "skill.py").write_text(
        _GOOD_SKILL.format(version="1").replace('name="demo"', 'name="clipboard"'), encoding="utf-8"
    )
    manager = _manager(directory, events, registry, memory, llm, tts)
    await manager.start()
    assert manager.loaded == ("clipboard",)
    assert manager.get("powershell") is manager.get("clipboard")
    assert manager.tools_of("powershell") == ("clipboard.do_it",)

    await manager.unload("powershell")
    assert manager.loaded == ()

    await manager.adopt("powershell")
    await manager.adopt("powershell")  # второй раз — перезагрузка, а не «уже загружен»
    assert manager.loaded == ("clipboard",)
    await manager.stop()


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


# --- подскиллы ---------------------------------------------------------------

_SUB_SKILL = '''
from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class PartSkill(Skill):
    """Часть главного скилла."""

    meta = SkillMeta(name="part", description="Подскилл")

    @tool()
    async def do_part(self) -> ToolResult:
        """Сделать частью."""
        return ToolResult.success(True)
'''


def _nested(skills_dir: Path, *, main: bool = True) -> None:
    """Разложить главный скилл и подскилл внутри него."""
    if main:
        host = skills_dir / "host"
        host.mkdir()
        (host / "skill.py").write_text(
            _GOOD_SKILL.format(version="1").replace('name="demo"', 'name="host"'),
            encoding="utf-8",
        )
    else:
        host = skills_dir / "host"
        host.mkdir()
    part = host / "part"
    part.mkdir()
    (part / "skill.py").write_text(_SUB_SKILL, encoding="utf-8")


async def test_subskill_loads_after_its_main_one(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Подскилл лежит внутри главного и грузится после него.

    Он не сосед: скилл страницы не работает без браузера в принципе, и в общем
    списке они выглядели как две независимые возможности «для браузера».
    """
    _nested(skills_dir)
    manager = _manager(skills_dir, events, registry, memory, llm, tts)

    await manager.start()

    assert manager.tree == ("demo", "host", "host/part")
    # Имя инструмента от вложенности не зависит: на такие имена ссылается
    # выученное в памяти.
    assert registry.has("part.do_part")
    await manager.stop()


async def test_subskill_is_skipped_without_its_main_one(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Нет главного — подскилл даже не пробуем: он на нём и держится."""
    _nested(skills_dir, main=False)
    manager = _manager(skills_dir, events, registry, memory, llm, tts)

    await manager.start()

    assert "part" not in manager.loaded
    assert not registry.has("part.do_part")
    await manager.stop()


async def test_reloading_the_main_one_brings_back_its_parts(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Подскиллы держатся на инструментах главного и едут вместе с ним."""
    _nested(skills_dir)
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    await manager.reload("host")

    assert manager.tree == ("demo", "host", "host/part")
    assert registry.has("part.do_part"), "подскилл должен вернуться сам"
    await manager.stop()


async def test_deeper_nesting_is_not_a_skill(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Уровень вложенности ровно один: дерево ничего не объясняет."""
    _nested(skills_dir)
    deeper = skills_dir / "host" / "part" / "deeper"
    deeper.mkdir()
    (deeper / "skill.py").write_text(
        _SUB_SKILL.replace('name="part"', 'name="deeper"'), encoding="utf-8"
    )
    manager = _manager(skills_dir, events, registry, memory, llm, tts)

    await manager.start()

    assert "deeper" not in manager.loaded
    await manager.stop()


async def test_new_skill_on_disk_can_be_connected_without_restart(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Скилл, появившийся на диске после запуска, подключается на ходу.

    `reload` для этого не годится: он начинает с записи о загруженном скилле, а
    у нового её по определению нет. Это и есть то, что превращает «файл
    написан» в «умение появилось» — без него ассистент, написавший себе скилл,
    ждал бы перезапуска.
    """
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()
    assert "fresh" not in manager.loaded

    (skills_dir / "fresh.py").write_text(
        _GOOD_SKILL.format(version="1").replace('name="demo"', 'name="fresh"'),
        encoding="utf-8",
    )
    record = await manager.adopt("fresh")

    assert record.name == "fresh"
    assert "fresh" in manager.loaded


async def test_adopting_a_loaded_skill_just_reloads_it(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """«Подключи такой-то» на знакомом имени значит «перечитай с диска»."""
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    (skills_dir / "demo.py").write_text(
        _GOOD_SKILL.format(version="2"), encoding="utf-8"
    )
    record = await manager.adopt("demo")

    assert record.instance.meta.version == "2"


async def test_adopting_what_is_not_on_disk_is_an_error(
    skills_dir: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts
) -> None:
    """Несуществующий скилл — понятная ошибка, а не молчание."""
    manager = _manager(skills_dir, events, registry, memory, llm, tts)
    await manager.start()

    with pytest.raises(SkillError):
        await manager.adopt("призрак")


_HANGING_SKILL = '''
import asyncio

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class HangingSkill(Skill):
    """Скилл, который не может остановиться — как браузер 14.09.2026."""

    meta = SkillMeta(name="hanging", description="Висит на остановке")

    async def on_stop(self) -> None:
        await asyncio.Event().wait()

    @tool(reversible=True)
    async def ping(self) -> ToolResult:
        """Ответить."""
        return ToolResult.success("pong")
'''


async def test_hanging_skill_is_unloaded_by_timeout(
    tmp_path: Path, events: LocalEventBus, registry: ToolRegistry, memory, llm, tts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Зависший на остановке скилл не держит выключение всего ассистента."""
    import asyncio

    from jarvis.core.skills import manager as manager_module

    monkeypatch.setattr(manager_module, "SKILL_STOP_TIMEOUT_S", 0.1)
    directory = tmp_path / "skills"
    directory.mkdir()
    (directory / "hanging.py").write_text(_HANGING_SKILL, encoding="utf-8")
    manager = _manager(directory, events, registry, memory, llm, tts)
    await manager.start()
    assert registry.has("hanging.ping")

    await asyncio.wait_for(manager.stop(), 2.0)

    assert manager.loaded == ()
    assert not registry.has("hanging.ping"), "инструменты сняты, хоть остановка и не дождалась"
