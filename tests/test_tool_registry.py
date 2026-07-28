"""Реестр инструментов: вывод схемы, валидация, вызов, изоляция сбоев."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.core.contracts import ToolResult
from jarvis.core.errors import ToolNotFound
from jarvis.core.tools import ToolRegistry, collect_tools, tool


class Demo:
    """Носитель инструментов для тестов."""

    @tool(phrases=["включи свет"])
    async def set_light(self, zone: str, brightness: int = 100, on: bool = True) -> ToolResult:
        """Включить свет в зоне.

        :param zone: зона освещения.
        :param brightness: яркость в процентах.
        """
        return ToolResult.success({"zone": zone, "brightness": brightness, "on": on})

    @tool()
    async def boom(self) -> str:
        """Инструмент, который всегда падает."""
        raise RuntimeError("внутренняя поломка")

    @tool(timeout=0.05)
    async def slow(self) -> str:
        """Инструмент, который не успевает ответить."""
        await asyncio.sleep(5)
        return "поздно"

    def not_a_tool(self) -> str:
        """Обычный метод без декоратора."""
        return "невидим"


def test_schema_derived_from_signature_and_docstring() -> None:
    """JSON Schema строится из аннотаций и строк `:param:` — без дублирования."""
    schema = next(t for t in collect_tools(Demo(), namespace="demo") if t.name == "demo.set_light")
    properties = schema.spec.parameters["properties"]

    assert schema.spec.parameters["required"] == ["zone"]
    assert properties["zone"]["type"] == "string"
    assert properties["zone"]["description"] == "зона освещения."
    assert properties["brightness"]["type"] == "integer"
    assert properties["brightness"]["default"] == 100
    assert properties["on"]["type"] == "boolean"
    assert schema.spec.description == "Включить свет в зоне."


def test_only_decorated_methods_become_tools() -> None:
    """Метод без `@tool` в каталог не попадает."""
    names = {t.name for t in collect_tools(Demo(), namespace="demo")}
    assert names == {"demo.set_light", "demo.boom", "demo.slow"}


def test_function_schema_is_llm_ready() -> None:
    """Каталог отдаёт схемы в формате function-calling."""
    registry = ToolRegistry()
    for item in collect_tools(Demo(), namespace="demo"):
        registry.register(item)

    schemas = registry.catalog().function_schemas()
    light = next(s for s in schemas if s["function"]["name"] == "demo__set_light")

    assert light["type"] == "function"
    assert "zone" in light["function"]["parameters"]["properties"]
    assert registry.resolve_function_name("demo__set_light") == "demo.set_light"


async def test_invoke_coerces_arguments(registry: ToolRegistry) -> None:
    """Строки от LLM и распознавания приводятся к типам схемы."""
    for item in collect_tools(Demo(), namespace="demo"):
        registry.register(item)

    result = await registry.invoke("demo.set_light", {"zone": "studio", "brightness": "60"})

    assert result.ok
    assert result.value["brightness"] == 60


async def test_invoke_rejects_bad_arguments(registry: ToolRegistry) -> None:
    """Нехватка обязательного параметра — ошибка, а не исключение наружу."""
    for item in collect_tools(Demo(), namespace="demo"):
        registry.register(item)

    result = await registry.invoke("demo.set_light", {})

    assert not result.ok
    assert "zone" in (result.error or "")


async def test_tool_failure_is_isolated(registry: ToolRegistry) -> None:
    """Исключение внутри инструмента возвращается как результат, а не всплывает."""
    for item in collect_tools(Demo(), namespace="demo"):
        registry.register(item)

    result = await registry.invoke("demo.boom")

    assert not result.ok
    assert "внутренняя поломка" in (result.error or "")


async def test_tool_timeout(registry: ToolRegistry) -> None:
    """Зависший инструмент прерывается по таймауту."""
    for item in collect_tools(Demo(), namespace="demo"):
        registry.register(item)

    result = await registry.invoke("demo.slow")

    assert not result.ok
    assert "не ответил" in (result.error or "")


async def test_unknown_tool_raises(registry: ToolRegistry) -> None:
    """Обращение к несуществующему инструменту — явная ошибка."""
    with pytest.raises(ToolNotFound):
        await registry.invoke("demo.nope")


def test_registration_revoke_removes_tool(registry: ToolRegistry) -> None:
    """Отзыв регистрации убирает инструмент из каталога."""
    items = collect_tools(Demo(), namespace="demo")
    registrations = [registry.register(item) for item in items]

    assert len(registry) == 3
    for registration in registrations:
        registration.revoke()
    assert len(registry) == 0


# --- экономия токенов -------------------------------------------------------


def test_service_tools_hidden_from_model() -> None:
    """Служебные инструменты не уходят в модель.

    Каталог отправляется на каждой неузнанной фразе, поэтому каждый лишний
    инструмент — это входные токены в каждом запросе. Голосом перезагрузку
    скилла никто не просит, а по имени она по-прежнему доступна.
    """

    class Admin:
        """Скилл с обычным и служебным инструментом."""

        @tool(phrases=["включи свет"])
        async def on(self) -> ToolResult:
            """Включить свет."""
            return ToolResult.success(True)

        @tool(routable=False)
        async def reload(self, skill: str) -> ToolResult:
            """Перезагрузить скилл.

            :param skill: имя скилла.
            """
            return ToolResult.success(True)

    registry = ToolRegistry()
    for item in collect_tools(Admin(), namespace="admin"):
        registry.register(item)

    catalog = registry.catalog()
    names = {schema["function"]["name"] for schema in catalog.function_schemas()}

    assert "admin__on" in names
    assert "admin__reload" not in names
    # Но вызвать его по имени всё ещё можно.
    assert "admin.reload" in {spec.name for spec in catalog.specs}


def test_spending_accumulates_by_task() -> None:
    """Расход считается по задачам: видно, куда именно уходят токены."""
    from jarvis.core.llm.service import Spending

    spending = Spending()
    spending.add("intent", {"prompt_tokens": 2400, "completion_tokens": 20, "cost": 0.00025})
    spending.add("intent", {"prompt_tokens": 2400, "completion_tokens": 20, "cost": 0.00025})
    spending.add("dialog", {"prompt_tokens": 300, "completion_tokens": 100})

    assert spending.calls == 3
    assert spending.total_tokens == 5240
    assert spending.by_task["intent"] == 4840
    assert round(spending.cost, 5) == 0.0005
    assert "intent" in spending.summary()


def test_spending_survives_missing_usage() -> None:
    """Не всякий провайдер сообщает расход — счётчик не должен падать."""
    from jarvis.core.llm.service import Spending

    spending = Spending()
    spending.add("dialog", {})

    assert spending.calls == 1
    assert spending.total_tokens == 0
