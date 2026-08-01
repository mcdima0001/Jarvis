"""События — факты о мире.

Событие описывает то, что **уже произошло**: датчик показал новую температуру,
Whisper распознал фразу, скилл загрузился. У события может быть сколько угодно
подписчиков и нет возвращаемого значения.

Если нужен ответ (например «какая температура»), это не событие, а инструмент —
см. `jarvis.core.tools`.

Имена событий пишутся с пространством имён: ``область.объект.действие``.
Скиллы объявляют собственные события, наследуясь от `Event`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Базовое событие. Имя задаётся классовой константой ``NAME``."""

    NAME: ClassVar[str] = "event"

    source: str = "core"
    timestamp: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        """Имя события, по которому идёт подписка."""
        return type(self).NAME


# --- Жизненный цикл ---------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemStarted(Event):
    """Все сервисы подняты, ассистент готов к работе."""

    NAME: ClassVar[str] = "system.started"

    skills: tuple[str, ...] = ()
    tools: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemStopping(Event):
    """Начата остановка приложения."""

    NAME: ClassVar[str] = "system.stopping"

    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillLoaded(Event):
    """Скилл загружен и его инструменты доступны."""

    NAME: ClassVar[str] = "skill.loaded"

    skill: str
    version: str = ""
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillUnloaded(Event):
    """Скилл выгружен, все его регистрации отозваны."""

    NAME: ClassVar[str] = "skill.unloaded"

    skill: str


# --- Голос ------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class WakeWordDetected(Event):
    """Услышана активационная фраза."""

    NAME: ClassVar[str] = "voice.wake_word.detected"

    phrase: str = ""
    score: float = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceCommandRecognized(Event):
    """Whisper распознал реплику пользователя."""

    NAME: ClassVar[str] = "voice.command.recognized"

    text: str
    confidence: float = 1.0
    language: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantSpeaking(Event):
    """Ассистент **начинает говорить** вслух.

    Отдельное событие от `AssistantReplied` не для симметрии: то приходит,
    когда реплика уже отзвучала, а знать бывает нужно заранее. Так, музыку надо
    приглушить **до** первого слова, иначе ассистента не слышно — и не только
    после обращения по имени: он здоровается при запуске и прощается при
    выходе, а этих реплик никто не заказывал.
    """

    NAME: ClassVar[str] = "assistant.speaking"

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantReplied(Event):
    """Ассистент сформировал ответ пользователю."""

    NAME: ClassVar[str] = "assistant.replied"

    text: str
    spoken: bool = False


# --- Маршрутизация и инструменты -------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentResolved(Event):
    """Роутер определил, какой инструмент вызвать."""

    NAME: ClassVar[str] = "intent.resolved"

    tool: str
    resolver: str
    confidence: float
    utterance: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentUnresolved(Event):
    """Ни один резолвер не понял запрос."""

    NAME: ClassVar[str] = "intent.unresolved"

    utterance: str
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvoked(Event):
    """Инструмент вызван."""

    NAME: ClassVar[str] = "tool.invoked"

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCompleted(Event):
    """Инструмент отработал (успешно или нет)."""

    NAME: ClassVar[str] = "tool.completed"

    tool: str
    ok: bool
    duration: float = 0.0
    error: str | None = None


# --- Студия -----------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SensorReadingChanged(Event):
    """Датчик сообщил новое значение."""

    NAME: ClassVar[str] = "sensor.reading.changed"

    sensor: str
    value: float
    unit: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class StudioModeChanged(Event):
    """Режим студии переключён (игровой, запись, кино…)."""

    NAME: ClassVar[str] = "studio.mode.changed"

    mode: str
    previous: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionDetected(Event):
    """Датчик движения сработал."""

    NAME: ClassVar[str] = "sensor.motion.detected"

    zone: str = "studio"
