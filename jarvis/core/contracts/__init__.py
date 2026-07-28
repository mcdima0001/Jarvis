"""Стабильный слой контрактов.

От этого пакета зависят все остальные, а он — ни от кого. Именно поэтому любую
реализацию (шину, роутер, память, LLM) можно заменить, не трогая скиллы: они
знают контракты, а не реализации.
"""

from .events import (
    AssistantReplied,
    Event,
    IntentResolved,
    IntentUnresolved,
    MotionDetected,
    SensorReadingChanged,
    SkillLoaded,
    SkillUnloaded,
    StudioModeChanged,
    SystemStarted,
    SystemStopping,
    ToolCompleted,
    ToolInvoked,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from .intent import Intent, Utterance, detect_language, parse_number
from .results import ToolResult

__all__ = [
    "AssistantReplied",
    "Event",
    "Intent",
    "IntentResolved",
    "IntentUnresolved",
    "MotionDetected",
    "parse_number",
    "SensorReadingChanged",
    "SkillLoaded",
    "SkillUnloaded",
    "StudioModeChanged",
    "SystemStarted",
    "SystemStopping",
    "ToolCompleted",
    "ToolInvoked",
    "ToolResult",
    "Utterance",
    "detect_language",
    "VoiceCommandRecognized",
    "WakeWordDetected",
]
