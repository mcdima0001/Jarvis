"""Агентный цикл: цель, шаг, результат, следующий шаг.

Проверяется не «модель умная», а то, что цикл нельзя заставить сделать лишнее:
необратимый шаг он не выполняет, по кругу не ходит, после сорвавшегося шага не
продолжает и дальше предела не идёт. Каждое из этих правил стоит денег или
последствий, поэтому каждое закрыто отдельно.

Модель здесь подставная и отвечает по написанному: настоящая нужна для качества
плана, а проверяем мы поведение цикла вокруг неё.
"""

from __future__ import annotations

from typing import Any, Sequence

import pytest

from jarvis.core.agent import HIDDEN, Planner
from jarvis.core.bus import LocalEventBus
from jarvis.core.config import TaskProfile
from jarvis.core.contracts import ToolResult
from jarvis.core.errors import LLMError
from jarvis.core.llm import LLMRequest, LLMResponse, LLMService, ProfileRegistry, ToolCall
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Studio:
    """Набор инструментов: обратимые, необратимый и сбойный."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @tool(reversible=True)
    async def louder(self) -> ToolResult:
        """Сделать громче."""
        self.calls.append("louder")
        return ToolResult.success({"volume": 70})

    @tool(reversible=True)
    async def set_volume(self, level: int = 50) -> ToolResult:
        """Выставить громкость.

        :param level: громкость в процентах.
        """
        self.calls.append(f"set_volume:{level}")
        return ToolResult.success({"volume": level})

    @tool(reversible=True)
    async def now_playing(self) -> ToolResult:
        """Что играет сейчас."""
        self.calls.append("now_playing")
        return ToolResult.success({"track": "Bohemian Rhapsody"})

    @tool(reversible=False)
    async def send_message(self, text: str = "") -> ToolResult:
        """Отправить сообщение."""
        self.calls.append("send_message")
        return ToolResult.success({"sent": text})

    @tool(reversible=True)
    async def broken(self) -> ToolResult:
        """Инструмент, который всегда срывается."""
        self.calls.append("broken")
        return ToolResult.failure("устройство не отвечает")


class ScriptedProvider:
    """Провайдер, отвечающий по заранее написанному сценарию."""

    def __init__(self, script: Sequence[Any]) -> None:
        #: Каждый элемент — либо имя инструмента с аргументами, либо текст.
        self._script = list(script)
        self.seen: list[LLMRequest] = []

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return "scripted"

    @property
    def configured(self) -> bool:
        """Заглушка всегда готова."""
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Выдать следующую реплику сценария."""
        self.seen.append(request)
        if not self._script:
            return LLMResponse(text="сценарий кончился", usage={})
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, str):
            return LLMResponse(text=nxt, usage={})
        name, arguments = nxt
        return LLMResponse(
            tool_calls=(ToolCall(name=name.replace(".", "__"), arguments=arguments),),
            usage={},
        )

    async def aclose(self) -> None:
        """Закрывать нечего."""


def _service(provider: ScriptedProvider) -> LLMService:
    """Сервис поверх подставного провайдера."""
    profile = TaskProfile(task="plan", provider="scripted", model="stub")
    return LLMService(
        providers={"scripted": provider},
        profiles=ProfileRegistry({"plan": profile}, default_task="plan"),
    )


@pytest.fixture
def studio(events: LocalEventBus) -> tuple[Studio, ToolRegistry]:
    """Реестр с набором инструментов студии."""
    registry = ToolRegistry(events=events, default_timeout=1.0)
    skill = Studio()
    for item in collect_tools(skill, namespace="studio"):
        registry.register(item)
    return skill, registry


def _planner(registry: ToolRegistry, script: Sequence[Any], **kwargs: Any) -> Planner:
    """Цикл поверх сценария."""
    return Planner(llm=_service(ScriptedProvider(script)), registry=registry, **kwargs)


# --- обычный ход ------------------------------------------------------------


