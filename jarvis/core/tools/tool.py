"""Инструмент и декоратор `@tool`.

Инструмент — это команда с ответом: вызвал по имени, получил `ToolResult`.
Скилл объявляет инструменты декоратором прямо на методах:

    class ESP32Skill(Skill):
        @tool(phrases=["какая температура", "температура в студии"])
        async def get_temperature(self, sensor: str = "studio") -> float:
            \"\"\"Вернуть температуру с датчика в градусах Цельсия.

            :param sensor: идентификатор датчика.
            \"\"\"

Отсюда автоматически получаются и имя (``esp32.get_temperature``), и описание,
и JSON Schema для function-calling, и фразы для быстрой маршрутизации без LLM.
Ни роутер, ни LLM не знают, что у скилла внутри — только этот каталог.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .schema import build_schema, parse_docstring

_MARKER = "__jarvis_tool__"


@dataclass(frozen=True, slots=True, kw_only=True)
class _ToolMarker:
    """Метка, которую декоратор вешает на метод до создания скилла."""

    name: str | None
    description: str | None
    phrases: tuple[str, ...]
    timeout: float | None
    routable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """Публичное описание инструмента — всё, что видят роутер и LLM."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    phrases: tuple[str, ...] = ()
    skill: str = ""
    #: Показывать ли инструмент языковой модели при разборе команды.
    #: Служебные операции голосом не вызывают, а место в каждом запросе они
    #: занимают — каталог уезжает в модель целиком и на каждой фразе.
    routable: bool = True

    def as_function_schema(self) -> dict[str, Any]:
        """Представление для function-calling API языковой модели."""
        return {
            "type": "function",
            "function": {
                "name": self.name.replace(".", "__"),
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Tool:
    """Описание плюс исполняемый обработчик."""

    spec: ToolSpec
    handler: Callable[..., Awaitable[Any]]
    timeout: float | None = None

    @property
    def name(self) -> str:
        """Полное имя инструмента."""
        return self.spec.name


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    phrases: Sequence[str] = (),
    timeout: float | None = None,
    routable: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Пометить метод скилла как инструмент.

    :param name: короткое имя; по умолчанию — имя метода.
    :param description: описание; по умолчанию — первая строка докстринга.
    :param phrases: фразы, по которым команду можно узнать без обращения к LLM.
    :param timeout: свой предел ожидания вместо общего из конфига.
    :param routable: показывать ли инструмент языковой модели. Служебные
        операции (перезагрузка скилла, смена модели) голосом не вызывают, а
        каталог уходит в модель на каждой неузнанной фразе — и это платный
        вход. Такие инструменты остаются доступны по точному имени и фразам.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            func,
            _MARKER,
            _ToolMarker(
                name=name,
                description=description,
                phrases=tuple(phrases),
                timeout=timeout,
                routable=routable,
            ),
        )
        return func

    return decorator


def is_tool(obj: Any) -> bool:
    """Помечен ли объект декоратором `@tool`."""
    return hasattr(obj, _MARKER)


def collect_tools(instance: Any, *, namespace: str) -> list[Tool]:
    """Собрать инструменты, объявленные на экземпляре скилла.

    :param instance: объект скилла.
    :param namespace: префикс имён, обычно имя скилла.
    """
    tools: list[Tool] = []
    for attr_name in dir(type(instance)):
        if attr_name.startswith("__"):
            continue
        attribute = getattr(type(instance), attr_name, None)
        if attribute is None or not is_tool(attribute):
            continue

        marker: _ToolMarker = getattr(attribute, _MARKER)
        bound = getattr(instance, attr_name)
        summary, _ = parse_docstring(attribute)
        short_name = marker.name or attr_name

        tools.append(
            Tool(
                spec=ToolSpec(
                    name=f"{namespace}.{short_name}",
                    description=marker.description or summary or short_name,
                    parameters=build_schema(attribute),
                    phrases=marker.phrases,
                    skill=namespace,
                    routable=marker.routable,
                ),
                handler=bound,
                timeout=marker.timeout,
            )
        )
    return tools


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCatalog:
    """Снимок доступных инструментов — то, что передаётся LLM и роутеру."""

    specs: tuple[ToolSpec, ...] = field(default_factory=tuple)

    def function_schemas(self) -> list[dict[str, Any]]:
        """Схемы инструментов для function-calling.

        Служебные (``routable=False``) не попадают: каталог уходит в модель на
        каждой неузнанной фразе, и каждый лишний инструмент — это входные
        токены в каждом запросе до конца жизни проекта.
        """
        return [spec.as_function_schema() for spec in self.specs if spec.routable]

    def describe(self) -> str:
        """Компактное текстовое описание каталога — для промпта."""
        lines = []
        for spec in self.specs:
            params = ", ".join(spec.parameters.get("properties", {})) or "без параметров"
            lines.append(f"- {spec.name}({params}) — {spec.description}")
        return "\n".join(lines)
