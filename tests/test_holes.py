"""Дыры, найденные стендом `tools/agent_bench` 25.09.2026, — каждая своим тестом.

После «проверь себя» и точных рук на стенде остались ошибки уже не ума, а
простые: нечёткое совпадение, картинки как обычный поиск, «открой его» как
программа, и найденное место, которое следующая просьба не видела.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router.resolvers.phrase import PhraseResolver
from jarvis.core.tools import ToolRegistry, collect_tools, tool

_ROOT = Path(__file__).resolve().parent.parent


def _load(relative: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


windows = _load("skills/windows/skill.py", "holes_windows")
browser = _load("skills/browser/skill.py", "holes_browser")
photo = _load("skills/photo_place/skill.py", "holes_photo")


# --- «диспетчер устройств» — не диспетчер задач -------------------------------


def test_one_shared_word_is_not_a_match() -> None:
    """У многословного запроса учитываются все слова, а не одно у края."""
    catalog = {"диспетчер задач": "taskmgr.exe", "Steam": "steam.lnk"}
    assert windows.match_program("диспетчер печати", catalog) is None
    assert windows.match_program("диспетчер задач", catalog) == ("диспетчер задач", "taskmgr.exe")


def test_short_names_still_match_by_their_edge() -> None:
    """«Обс» → OBS Studio: правило касается только многословных запросов."""
    catalog = {"OBS Studio": "obs.lnk"}
    assert windows.match_program("обс", catalog) == ("OBS Studio", "obs.lnk")


def test_misheard_names_from_the_owners_logs_still_work() -> None:
    """«Призом лаунчера» — как Whisper слышал Prism Launcher. Правило «все
    слова» его не трогает: «лаунчер» — общее слово и в ключи не идёт."""
    catalog = {"Prism Launcher": "prism.lnk", "диспетчер задач": "taskmgr.exe"}
    for spoken in ("призом лаунчера", "призм лаунчера", "prism launcher"):
        assert windows.match_program(spoken, catalog) == ("Prism Launcher", "prism.lnk"), spoken


def test_admin_tools_are_in_the_catalog() -> None:
    for spoken in ("диспетчер устройств", "редактор реестра", "сведения о системе"):
        assert windows.match_program(spoken, windows.BUILT_IN) is not None, spoken


# --- «открой его», «открой в картах» — не программы --------------------------


def test_pointers_and_places_are_not_program_names() -> None:
    skill = object.__new__(windows.WindowsSkill)
    skill._catalog = {"Steam": "steam.lnk"}
    assert not skill._knows_program({"program": "его"})
    assert not skill._knows_program({"program": "в картах"})
    assert not skill._knows_program({"program": "на ютубе трендовые"})
    assert skill._knows_program({"program": "стим"})


# --- вторая просьба в слоте — уступаем ----------------------------------------


class Search:
    @tool(phrases=["найди на {engine} {query}"], reversible=True)
    async def search(self, engine: str, query: str) -> ToolResult:
        """Поиск."""
        return ToolResult.success(query)


def _resolver(*skills: tuple[object, str]) -> PhraseResolver:
    registry = ToolRegistry()
    for skill, namespace in skills:
        for item in collect_tools(skill, namespace=namespace):
            registry.register(item)
    return PhraseResolver(registry)


async def test_a_second_request_in_the_slot_gives_the_template_up() -> None:
    """«…видео Veritasium и открой его» — две просьбы, работа для плана."""
    resolver = _resolver((Search(), "browser"))
    whole = await resolver.resolve(Utterance(text="найди на ютубе видео veritasium и открой его"))
    assert whole is None


async def test_a_conjunction_inside_a_name_stays() -> None:
    """«И» внутри названия — не вторая просьба: глагол следом не повелительный."""
    resolver = _resolver((Search(), "browser"))
    found = await resolver.resolve(Utterance(text="найди на ютубе я сошла с ума и не помню"))
    assert found is not None and found.arguments["query"] == "я сошла с ума и не помню"


# --- картинки — это картинки -------------------------------------------------


def test_pictures_are_searched_as_pictures() -> None:
    assert browser.images_asked("картинки с котами") == (True, "котами")
    assert browser.images_asked("фото Эйфелевой башни") == (True, "Эйфелевой башни")
    assert browser.images_asked("фотошоп уроки") == (False, "фотошоп уроки")
    assert browser.images_asked("картинки") == (False, "картинки"), "без предмета — это не про картинки"


# --- «открой в картах» — про только что найденное ----------------------------


class Maps:
    """Точная фраза с проверкой: работает, только пока место свежее."""

    def __init__(self, fresh: bool) -> None:
        self.fresh = fresh

    def knows(self, arguments: Mapping[str, str]) -> bool:
        return self.fresh

    @tool(phrases=["открой в картах"], reversible=True, recognizes="knows")
    async def open_found(self) -> ToolResult:
        return ToolResult.success(None)


async def test_an_exact_phrase_gives_up_without_a_found_place() -> None:
    fresh = await _resolver((Maps(True), "photo_place")).resolve(Utterance(text="открой в картах"))
    stale = await _resolver((Maps(False), "photo_place")).resolve(Utterance(text="открой в картах"))
    assert fresh is not None and fresh.tool == "photo_place.open_found"
    assert stale is None, "без найденного места — обычные карты, а не пустое «открыл»"


def test_a_found_place_stays_fresh_for_half_an_hour() -> None:
    skill = object.__new__(photo.PhotoPlaceSkill)
    assert not skill._has_found({})
    skill._remember_place("Marina Bay Sands", (1.2834, 103.8607))
    assert skill._has_found({})
    skill._found = ("старое", (0.0, 0.0), time.monotonic() - photo.PLACE_FRESH_MIN * 60 - 1)
    assert not skill._has_found({})


# --- вторая просьба не просачивается ни через один резолвер со слотами --------


class Channels:
    @tool(phrases=["найди канал {channel}"], reversible=True)
    async def open_channel(self, channel: str) -> ToolResult:
        """Открыть канал."""
        return ToolResult.success(channel)


async def test_the_loose_resolver_does_not_swallow_a_second_request() -> None:
    """Живой прогон 25.09.2026: шаблон фраз уступил, а `loose` положил в канал
    «veritasium и открой его» — и прозвучало «Открываю канал veritasium и открой его»."""
    from jarvis.core.router.resolvers.loose import LooseResolver

    registry = ToolRegistry()
    for item in collect_tools(Channels(), namespace="page"):
        registry.register(item)
    loose = LooseResolver(registry)
    found = await loose.resolve(
        Utterance(text="найди на ютубе последнее видео канала veritasium и открой его")
    )
    assert found is None


def test_the_second_request_check_is_shared() -> None:
    from jarvis.core.router.templates import second_request

    assert second_request({"channel": "veritasium и открой его"})
    assert second_request({"q": "котов и потом включи музыку"})
    assert not second_request({"track": "я сошла с ума и не помню"})
    assert not second_request({"n": 5})


def test_windows_in_a_request_is_not_required_in_the_name() -> None:
    """«Открой настройки звука Windows» — решалось раньше, и должно решаться."""
    assert windows.match_program("настройки звука windows", windows.BUILT_IN) == (
        "настройки звука", "ms-settings:sound",
    )