async def test_goal_of_two_steps_is_carried_out_in_order(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Модель видит результат шага и решает следующий по нему.

    Это и есть то, чего не умеет цепочка через союз «и»: там обе команды назвал
    человек, здесь вторую выбирают по результату первой.
    """
    skill, registry = studio
    planner = _planner(
        registry,
        [("studio.now_playing", {}), ("studio.louder", {}), "Играет Bohemian Rhapsody, сделал громче."],
    )

    outcome = await planner.run("узнай что играет и сделай погромче")

    assert outcome.ok
    assert skill.calls == ["now_playing", "louder"]
    assert [step.tool for step in outcome.steps] == ["studio.now_playing", "studio.louder"]
    assert outcome.answer == "Играет Bohemian Rhapsody, сделал громче."


async def test_result_of_each_step_reaches_the_model(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Результат шага уходит в переписку, иначе следующий шаг — догадка."""
    _, registry = studio
    provider = ScriptedProvider([("studio.now_playing", {}), "Готово."])
    planner = Planner(llm=_service(provider), registry=registry)

    await planner.run("что играет")

    # Второй запрос уже содержит рассказ о первом шаге.
    second = provider.seen[1]
    assert any("Bohemian Rhapsody" in message.content for message in second.messages)


# --- «план не даёт новых прав» ----------------------------------------------


async def test_irreversible_step_is_refused_not_performed(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Необратимый шаг цикл не выполняет, а возвращает целиком — вместе с
    аргументами.

    Главное правило всей затеи. Спрашивать о шаге — дело вызывающего; цикл лишь
    останавливается и говорит, обо что упёрся.
    """
    skill, registry = studio
    planner = _planner(registry, [("studio.send_message", {"text": "привет"}), "не дойдёт"])

    outcome = await planner.run("напиши маме")

    assert outcome.blocked is not None
    assert outcome.blocked.tool == "studio.send_message"
    # Аргументы сохраняются целиком: согласие исполняется ровно тем, о чём
    # спрашивали, иначе «отправить маме?» — «да» отправило бы неизвестно что.
    assert outcome.blocked.arguments == {"text": "привет"}
    assert not outcome.ok
    assert "send_message" not in skill.calls


async def test_steps_before_the_block_are_reported(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Половина работы уже сделана, и владелец должен знать какая."""
    skill, registry = studio
    planner = _planner(
        registry, [("studio.now_playing", {}), ("studio.send_message", {"text": "x"})]
    )

    outcome = await planner.run("узнай что играет и напиши маме")

    assert skill.calls == ["now_playing"]
    assert [step.tool for step in outcome.steps] == ["studio.now_playing"]
    assert outcome.blocked is not None and outcome.blocked.tool == "studio.send_message"


async def test_plan_and_chat_are_hidden_from_the_loop(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Цикл не строит план внутри плана и не «выполняет» цель разговором.

    Вложенный план умножает расход, ничего не добавляя. А свободный разговор
    отвечает всегда и на что угодно — с ним любая недостижимая цель выглядела бы
    успешной.
    """
    assert "core.plan" in HIDDEN
    assert "core.chat" in HIDDEN

    _, registry = studio
    provider = ScriptedProvider(["всё"])
    await Planner(llm=_service(provider), registry=registry).run("что-нибудь")

    offered = {
        schema["function"]["name"] for schema in (provider.seen[0].tools or [])
    }
    assert "core__plan" not in offered
    assert "core__chat" not in offered


# --- пределы ----------------------------------------------------------------


async def test_failed_step_stops_the_plan(studio: tuple[Studio, ToolRegistry]) -> None:
    """Сорвался шаг — дальше не идём.

    То же правило, что в цепочке через союз: человек подразумевает порядок, а не
    независимые поручения. «Переключись и включи трек» после неудачного
    переключения включило бы трек неизвестно где.
    """
    skill, registry = studio
    planner = _planner(registry, [("studio.broken", {}), ("studio.louder", {}), "готово"])

    outcome = await planner.run("почини и сделай громче")

    assert not outcome.ok
    assert "не удался" in outcome.stopped
    assert skill.calls == ["broken"]


async def test_repeated_step_stops_the_plan(studio: tuple[Studio, ToolRegistry]) -> None:
    """Повтор того же шага означает, что модель ходит по кругу.

    Ходить по нему она будет за наши деньги: каждый виток тащит каталог.
    """
    skill, registry = studio
    planner = _planner(
        registry, [("studio.louder", {}), ("studio.louder", {}), ("studio.louder", {})]
    )

    outcome = await planner.run("сделай громче")

    assert outcome.stopped == "шаг повторился"
    assert skill.calls == ["louder"]


async def test_step_limit_is_enforced(studio: tuple[Studio, ToolRegistry]) -> None:
    """Дальше предела цикл не идёт, даже если модель готова продолжать."""
    skill, registry = studio
    # Аргумент каждый раз новый, иначе цикл остановится раньше — на повторе.
    script = [("studio.set_volume", {"level": number}) for number in range(10)]
    planner = _planner(registry, script, steps=3)

    outcome = await planner.run("крути громкость")

    assert outcome.stopped == "исчерпан предел шагов"
    assert len(outcome.steps) == 3
    assert len(skill.calls) == 3


async def test_model_failure_does_not_crash_the_plan(
    studio: tuple[Studio, ToolRegistry]
) -> None:
    """Оборванная сеть посреди плана — это остановка, а не исключение наружу."""
    _, registry = studio
    planner = _planner(registry, [LLMError("сеть отвалилась")])

    outcome = await planner.run("сделай что-нибудь")

    assert not outcome.ok
    assert "модель не ответила" in outcome.stopped


async def test_unknown_tool_stops_the_plan(studio: tuple[Studio, ToolRegistry]) -> None:
    """Выдуманный моделью инструмент останавливает цикл, а не роняет его."""
    _, registry = studio
    planner = _planner(registry, [("studio.teleport", {})])

    outcome = await planner.run("телепортируй меня")

    assert outcome.stopped == "выбран несуществующий инструмент"


# --- выжимка результата -----------------------------------------------------


async def test_long_result_is_cut_before_it_reaches_the_model(
    events: LocalEventBus
) -> None:
    """Целиком результат не показываем: выдача поиска съест окно за два шага."""
    from jarvis.core.agent import RESULT_LIMIT

    class Wordy:
        @tool(reversible=True)
        async def dump(self) -> ToolResult:
            """Очень длинный ответ."""
            return ToolResult.success("я" * 5000)

    registry = ToolRegistry(events=events, default_timeout=1.0)
    for item in collect_tools(Wordy(), namespace="wordy"):
        registry.register(item)

    provider = ScriptedProvider([("wordy.dump", {}), "всё"])
    outcome = await Planner(llm=_service(provider), registry=registry).run("вывали")

    assert len(outcome.steps[0].summary) <= RESULT_LIMIT
