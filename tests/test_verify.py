"""Проверь себя: угаданное моделью сверяется с тем, что на экране.

Стенд `tools/agent_bench` 25.09.2026: из 20 просьб без своего скилла **9
кончились ложным успехом** — ассистент говорил «сделано», а на машине было
другое. Диспетчер задач вместо диспетчера устройств, главная Википедии вместо
статьи, «репозиторий открыт» при открытом поиске.
"""

from __future__ import annotations

from typing import Any, Sequence

from jarvis.core.agent import Planner
from jarvis.core.config import TaskProfile
from jarvis.core.contracts import ToolResult
from jarvis.core.llm import LLMRequest, LLMResponse, LLMService, ProfileRegistry, ToolCall
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from jarvis.core.verify import Checker, describe, parse_verdict

# --- разбор ответа проверки ---------------------------------------------------


def test_yes_and_no_are_read() -> None:
    assert parse_verdict("ДА").ok
    assert parse_verdict("YES").ok
    verdict = parse_verdict("НЕТ — открыт диспетчер задач, а не устройств")
    assert not verdict.ok and verdict.reason == "открыт диспетчер задач, а не устройств"


def test_garbage_is_trusted() -> None:
    """Проверка — добавка, а не новая причина отказать: мусор судьи — «да»."""
    assert parse_verdict("").ok
    assert parse_verdict("Думаю, что всё в порядке").ok


def test_a_bare_no_still_has_a_reason() -> None:
    """Вслух говорится причина — пустой её быть не может."""
    assert parse_verdict("НЕТ").reason


def test_the_snapshot_is_short_and_readable() -> None:
    seen = describe({
        "windows.observe": {"active": "Диспетчер задач", "windows": ["Калькулятор"]},
        "browser.page_target": {"tabId": 7, "title": "YouTube", "url": "https://youtube.com/"},
        "empty": {},
    })
    assert "Диспетчер задач" in seen and "youtube.com" in seen
    assert "tabId" not in seen, "служебное модели ни к чему"
    assert "empty" not in seen


# --- подставные глаза и модель ----------------------------------------------


class Screen:
    """Экран, который показывает то, что ему велели."""

    def __init__(self, *titles: str) -> None:
        self.titles = list(titles)

    @tool(routable=False, reversible=True)
    async def observe(self) -> ToolResult:
        return ToolResult.success({"active": self.titles[0] if self.titles else ""})


class Desk:
    @tool(reversible=True, shows=True)
    async def launch(self, program: str = "") -> ToolResult:
        """Запустить программу."""
        return ToolResult.success(f"запущено {program}")

    @tool(reversible=True)
    async def weather(self) -> ToolResult:
        """Погода: результат — сам ответ, на экран смотреть незачем."""
        return ToolResult.success("тепло")


class Scripted:
    """Модель по сценарию: каждое следующее обращение — следующая реплика."""

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.asked: list[LLMRequest] = []

    name = "scripted"
    configured = True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.asked.append(request)
        step = self.script.pop(0) if self.script else "сценарий кончился"
        if isinstance(step, str):
            return LLMResponse(text=step, usage={})
        tool_name, arguments = step
        return LLMResponse(
            tool_calls=(ToolCall(name=tool_name.replace(".", "__"), arguments=arguments),), usage={}
        )

    async def aclose(self) -> None:
        pass


def _world(script: Sequence[Any], *titles: str) -> tuple[Planner, Scripted, Screen]:
    registry = ToolRegistry(default_timeout=1.0)
    screen = Screen(*titles)
    for skill, namespace in ((screen, "windows"), (Desk(), "desk")):
        for item in collect_tools(skill, namespace=namespace):
            registry.register(item)
    provider = Scripted(script)
    llm = LLMService(
        providers={"scripted": provider},  # type: ignore[dict-item]
        profiles=ProfileRegistry(
            {"plan": TaskProfile(task="plan", provider="scripted", model="stub")},
            default_task="plan",
        ),
    )
    checker = Checker(llm=llm, registry=registry, observe={"windows.observe": {}})
    return Planner(llm=llm, registry=registry, checker=checker), provider, screen


# --- план проверяет себя -----------------------------------------------------


async def test_a_plan_that_lies_is_caught() -> None:
    """Живой случай: «репозиторий открыт», а открыт поиск. Не верим на слово."""
    planner, _, _ = _world(
        [("desk.launch", {"program": "диспетчер устройств"}),
         "Готово, диспетчер устройств открыт.",
         "НЕТ — открыт диспетчер задач",       # проверка
         "Не вышло: открылся диспетчер задач.",  # вторая попытка — честно
         "ДА"],                                 # честный отказ проверку проходит
        "Диспетчер задач",
    )
    outcome = await planner.run("открой диспетчер устройств")
    assert "Не вышло" in outcome.answer or "не то" in outcome.stopped


async def test_a_plan_that_did_it_passes() -> None:
    planner, provider, _ = _world(
        [("desk.launch", {"program": "калькулятор"}), "Калькулятор открыт.", "ДА"],
        "Калькулятор",
    )
    outcome = await planner.run("открой калькулятор")
    assert outcome.ok and outcome.answer == "Калькулятор открыт."
    assert len(provider.asked) == 3, "шаг, ответ и одна проверка"


