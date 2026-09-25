"""Шаблон уступает, если инструмент не узнаёт пойманное значение.

Стенд 25.09.2026 (`tools/agent_bench`): 13 просьб из 20 забирал один шаблон
`открой {program}` — от «открой в википедии статью про Тверь» до «открой мои
подписки на ютубе». Ни одна не была программой, а до модели, которая поняла бы
их, они не доходили. Это и есть то, из-за чего ассистент ощущался набором
команд: дешёвый слой был слишком уверен в себе.

После правки те же 20 просьб дали 17 попаданий в подходящий инструмент вместо
11 — без единого нового скилла.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Mapping

from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router.resolvers.phrase import PhraseResolver
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Launcher:
    """Скилл в миниатюре: узнаёт только «стим»."""

    def knows(self, arguments: Mapping[str, str]) -> bool:
        return arguments.get("program") == "стим"

    @tool(phrases=["открой {program}"], reversible=True, recognizes="knows")
    async def launch(self, program: str) -> ToolResult:
        return ToolResult.success(program)


class Sites:
    """Второй шаблон, более общий: забирает то, что отпустил первый."""

    @tool(phrases=["открой {site}"], reversible=True)
    async def open_site(self, site: str) -> ToolResult:
        return ToolResult.success(site)


def _resolver(*skills: tuple[object, str]) -> PhraseResolver:
    registry = ToolRegistry()
    for skill, namespace in skills:
        for item in collect_tools(skill, namespace=namespace):
            registry.register(item)
    return PhraseResolver(registry)


async def test_a_recognized_value_is_taken() -> None:
    intent = await _resolver((Launcher(), "windows")).resolve(Utterance(text="открой стим"))
    assert intent is not None and intent.tool == "windows.launch"


async def test_an_unrecognized_value_is_given_up() -> None:
    """Шаблон совпал по форме, но это не программа — уступаем дальше по цепочке."""
    intent = await _resolver((Launcher(), "windows")).resolve(
        Utterance(text="открой в википедии статью про тверь")
    )
    assert intent is None, "модель разберёт это лучше, чем запуск программы"


async def test_the_next_template_gets_what_the_first_let_go() -> None:
    resolver = _resolver((Launcher(), "windows"), (Sites(), "browser"))
    intent = await resolver.resolve(Utterance(text="открой гитхаб"))
    assert intent is not None and intent.tool == "browser.open_site"


async def test_a_broken_check_does_not_take_away_the_phrases() -> None:
    """Ошибка в проверке не должна отнимать у скилла все его фразы разом."""

    class Broken:
        def knows(self, arguments: Mapping[str, str]) -> bool:
            raise RuntimeError("сломалось")

        @tool(phrases=["открой {program}"], reversible=True, recognizes="knows")
        async def launch(self, program: str) -> ToolResult:
            return ToolResult.success(program)

    intent = await _resolver((Broken(), "windows")).resolve(Utterance(text="открой стим"))
    assert intent is not None and intent.tool == "windows.launch"


def _windows() -> object:
    path = Path(__file__).resolve().parent.parent / "skills" / "windows" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_windows_recognizes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_program_template_keeps_everything_that_used_to_work() -> None:
    """Правило выведено из живых логов: всё срабатывавшее — не длиннее двух слов.

    «Телеграм», «prism launcher», «призом лаунчер» обязаны остаться за
    запуском программ; «в википедии статью про тверь» — уйти дальше.
    """
    windows = _windows()
    skill = object.__new__(windows.WindowsSkill)
    skill._catalog = {"Steam": "C:/Menu/Steam.lnk", "Prism Launcher": "C:/Menu/Prism.lnk"}

    assert skill._knows_program({"program": "стим"}), "есть в каталоге"
    assert skill._knows_program({"program": "призом лаунчер"}), "два слова — за программой"
    assert skill._knows_program({"program": "гитхаб"}), "коротко — может быть сайтом"
    assert not skill._knows_program({"program": "в википедии статью про тверь"})
    assert not skill._knows_program({"program": "мои подписки на ютубе"})
    assert not skill._knows_program({"program": ""})
