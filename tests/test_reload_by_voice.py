"""Перезагрузка модуля голосом: «переподключи модуль браузер».

Инструмент был и раньше, но добраться до него можно было только по точному
английскому имени — то есть никак, если говоришь вслух.
"""

from __future__ import annotations

import pytest

from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class _Browser(Skill):
    meta = SkillMeta(name="browser", description="Браузер", spoken=("браузер", "browser"))

    @tool()
    async def open_site(self) -> str:
        """Открыть сайт."""
        return "ok"


class _Weather(Skill):
    meta = SkillMeta(name="weather", description="Погода", spoken=("погода", "weather"))

    @tool()
    async def now(self) -> str:
        """Погода сейчас."""
        return "ok"


def test_meta_lists_all_names() -> None:
    """Настоящее имя и произносимые лежат вместе — искать надо по всем."""
    assert _Browser.meta.names == ("browser", "браузер", "browser")


def test_spoken_names_are_optional() -> None:
    """Скилл без произносимых имён остаётся рабочим скиллом."""
    meta = SkillMeta(name="light")

    assert meta.names == ("light",)
    assert meta.spoken == ()


#: Два скилла на диске: имена вслух объявляют они сами, и ядро о них не знает.
_SKILL = '''
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class {klass}Skill(Skill):
    meta = SkillMeta(name="{name}", description="демо", spoken={spoken})

    @tool()
    async def do_it(self) -> str:
        """Сделать что-нибудь."""
        return "ok"
'''


@pytest.fixture
def two_skills(tmp_path):
    """Каталог с браузером и погодой."""
    directory = tmp_path / "skills"
    directory.mkdir()
    (directory / "browser.py").write_text(
        _SKILL.format(klass="Browser", name="browser", spoken='("браузер", "browser")'),
        encoding="utf-8",
    )
    (directory / "weather.py").write_text(
        _SKILL.format(klass="Weather", name="weather", spoken='("погода", "weather")'),
        encoding="utf-8",
    )
    return directory


async def _manager(directory, events, registry, memory, llm, tts):
    """Поднять менеджер поверх временного каталога."""
    from jarvis.core.config import SkillsConfig
    from jarvis.core.skills import SkillManager

    manager = SkillManager(
        config=SkillsConfig(paths=(directory,)),
        events=events,
        tools=registry,
        memory=memory,
        llm=llm,
        tts=tts,
        root=directory.parent,
    )
    await manager.start()
    return manager


async def test_module_is_found_by_spoken_name(
    two_skills, events, registry, memory, llm, tts
) -> None:
    """«Браузер» находит `browser`, хотя общего у них — ничего.

    Это перевод, а не написание: никакое сравнение букв тут не поможет, и
    поэтому имена вслух объявляет сам скилл.
    """
    manager = await _manager(two_skills, events, registry, memory, llm, tts)

    assert manager.find("браузер") == "browser"
    assert manager.find("погоду") == "weather", "падеж не должен мешать"
    assert manager.find("weather") == "weather"
    await manager.stop()


async def test_unknown_module_is_named_not_guessed(
    two_skills, events, registry, memory, llm, tts
) -> None:
    """Незнакомое слово — отказ, а не ближайший сосед.

    Перезагрузка не того модуля тихо снимает его инструменты: со стороны это
    выглядит как «часть команд вдруг перестала работать».
    """
    manager = await _manager(two_skills, events, registry, memory, llm, tts)

    assert manager.find("ерунда") is None
    await manager.stop()


def test_phrases_reach_the_tool_without_the_model() -> None:
    """Фразы есть, а в каталог модели инструмент не уходит.

    Это не противоречие: `phrase_index` строится по всем инструментам, и шаблон
    срабатывает **до** модели, то есть бесплатно. Платить за перезагрузку
    входными токенами в каждом запросе незачем — просят её редко и говорят при
    этом одинаково.
    """
    from jarvis.core.builtin import CoreTools
    from jarvis.core.tools import collect_tools

    tools = {tool.name: tool for tool in collect_tools(_stub_core(), namespace="core")}
    spec = tools["core.reload_skill"].spec

    assert spec.routable is False, "служебный инструмент не место в каталоге модели"
    assert any("{skill}" in phrase for phrase in spec.phrases)
    assert "переподключи модуль {skill}" in spec.phrases


def _stub_core():
    """Ядровые инструменты без настоящих сервисов — нужны только их описания."""
    from unittest.mock import MagicMock

    from jarvis.core.builtin import CoreTools

    return CoreTools(
        llm=MagicMock(),
        memory=MagicMock(),
        registry=MagicMock(),
        skills=MagicMock(),
    )
