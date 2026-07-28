"""Типизированная схема конфигурации.

Конфиг разбирается один раз при старте и дальше живёт как набор неизменяемых
датаклассов. Ошибка в config.yaml обнаруживается при запуске, а не через час
работы в глубине скилла.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class AppConfig:
    """Общие сведения о приложении."""

    name: str = "Jarvis"
    language: str = "ru"


@dataclass(frozen=True, slots=True, kw_only=True)
class LoggingConfig:
    """Настройки логирования."""

    level: str = "INFO"
    dir: Path = Path("logs")
    file: str = "jarvis.log"
    console: bool = True
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConfig:
    """Исполнение блокирующих задач и предохранители."""

    worker_threads: int = 2
    tool_timeout: float = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillsConfig:
    """Где искать скиллы и как их настраивать."""

    paths: tuple[Path, ...] = (Path("skills"),)
    disabled: frozenset[str] = frozenset()
    settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def settings_for(self, skill_name: str) -> Mapping[str, Any]:
        """Вернуть секцию конфига конкретного скилла (пустую, если её нет)."""
        return self.settings.get(skill_name, {})


@dataclass(frozen=True, slots=True, kw_only=True)
class RouterConfig:
    """Цепочка резолверов и порог уверенности."""

    confidence_threshold: float = 0.6
    resolvers: tuple[str, ...] = ("phrase", "alias", "llm", "fallback")
    aliases: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderConfig:
    """Подключение к конкретному провайдеру LLM."""

    name: str
    type: str
    api_key: str = ""
    base_url: str = ""
    timeout: float = 60.0
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """Есть ли всё необходимое, чтобы провайдер реально работал."""
        return bool(self.api_key)


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskProfile:
    """Модель и параметры под конкретный тип задачи (диалог, код, суммаризация…)."""

    task: str
    provider: str
    model: str
    temperature: float = 0.7
    max_tokens: int = 1024
    system: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMConfig:
    """Провайдеры и профили задач."""

    default_task: str = "dialog"
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    profiles: Mapping[str, TaskProfile] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class STTConfig:
    """Распознавание речи."""

    engine: str = "faster-whisper"
    model: str = "base"
    device: str = "auto"
    compute_type: str = "int8"
    language: str = "ru"
    beam_size: int = 1
    models_dir: Path = Path("models/whisper")


@dataclass(frozen=True, slots=True, kw_only=True)
class TTSConfig:
    """Синтез речи."""

    engine: str = "piper"
    voice: str = "ru_RU-irina-medium"
    models_dir: Path = Path("models/piper")
    length_scale: float = 1.0
    sample_rate: int = 22050


@dataclass(frozen=True, slots=True, kw_only=True)
class VADConfig:
    """Детектор речи."""

    engine: str | None = None
    aggressiveness: int = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class WakeWordConfig:
    """Активационная фраза."""

    engine: str | None = None
    phrase: str = "джарвис"
    threshold: float = 0.5


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioConfig:
    """Захват и воспроизведение звука."""

    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000
    frame_ms: int = 30
    vad: VADConfig = field(default_factory=VADConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryConfig:
    """Разделы памяти и бюджет контекста."""

    dir: Path = Path("memory")
    documents: tuple[str, ...] = ("profile", "preferences", "studio")
    journals: tuple[str, ...] = ("today", "history")
    context_budget_tokens: int = 2000


@dataclass(frozen=True, slots=True, kw_only=True)
class JarvisConfig:
    """Корень конфигурации."""

    root: Path
    source: Path
    app: AppConfig
    logging: LoggingConfig
    runtime: RuntimeConfig
    skills: SkillsConfig
    router: RouterConfig
    llm: LLMConfig
    stt: STTConfig
    tts: TTSConfig
    audio: AudioConfig
    memory: MemoryConfig
