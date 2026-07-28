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
