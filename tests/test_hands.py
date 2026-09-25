"""Точные руки: нажать по названию, и не нажать сам то, что необратимо.

Стенд 25.09.2026: план тянулся к мыши «в текущей точке указателя», то есть
вслепую. Теперь у него руки по названию — дерево доступности Windows и кнопки
страницы. Живой замер на калькуляторе: «8 + 1 =» нажато по именам, на табло 9.

Но руки плана — это и риск: правило «план не даёт новых прав» требует, чтобы
необратимое план сам не делал. Решает название кнопки (`jarvis.core.risk`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from jarvis.core.agent import Planner
from jarvis.core.config import TaskProfile
from jarvis.core.contracts import ToolResult
from jarvis.core.llm import LLMRequest, LLMResponse, LLMService, ProfileRegistry, ToolCall
from jarvis.core.risk import risky
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from jarvis.core.tools.tool import ToolSpec

# --- что считается необратимым ----------------------------------------------


def test_ordinary_buttons_are_pressed() -> None:
    for label in ("Семь", "Подписки", "Далее", "Sign in", "Border", "Найти"):
        assert not risky(label), label


def test_irreversible_buttons_are_not() -> None:
    for label in ("Удалить", "Отправить", "Купить за 499 ₽", "Оплатить", "Подписаться",
                  "Sign out", "Order now", "Стереть всё", "Выйти из аккаунта"):
        assert risky(label), label


def test_the_word_must_start_there() -> None:
    """«order» не должен находиться в «border», «post» — в «compost»."""
    assert not risky("Border radius")
    assert not risky("compost")


# --- решает значение, а не инструмент ----------------------------------------


def _spec(**kwargs: object) -> ToolSpec:
    return ToolSpec(name="page.press", description="", parameters={}, **kwargs)  # type: ignore[arg-type]


def test_a_named_press_is_judged_by_its_label() -> None:
    spec = _spec(reversible=False, risk_arg="control")
    assert spec.unattended_with({"control": "Подписки"})
    assert not spec.unattended_with({"control": "Купить"})
    assert not spec.unattended_with({}), "без названия — не угадываем"


def test_without_a_risk_argument_nothing_changes() -> None:
    assert not _spec(reversible=False).unattended_with({"control": "Подписки"})
    assert _spec(reversible=True).unattended_with({})


# --- план видит руки, которых не видит разбор команд -------------------------


class Page:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    @tool(routable=False, reversible=False, agent=True, risk_arg="control")
    async def press(self, control: str) -> ToolResult:
        """Нажать кнопку на странице."""
        self.pressed.append(control)
        return ToolResult.success(control)

    @tool(routable=False, reversible=True)
    async def service(self) -> ToolResult:
        """Служебное: не видно ни разбору, ни плану."""
        return ToolResult.success(None)


def test_hands_go_to_the_plan_but_not_to_the_intent_catalog() -> None:
    registry = ToolRegistry()
    for item in collect_tools(Page(), namespace="page"):
        registry.register(item)
    names = lambda schemas: {s["function"]["name"] for s in schemas}  # noqa: E731
    assert names(registry.catalog().function_schemas()) == set(), "разбору команд — не за чем платить"
    assert names(registry.catalog().function_schemas(agent=True)) == {"page__press"}


class Scripted:
    name = "scripted"
    configured = True

    def __init__(self, script: list[object]) -> None:
        self.script = script

    async def complete(self, request: LLMRequest) -> LLMResponse:
        step = self.script.pop(0) if self.script else "всё"
        if isinstance(step, str):
            return LLMResponse(text=step, usage={})
        tool_name, arguments = step  # type: ignore[misc]
        return LLMResponse(
            tool_calls=(ToolCall(name=tool_name.replace(".", "__"), arguments=arguments),), usage={}
        )

    async def aclose(self) -> None:
        pass


def _planner(script: list[object]) -> tuple[Planner, Page]:
    registry = ToolRegistry(default_timeout=1.0)
    page = Page()
    for item in collect_tools(page, namespace="page"):
        registry.register(item)
    llm = LLMService(
        providers={"scripted": Scripted(script)},  # type: ignore[dict-item]
        profiles=ProfileRegistry(
            {"plan": TaskProfile(task="plan", provider="scripted", model="stub")}, default_task="plan"
        ),
    )
    return Planner(llm=llm, registry=registry), page


async def test_the_plan_presses_a_harmless_button_itself() -> None:
    planner, page = _planner([("page.press", {"control": "Подписки"}), "Открыл подписки."])
    outcome = await planner.run("открой мои подписки")
    assert outcome.ok and page.pressed == ["Подписки"]


async def test_the_plan_asks_before_buying() -> None:
    """«План не даёт новых прав»: «Купить» — только с разрешения владельца."""
    planner, page = _planner([("page.press", {"control": "Купить"})])
    outcome = await planner.run("купи эту игру")
    assert page.pressed == [], "сам не нажал"
    assert outcome.blocked is not None and outcome.blocked.arguments == {"control": "Купить"}


# --- окна, куда руки не лезут ------------------------------------------------


def _hands() -> object:
    path = Path(__file__).resolve().parent.parent / "skills" / "windows" / "hands.py"
    spec = importlib.util.spec_from_file_location("windows_hands_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_passwords_and_money_are_off_limits() -> None:
    hands = _hands()
    assert hands.forbidden("KeePassXC — Passwords.kdbx")
    assert hands.forbidden("Т-Банк — Переводы")
    assert not hands.forbidden("Калькулятор")
