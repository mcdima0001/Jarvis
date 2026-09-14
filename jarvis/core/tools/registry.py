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
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from jarvis.core.bus import EventBus
from jarvis.core.contracts import ToolCompleted, ToolInvoked, ToolResult
from jarvis.core.errors import ToolInvalidArguments, ToolNotFound
from jarvis.core.tools.schema import validate_arguments
from jarvis.core.tools.tool import Tool, ToolCatalog, ToolSpec
from jarvis.core.tts.normalize import plural_form

logger = logging.getLogger(__name__)

#: Флаги, которые владелец правит из панели поверх объявленного в скилле.
_FLAGS = ("routable", "reversible")


def _load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """Прочитать поправки; битый или чужой файл — не повод не запуститься."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Поправки флагов инструментов не прочитаны (%s): %s", path, exc)
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, flags in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(flags, dict):
            continue
        clean = {
            flag: value for flag, value in flags.items()
            if (flag == "routable" and isinstance(value, bool))
            or (flag == "reversible" and (value is None or isinstance(value, bool)))
        }
        if clean:
            result[str(name)] = clean
    return result


def _save_overrides(path: Path | None, overrides: Mapping[str, Mapping[str, Any]]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


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
        overrides_path: Path | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        # Что объявил автор скилла — до поправок владельца. Нужно, чтобы поправку
        # можно было снять и чтобы панель показывала, что было «по умолчанию».
        self._declared: dict[str, ToolSpec] = {}
        self._events = events
        self._default_timeout = default_timeout
        self._overrides_path = overrides_path
        self._overrides = _load_overrides(overrides_path)

    # --- регистрация -------------------------------------------------------

    def register(self, tool: Tool, *, replace: bool = False) -> Registration:
        """Добавить инструмент в реестр."""
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"Инструмент {tool.name!r} уже зарегистрирован "
                f"(скилл {self._tools[tool.name].spec.skill!r})"
            )
        self._declared[tool.name] = tool.spec
        self._tools[tool.name] = self._with_override(tool)
        logger.debug("Инструмент зарегистрирован: %s", tool.name)
        return Registration(self, tool.name)

    def unregister(self, name: str) -> None:
        """Убрать инструмент из реестра."""
        self._declared.pop(name, None)
        if self._tools.pop(name, None) is not None:
            logger.debug("Инструмент снят: %s", name)

    # --- поправки владельца --------------------------------------------------

    def declared(self, name: str) -> ToolSpec | None:
        """Описание инструмента в том виде, как его объявил скилл."""
        return self._declared.get(name)

    def overrides(self, name: str) -> dict[str, Any]:
        """Какие флаги у инструмента поправлены владельцем."""
        return dict(self._overrides.get(name, {}))

    def set_override(self, name: str, flag: str, value: bool | None) -> ToolSpec:
        """Поправить «видит ли модель» или «обратим ли» у инструмента.

        Поправка переживает перезапуск и перезагрузку скилла: хранится файлом
        и накладывается при каждой регистрации. Значение, совпавшее с
        объявленным автором, поправкой не считается и из файла убирается.

        :param name: полное имя инструмента.
        :param flag: ``routable`` или ``reversible``.
        :param value: новое значение; ``None`` у ``reversible`` — «не объявлено».
        """
        if flag not in _FLAGS:
            raise ValueError(f"Флаг {flag!r} не правится: только {', '.join(_FLAGS)}")
        if flag == "routable" and not isinstance(value, bool):
            raise ValueError("«Видит модель» бывает только да или нет")
        declared = self._declared.get(name)
        if declared is None:
            raise ValueError(f"Инструмент {name!r} не загружен")
        own = self._overrides.setdefault(name, {})
        if getattr(declared, flag) == value:
            own.pop(flag, None)
        else:
            own[flag] = value
        if not own:
            self._overrides.pop(name, None)
        _save_overrides(self._overrides_path, self._overrides)
        tool = replace(self._tools[name], spec=declared)
        self._tools[name] = self._with_override(tool)
        logger.info("Флаг %s у %s: %s (поправка владельца)", flag, name, value)
        return self._tools[name].spec

    def _with_override(self, tool: Tool) -> Tool:
        own = self._overrides.get(tool.name)
        if not own:
            return tool
        return replace(tool, spec=replace(tool.spec, **own))

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
            # Полным словом, а не «с»: реплика произносится вслух, и
            # сокращение синтез читает буквой. Поймано на живом запуске:
            # «инструмент не ответил за тридцать с».
            seconds = int(timeout)
            result = ToolResult.failure(
                f"Инструмент не ответил за {seconds} "
                f"{plural_form(seconds, ('секунду', 'секунды', 'секунд'))}",
                tool=name,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Инструмент %s завершился ошибкой", name)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", tool=name)

        duration = time.perf_counter() - started
        # Копия с заменой, а не сборка по полям. Пересчитывать поля руками —
        # значит терять каждое новое: так молча пропало `confirm`, и вопрос
        # «отправить маме?» переставал быть вопросом на пути от инструмента к
        # диспетчеру.
        result = replace(result, tool=name, duration=duration)

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
