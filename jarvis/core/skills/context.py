"""SkillContext — весь Dependency Injection одним объектом.

Скилл ничего не импортирует из ядра ради доступа к сервисам и не лезет в
глобальные переменные: всё нужное приходит в `setup()`. Заменить реализацию
любого сервиса можно в одном месте — в composition root (`core/app.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from jarvis.core.bus import EventBus
from jarvis.core.tools import ToolRegistry

from .scope import SkillScope

if TYPE_CHECKING:  # только для типов — реальных зависимостей не создаём
    from jarvis.core.llm import LLMService
    from jarvis.core.memory import Memory
    from jarvis.core.situation import Situation
    from jarvis.core.state import Modes
    from jarvis.core.tts import TTS


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillContext:
    """Сервисы, которые получает скилл при инициализации.

    :param config: секция ``skills.settings.<имя>`` из config.yaml.
    :param scope: через него регистрируются инструменты, подписки и задачи.
    :param modes: состояние, которое живёт между командами. Скилл его читает
        («меня просили не трогать музыку») и при желании ставит сам.
    :param situation: куда положить факт о происходящем, который скилл узнал по
        дороге. Оттуда он попадёт в подсказку модели при разборе следующей
        фразы. Класть только то, что уже известно: ходить за фактом специально
        нельзя, это добавит ожидание к каждой команде.
    """

    skill: str
    config: Mapping[str, Any]
    logger: Logger
    events: EventBus
    tools: ToolRegistry
    memory: "Memory"
    llm: "LLMService"
    tts: "TTS"
    scope: SkillScope
    root: Path
    modes: "Modes"
    situation: "Situation"

    def setting(self, key: str, default: Any = None) -> Any:
        """Достать значение из конфига скилла."""
        return self.config.get(key, default)