async def test_the_plan_sees_the_screen_after_a_visible_step() -> None:
    """Инструмент говорит «запущено», а смотреть надо, что на экране."""
    planner, provider, _ = _world(
        [("desk.launch", {"program": "x"}), "Готово.", "ДА"], "Окно X",
    )
    await planner.run("открой x")
    told = provider.asked[1].messages[-1].content
    assert "На экране сейчас" in told and "Окно X" in told


async def test_nothing_visible_is_not_checked() -> None:
    """Погоду на экране не проверишь — и платить за проверку незачем."""
    planner, provider, _ = _world([("desk.weather", {}), "Тепло."], "Что угодно")
    outcome = await planner.run("какая погода")
    assert outcome.ok and len(provider.asked) == 2, "без лишнего обращения к модели"


async def test_without_eyes_the_plan_is_as_before() -> None:
    """Нечем смотреть — всё как раньше, без проверки."""
    registry = ToolRegistry(default_timeout=1.0)
    for item in collect_tools(Desk(), namespace="desk"):
        registry.register(item)
    provider = Scripted([("desk.launch", {}), "Готово."])
    llm = LLMService(
        providers={"scripted": provider},  # type: ignore[dict-item]
        profiles=ProfileRegistry(
            {"plan": TaskProfile(task="plan", provider="scripted", model="stub")},
            default_task="plan",
        ),
    )
    checker = Checker(llm=llm, registry=registry, observe={"windows.observe": {}})
    outcome = await Planner(llm=llm, registry=registry, checker=checker).run("открой")
    assert outcome.ok and len(provider.asked) == 2


# --- диспетчер: угаданное моделью проверяется, неподтверждённое не учится ----


class _Guess:
    """Резолвер, который «угадывает» как модель."""

    name = "llm"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def resolve(self, utterance: Any) -> Any:
        from jarvis.core.contracts import Intent

        return Intent(tool=self.tool_name, confidence=0.9, resolver="llm")


class _Learner:
    def __init__(self) -> None:
        self.learned: list[str] = []

    async def remember(self, text: str, intent: Any) -> str:
        self.learned.append(text)
        return text


class _Plan:
    def __init__(self) -> None:
        self.goals: list[str] = []

    @tool(name="plan", reversible=False)
    async def plan(self, goal: str, language: str = "ru") -> ToolResult:
        """План в миниатюре: запоминает, с какой целью его позвали."""
        self.goals.append(goal)
        return ToolResult.success("сделал по-другому", speech="Сделал по-другому.")


def _dispatcher(verdict: str, *titles: str) -> tuple[Any, _Learner, _Plan]:
    from jarvis.core.router import Dispatcher, Router

    registry = ToolRegistry(default_timeout=1.0)
    plan = _Plan()
    for skill, namespace in ((Screen(*titles), "windows"), (Desk(), "desk"), (plan, "core")):
        for item in collect_tools(skill, namespace=namespace):
            registry.register(item)
    llm = LLMService(
        providers={"scripted": Scripted([verdict])},  # type: ignore[dict-item]
        profiles=ProfileRegistry(
            {"plan": TaskProfile(task="plan", provider="scripted", model="stub")},
            default_task="plan",
        ),
    )
    learner = _Learner()
    dispatcher = Dispatcher(
        router=Router([_Guess("desk.launch")]),
        registry=registry,
        learner=learner,  # type: ignore[arg-type]
        checker=Checker(llm=llm, registry=registry, observe={"windows.observe": {}}),
    )
    return dispatcher, learner, plan


async def test_a_guess_that_missed_goes_to_the_plan_and_is_not_learned(monkeypatch) -> None:
    """Главное: ложный успех больше не выучивается и не повторяется вечно.

    До 25.09.2026 выучивался любой «ок» инструмента — и «статья про Тверь» →
    главная Википедии закрепилась бы как верный разбор.
    """
    import jarvis.core.verify as verify

    monkeypatch.setattr(verify, "SETTLE_S", 0.0)
    from jarvis.core.contracts import Utterance

    dispatcher, learner, plan = _dispatcher("НЕТ — открыт диспетчер задач", "Диспетчер задач")
    result = await dispatcher.handle(Utterance(text="открой диспетчер устройств", named=True))

    assert result.speech_for("ru") == "Сделал по-другому.", "работу доделывал план"
    assert plan.goals and "диспетчер задач" in plan.goals[0], "план знает, что не вышло"
    assert learner.learned == [], "неподтверждённое не выучивается"


async def test_a_confirmed_guess_is_learned(monkeypatch) -> None:
    import jarvis.core.verify as verify

    monkeypatch.setattr(verify, "SETTLE_S", 0.0)
    from jarvis.core.contracts import Utterance

    dispatcher, learner, plan = _dispatcher("ДА", "Калькулятор")
    result = await dispatcher.handle(Utterance(text="открой калькулятор", named=True))

    assert result.ok and plan.goals == []
    assert learner.learned == ["открой калькулятор"]
