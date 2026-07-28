"""Вывод JSON Schema из аннотаций типов и докстринга.

Описание инструмента не дублируется: схема для function-calling строится из
того, что уже написано в коде — типов параметров и докстринга. Добавил
параметр в метод — он сам появился в схеме, которую видит LLM.
"""

from __future__ import annotations

import enum
import inspect
import re
import types
import typing
from typing import Any, Callable, Mapping, get_args, get_origin, get_type_hints

from jarvis.core.errors import ToolInvalidArguments

# ":param имя: описание" — тот же стиль, что и во всём проекте.
_PARAM_DOC = re.compile(r"^\s*:param\s+(?P<name>\w+)\s*:\s*(?P<text>.+)$", re.MULTILINE)

_PRIMITIVES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def parse_docstring(func: Callable[..., Any]) -> tuple[str, dict[str, str]]:
    """Разобрать докстринг на общее описание и описания параметров."""
    doc = inspect.getdoc(func) or ""
    params = {m.group("name"): m.group("text").strip() for m in _PARAM_DOC.finditer(doc)}
    summary = doc.split("\n\n", 1)[0].strip()
    summary = _PARAM_DOC.sub("", summary).strip()
    return summary, params


def _type_schema(annotation: Any) -> dict[str, Any]:
    """Перевести аннотацию типа в фрагмент JSON Schema."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    if annotation in _PRIMITIVES:
        return {"type": _PRIMITIVES[annotation]}

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {"type": "string", "enum": [item.value for item in annotation]}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is typing.Literal:
        values = list(args)
        kind = _PRIMITIVES.get(type(values[0]), "string") if values else "string"
        return {"type": kind, "enum": values}

    # Optional[X] и X | None
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        schema = _type_schema(inner[0]) if len(inner) == 1 else {"type": "string"}
        if len(args) != len(inner):
            schema["nullable"] = True
        return schema

    if origin in (list, tuple, set, frozenset):
        item = _type_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}

    if origin is dict:
        return {"type": "object", "additionalProperties": True}

    return {"type": "string"}


def build_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Построить JSON Schema параметров функции или метода."""
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # незакрытые forward-ссылки не должны ронять загрузку скилла
        hints = {}
    _, param_docs = parse_docstring(func)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue

        schema = _type_schema(hints.get(name, parameter.annotation))
        if name in param_docs:
            schema["description"] = param_docs[name]
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        else:
            schema["default"] = parameter.default
        properties[name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _coerce(value: Any, schema: Mapping[str, Any], *, field: str) -> Any:
    """Мягко привести значение к типу из схемы.

    LLM и распознавание речи присылают почти всё строками — «21» вместо 21.
    Приведение делает вызовы устойчивее, но не молчит при явной ерунде.
    """
    kind = schema.get("type", "string")
    if value is None:
        return None
    try:
        if kind == "string":
            return value if isinstance(value, str) else str(value)
        if kind == "integer":
            return value if isinstance(value, int) and not isinstance(value, bool) else int(value)
        if kind == "number":
            return value if isinstance(value, (int, float)) else float(value)
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "да", "вкл", "on")
        if kind == "array":
            return list(value) if isinstance(value, (list, tuple, set)) else [value]
    except (TypeError, ValueError) as exc:
        raise ToolInvalidArguments(
            f"Параметр {field!r}: ожидался тип {kind}, получено {value!r}"
        ) from exc
    return value


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Проверить и привести аргументы к схеме.

    :param schema: JSON Schema, полученная из `build_schema`.
    :param arguments: то, что прислал резолвер, LLM или другой скилл.
    """
    properties: Mapping[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", ()))

    unknown = set(arguments) - set(properties)
    if unknown and not schema.get("additionalProperties", False):
        raise ToolInvalidArguments(
            f"Неизвестные параметры: {', '.join(sorted(unknown))}. "
            f"Допустимые: {', '.join(sorted(properties)) or '(нет)'}"
        )

    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolInvalidArguments(f"Не хватает обязательных параметров: {', '.join(missing)}")

    result: dict[str, Any] = {}
    for name, value in arguments.items():
        field_schema = properties.get(name, {})
        coerced = _coerce(value, field_schema, field=name)
        allowed = field_schema.get("enum")
        if allowed is not None and coerced not in allowed:
            raise ToolInvalidArguments(
                f"Параметр {name!r}: допустимо {allowed}, получено {coerced!r}"
            )
        result[name] = coerced
    return result
