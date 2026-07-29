"""Конфигурация: типизированная схема и загрузчик."""

from .loader import DEFAULT_CONFIG_PATH, load_config, load_dotenv
from .schema import (
    AppConfig,
    AudioConfig,
    JarvisConfig,
    LLMConfig,
    LoggingConfig,
    MemoryConfig,
    PersonaConfig,
    ProviderConfig,
    RouterConfig,
    RuntimeConfig,
    SkillsConfig,
    STTConfig,
    TaskProfile,
    TTSConfig,
    VADConfig,
    WakeWordConfig,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AppConfig",
    "AudioConfig",
    "JarvisConfig",
    "LLMConfig",
    "LoggingConfig",
    "MemoryConfig",
    "PersonaConfig",
    "ProviderConfig",
    "RouterConfig",
    "RuntimeConfig",
    "STTConfig",
    "SkillsConfig",
    "TTSConfig",
    "TaskProfile",
    "VADConfig",
    "WakeWordConfig",
    "load_config",
    "load_dotenv",
]
