"""Базовый класс скилла — единый интерфейс всех возможностей Jarvis.

Минимальный рабочий скилл:

    from jarvis.core.skills import Skill, SkillMeta
    from jarvis.core.tools import tool

    class LightSkill(Skill):
        meta = SkillMeta(name="light", description="Освещение студии")

        @tool(phrases=["включи свет"])
        async def turn_on(self, zone: str = "studio") -> str:
            \"\"\"Включить свет в зоне.

            :param zone: зона освещения.
            \"\"\"
            return f"Свет включён: {zone}"

Инструменты, фразы и схема параметров подхватываются автоматически. Ядро и
конфиг править не нужно — достаточно положить файл в `skills/`.
"""

from __future__ import annotations

import sys
from abc import ABC
from dataclasses import dataclass, field
from typing import ClassVar

from jarvis.core.bus import EventBus
from jarvis.core.errors import SkillError
from jarvis.core.tools import ToolRegistry

from .context import SkillContext


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillMeta:
    """Паспорт скилла."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    #: Платформы, на которых скилл имеет смысл: ``("windows",)``. Пусто — любая.
    platforms: tuple[str, ...] = ()
    #: Инструменты других скиллов, без которых этот работать не будет.
    requires: tuple[str, ...] = field(default_factory=tuple)

    def supported_here(self) -> bool:
        """Подходит ли скилл текущей платформе."""
        if not self.platforms:
            return True
        current = {
            "linux": "linux",
            "win32": "windows",
            "darwin": "macos",
        }.get(sys.platform, sys.platform)
        return current in self.platforms


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthStatus:
    """Состояние скилла."""

    ok: bool
    detail: str = ""

    @classmethod
    def healthy(cls, detail: str = "") -> "HealthStatus":
        """Всё в порядке."""
        return cls(ok=True, detail=detail)

    @classmethod
    def degraded(cls, detail: str) -> "HealthStatus":
        """Работает частично или не работает."""
        return cls(ok=False, detail=detail)


class Skill(ABC):
    """Базовый класс всех скиллов.

    Переопределяй `on_setup`, `on_start`, `on_stop` — жизненным циклом
    управляет `SkillManager`, вручную его дёргать не нужно.
    """

    meta: ClassVar[SkillMeta]

    def __init__(self) -> None:
        self._context: SkillContext | None = None

    # --- доступ к сервисам -------------------------------------------------

    @property
    def context(self) -> SkillContext:
        """Контекст со всеми сервисами. Доступен начиная с `on_setup`."""
        if self._context is None:
            raise SkillError(
                f"Скилл {type(self).__name__} ещё не инициализирован: "
                f"контекст появляется в on_setup()"
            )
        return self._context

    @property
    def log(self):
        """Логгер с именем скилла."""
        return self.context.logger

    @property
    def events(self) -> EventBus:
        """Шина событий."""
        return self.context.events

    @property
    def tools(self) -> ToolRegistry:
        """Реестр инструментов — вызов чужих команд по имени."""
        return self.context.tools

    # --- жизненный цикл ----------------------------------------------------

    async def setup(self, context: SkillContext) -> None:
        """Принять контекст. Вызывается менеджером; переопределяй `on_setup`."""
        self._context = context
        await self.on_setup()

    async def on_setup(self) -> None:
        """Инициализация: подписки, соединения, разбор настроек."""

    async def on_start(self) -> None:
        """Запуск фоновой работы. Долгие циклы — через ``context.scope.spawn``."""

    async def on_stop(self) -> None:
        """Освобождение ресурсов. Подписки и задачи ядро снимет само."""

    async def health(self) -> HealthStatus:
        """Проверка работоспособности; по умолчанию — всё в порядке."""
        return HealthStatus.healthy()
