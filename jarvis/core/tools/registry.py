"""Реестр инструментов — второй канал связи, рядом с шиной событий.

Событие говорит «что-то случилось» и не возвращает ответа. Но «какая
температура» — это вопрос, на который нужен ответ здесь и сейчас. Пропускать
такое через pub/sub значит городить correlation-id, события-ответы и таймауты
в каждом скилле.

Поэтому команды идут через реестр: вызов по строковому имени, `await`, готовый
`ToolResult`. Развязка та же, что у событий — скилл ссылается на чужой
инструмент по имени и никогда его не импортирует.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from jarvis.core.bus import EventBus
from jarvis.core.contracts import ToolCompleted, ToolInvoked, ToolResult
from jarvis.core.errors import ToolInvalidArguments, ToolNotFound
from jarvis.core.tools.schema import validate_arguments
from jarvis.core.tools.tool import Tool, ToolCatalog, ToolSpec

logger = logging.getLogger(__name__)


class Registration:
    """Токен регистрации: позволяет снять инструмент при выгрузке скилла."""

    __slots__ = ("_registry", "_name", "_active")

    def __init__(self, registry: "ToolRegistry", name: str) -> None:
        self._registry = registry
        self._name = name
        self._active = True

    @property
    def name(self) -> str:
        """Имя зарегистрированного инструмента."""
        return self._name

    def revoke(self) -> None:
        """Снять инструмент с регистрации; повторные вызовы безопасны."""
        if self._active:
            self._registry.unregister(self._name)
            self._active = False


class ToolRegistry:
    """Хранит инструменты и исполняет их по имени."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._events = events
        self._default_timeout = default_timeout

    # --- регистрация -------------------------------------------------------

    def register(self, tool: Tool, *, replace: bool = False) -> Registration:
        """Добавить инструмент в реестр."""
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"Инструмент {tool.name!r} уже зарегистрирован "
                f"(скилл {self._tools[tool.name].spec.skill!r})"
            )
        self._tools[tool.name] = tool
        logger.debug("Инструмент зарегистрирован: %s", tool.name)
        return Registration(self, tool.name)

    def unregister(self, name: str) -> None:
        """Убрать инструмент из реестра."""
        if self._tools.pop(name, None) is not None:
            logger.debug("Инструмент снят: %s", name)

    # --- чтение ------------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        """Найти инструмент по имени."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Есть ли такой инструмент."""
        return name in self._tools

    def __len__(self) -> int:
        """Сколько инструментов зарегистрировано."""
        return len(self._tools)

    def catalog(self, *, skill: str | None = None) -> ToolCatalog:
        """Снимок каталога, при необходимости — только по одному скиллу."""
        specs = tuple(
            tool.spec
            for tool in sorted(self._tools.values(), key=lambda t: t.name)
            if skill is None or tool.spec.skill == skill
        )
        return ToolCatalog(specs=specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Описания всех инструментов."""
        return self.catalog().specs

    def phrase_index(self) -> dict[str, str]:
        """Отображение «фраза -> имя инструмента» для быстрой маршрутизации."""
        index: dict[str, str] = {}
        for tool in self._tools.values():
            for phrase in tool.spec.phrases:
                index[" ".join(phrase.lower().split())] = tool.name
        return index

    def resolve_function_name(self, function_name: str) -> str | None:
        """Вернуть имя инструмента по имени функции из ответа LLM."""
        candidate = function_name.replace("__", ".")
        return candidate if candidate in self._tools else None

    # --- исполнение --------------------------------------------------------

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Вызвать инструмент по имени.

        Ошибки не пробрасываются наружу: и сбой, и таймаут возвращаются как
        `ToolResult` с ``ok=False``. Так один скилл не роняет ядро.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(
                f"Инструмент {name!r} не найден. Доступны: "
                f"{', '.join(sorted(self._tools)) or '(ни одного)'}"
            )

        arguments = dict(arguments or {})
        try:
            payload = validate_arguments(tool.spec.parameters, arguments)
        except ToolInvalidArguments as exc:
            logger.warning("Некорректные аргументы для %s: %s", name, exc)
            return ToolResult.failure(str(exc), tool=name)

        if self._events is not None:
            self._events.emit(ToolInvoked(source=name, tool=name, arguments=payload))

        timeout = tool.timeout if tool.timeout is not None else self._default_timeout
        started = time.perf_counter()
        try:
            value = await asyncio.wait_for(tool.handler(**payload), timeout=timeout)
            result = (
                value
                if isinstance(value, ToolResult)
                else ToolResult.success(value, tool=name)
            )
        except asyncio.TimeoutError:
            logger.error("Инструмент %s не ответил за %.1f с", name, timeout)
            result = ToolResult.failure(f"Инструмент не ответил за {timeout:.0f} с", tool=name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Инструмент %s завершился ошибкой", name)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", tool=name)

        duration = time.perf_counter() - started
        result = ToolResult(
            ok=result.ok,
            value=result.value,
            error=result.error,
            tool=name,
            duration=duration,
            speech=result.speech,
        )

        if self._events is not None:
            self._events.emit(
                ToolCompleted(
                    source=name,
                    tool=name,
                    ok=result.ok,
                    duration=duration,
                    error=result.error,
                )
            )
        return result
