"""Две глубины у «где снято»: быстрая модель и медленный агент наперегонки."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    """Загрузить модуль скилла так же, как это делает загрузчик."""
    path = _ROOT / "skills" / "photo_place" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"photo_place_{name}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent = _load("agent")


# --- разбор ответа агента ----------------------------------------------------


def test_three_lines_become_a_verdict() -> None:
    verdict = agent.parse_verdict(
        "МЕСТО: Лондон (Хайгейт, N6), Великобритания\n"
        "ТОЧКА: 51.5718, -0.1502\n"
        "ТОЧНОСТЬ: здание — паб The Gatehouse",
        seconds=25.0,
    )
    assert verdict is not None
    assert verdict.point == (51.5718, -0.1502)
    assert verdict.sure and "Хайгейт" in verdict.place
    assert verdict.precision.startswith("здание")


def test_i_do_not_know_is_not_a_place() -> None:
    """На снимке птицы крупным планом агент честно говорит «не знаю» — и это отказ."""
    verdict = agent.parse_verdict(
        "МЕСТО: не знаю — город не определить\nТОЧКА: -29.60, 30.38\nТОЧНОСТЬ: страна"
    )
    assert verdict is not None and not verdict.sure


def test_answer_out_of_form_is_no_answer() -> None:
    assert agent.parse_verdict("Похоже на Лондон, но не уверен") is None


def test_impossible_coordinates_are_dropped() -> None:
    verdict = agent.parse_verdict("МЕСТО: Где-то\nТОЧКА: 999.0, 0.5\nТОЧНОСТЬ: город")
    assert verdict is not None and verdict.point is None and not verdict.sure


# --- раздача снимка ----------------------------------------------------------


def test_handoff_gives_the_file_once_and_only_by_its_secret_path() -> None:
    """Снимок уходит на сервер владельца, но не в открытый интернет."""
    handoff = agent.Handoff(b"\xff\xd8\xff payload", host="127.0.0.1", port=8811)
    with handoff:
        assert handoff.name.endswith(".jpg") and len(handoff.name) > 12
        wrong = httpx.get("http://127.0.0.1:8811/other.jpg", timeout=5)
        assert wrong.status_code == 404
        assert not handoff.taken, "чужой путь файла не выдаёт"
        right = httpx.get(handoff.url, timeout=5)
        assert right.status_code == 200 and right.content == b"\xff\xd8\xff payload"
        assert handoff.taken
    with pytest.raises(httpx.HTTPError):
        httpx.get(handoff.url, timeout=2)


# --- когда поправлять вслух --------------------------------------------------


def test_correction_only_when_it_changes_the_answer() -> None:
    skill = _load("skill")
    london = (51.5718, -0.1502)
    near = (51.5720, -0.1505)
    other = (55.7558, 37.6173)
    assert not skill.worth_correcting(london, near, 500), "тридцать метров — не новость"
    assert skill.worth_correcting(london, other, 500), "другой город — сказать обязательно"
    assert skill.worth_correcting(None, london, 500), "первая глубина не смогла — сказать"
    assert not skill.worth_correcting(london, None, 500), "агенту нечего сказать — молчим"


# --- гонка двух глубин -------------------------------------------------------


class _Scope:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def spawn(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task


def _skill(deep: Any, *, wait: float = 0.05, apart: float = 500.0) -> Any:
    """Скилл с подменённой второй глубиной и записывающей политикой речи."""
    import logging
    from types import SimpleNamespace

    module = _load("skill")
    skill = module.PhotoPlaceSkill()
    said: list[str] = []
    skill._context = SimpleNamespace(  # type: ignore[assignment]
        scope=_Scope(),
        logger=logging.getLogger("test.photo_place"),
        announcer=SimpleNamespace(offer=lambda text, **kwargs: said.append(text) or "say"),
    )
    skill._agent = deep
    skill._agent_wait, skill._agent_late, skill._agent_apart = wait, 5.0, apart
    return skill, module, said


class _Agent:
    """Вторая глубина, которая отвечает через заданное время."""

    ready = True

    def __init__(self, verdict: Any, after: float = 0.0) -> None:
        self._verdict, self._after = verdict, after
        self.asked = 0

    async def place(self, image: bytes, **kwargs: Any) -> Any:
        self.asked += 1
        await asyncio.sleep(self._after)
        return self._verdict


async def test_the_deep_answer_wins_when_it_arrives_in_time() -> None:
    verdict = agent.Verdict(place="Лондон, Хайгейт", point=(51.5718, -0.1502),
                            precision="здание", seconds=1.0)
    skill, _, _ = _skill(_Agent(verdict))
    quick_ran = False

    async def quick() -> Any:
        nonlocal quick_ran
        await asyncio.sleep(10)
        quick_ran = True
        raise AssertionError("быстрый путь не должен был доиграть")

    result = await skill._race(b"jpeg", quick(), "ru", "")
    assert result.ok and result.value["place"] == "Лондон, Хайгейт"
    assert result.value["depth"] == "agent" and result.value["map"]
    assert not quick_ran, "успел агент — быстрый путь отменяется"


async def test_a_slow_agent_does_not_hold_the_answer() -> None:
    """Главное в замысле: ждать агента дольше `agent_wait_s` никто не обязан."""
    verdict = agent.Verdict(place="Москва", point=(55.7558, 37.6173), precision="улица", seconds=9.0)
    skill, module, said = _skill(_Agent(verdict, after=0.3))

    async def quick() -> Any:
        return module.ToolResult.success({"place": "Тверь", "latitude": 56.86, "longitude": 35.9})

    result = await skill._race(b"jpeg", quick(), "ru", "")
    assert result.value["place"] == "Тверь", "сказали то, что нашла первая глубина"
    await asyncio.gather(*skill._context.scope.tasks)
    assert said == ["Уточняю по фотографии: Москва."], "агент разошёлся — досказал позже"


async def test_a_late_agent_that_agrees_says_nothing() -> None:
    verdict = agent.Verdict(place="Тверь, набережная", point=(56.8601, 35.9010),
                            precision="улица", seconds=9.0)
    skill, module, said = _skill(_Agent(verdict, after=0.3))

    async def quick() -> Any:
        return module.ToolResult.success({"place": "Тверь", "latitude": 56.86, "longitude": 35.9})

    await skill._race(b"jpeg", quick(), "ru", "")
    await asyncio.gather(*skill._context.scope.tasks)
    assert said == [], "то же место — молчим, речь без вопроса дорога"


async def test_without_the_agent_everything_works_as_before() -> None:
    skill, module, _ = _skill(None)

    async def quick() -> Any:
        return module.ToolResult.success({"place": "Тверь"})

    result = await skill._race(b"jpeg", quick(), "ru", "")
    assert result.value == {"place": "Тверь"}
