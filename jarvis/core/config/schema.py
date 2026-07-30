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

    #: Что показывать в консоли. Её читают глазами по ходу дела.
    level: str = "INFO"
    #: Что писать в файл. По умолчанию подробнее консоли: файл читают, когда
    #: уже что-то пошло не так, и нужны как раз те записи, которых не ждали.
    file_level: str = "DEBUG"
    dir: Path = Path("logs")
    file: str = "jarvis.log"
    console: bool = True
    #: Цвет в консоли: ``auto`` (только для живого терминала), ``always``,
    #: ``never``. В файл цвет не попадает никогда — там он ломает и `grep`, и
    #: чтение глазами.
    color: str = "auto"
    #: Как показывать время записи — в формате `time.strftime`. Одинаково в
    #: файле и в консоли: искать причину приходится то там, то там, и разные
    #: написания времени этому только мешают.
    time_format: str = "%d.%m.%y, %H:%M:%S"
    #: Сколько дневных файлов хранить. Один файл — один день: имя файла и есть
    #: ответ на вопрос «за какой это день».
    keep_days: int = 14


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
    resolvers: tuple[str, ...] = ("phrase", "alias", "learned", "llm", "fallback")
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: Запоминать формулировки, которые модель разобрала удачно. Со второго
    #: раза такая фраза обходится без модели, то есть бесплатно.
    learn_commands: bool = True
    #: Раздел памяти для выученных формулировок.
    learned_section: str = "commands"
    #: Профили задач, которыми резолвер `llm` разбирает команду, по порядку.
    #: Первая — самая дешёвая; следующая спрашивается, только если предыдущая
    #: инструмента не выбрала. Одна задача в списке — переспрашивать не будем.
    intent_tasks: tuple[str, ...] = ("intent",)


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
    #: Код языка или ``auto``. Автоопределение на коротких фразах ненадёжно,
    #: поэтому работает вместе с порогом уверенности и откатом.
    language: str = "ru"
    #: Какие языки допускаются при ``language: auto``.
    languages: tuple[str, ...] = ("ru", "en")
    #: Ниже этой уверенности определение отбрасывается и берётся `fallback_language`.
    language_min_probability: float = 0.6
    #: Основной язык: на него откатываемся, когда определение не уверено.
    fallback_language: str = "ru"
    beam_size: int = 1
    #: Отбрасывать тишину и не-речь до распознавания (Silero внутри Whisper).
    #: Главный рычаг на процессоре: модель перестаёт разбирать фон.
    vad_filter: bool = True
    #: Сколько потоков отдать Whisper; 0 — на усмотрение ctranslate2.
    cpu_threads: int = 0
    models_dir: Path = Path("models/whisper")
    #: Подсказка словаря по языкам. Имя «Джарвис» и названия программ модель
    #: сама по себе слышит плохо («жаркость», «жар виск»); с подсказкой — уверенно.
    initial_prompt: Mapping[str, str] = field(default_factory=dict)

    @property
    def auto_detect(self) -> bool:
        """Определять ли язык автоматически."""
        return self.language in ("", "auto")

    def prompt_for(self, language: str) -> str | None:
        """Подсказка словаря для языка."""
        return self.initial_prompt.get(language) or self.initial_prompt.get("*") or None


@dataclass(frozen=True, slots=True, kw_only=True)
class TTSConfig:
    """Синтез речи."""

    engine: str = "piper"
    #: Голос под каждый язык: ``{"ru": "ru_RU-denis-medium", "en": "en_US-ryan-high"}``.
    #: Русский голос английский текст внятно не прочтёт, и наоборот.
    voices: Mapping[str, str] = field(default_factory=dict)
    #: Язык, на котором говорим, если он не указан явно.
    default_language: str = "ru"
    models_dir: Path = Path("models/piper")
    length_scale: float = 1.0
    sample_rate: int = 22050
    #: Где считать тяжёлым движкам: ``auto`` | ``cpu`` | ``cuda``.
    device: str = "auto"
    #: Как читать чужие названия: ``{"OBS": "О-Би-Эс"}``. Дополняет
    #: встроенный словарь в `jarvis.core.tts.normalize`.
    pronounce: Mapping[str, str] = field(default_factory=dict)

    def voice_for(self, language: str | None) -> tuple[str, str]:
        """Подобрать голос под язык.

        :return: пара «язык» и «имя голоса». Если голоса для языка нет,
            берётся язык по умолчанию — лучше прочитать с акцентом, чем молчать.
        """
        code = (language or self.default_language).split("-")[0].lower()
        voice = self.voices.get(code)
        if voice:
            return code, voice
        fallback = self.default_language
        return fallback, self.voices.get(fallback, "")

    @property
    def languages(self) -> tuple[str, ...]:
        """Языки, для которых настроен голос."""
        return tuple(sorted(self.voices))


@dataclass(frozen=True, slots=True, kw_only=True)
class VADConfig:
    """Детектор речи."""

    engine: str = "energy"
    #: Порог громкости 0..1. Ноль включает автокалибровку по фону комнаты.
    threshold: float = 0.0
    #: Сколько секунд слушать фон перед калибровкой.
    calibrate_seconds: float = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class WakeWordConfig:
    """Активационная фраза."""

    #: ``text`` — проверять по распознанному тексту, ``none`` — реагировать на всё.
    mode: str = "text"
    #: Написания имени на всех языках, на которых к ассистенту обращаются.
    #: Нечёткое сравнение идёт с каждым: «jarvis» и «джарвис» — разные алфавиты,
    #: и одно другому не близко.
    phrases: tuple[str, ...] = ("джарвис", "jarvis")
    #: Варианты, которые засчитываются как обращение без нечёткого сравнения.
    aliases: tuple[str, ...] = ()
    #: Насколько похожим должно быть первое слово (Whisper часто пишет имя иначе).
    similarity: float = 0.7
    #: Сколько секунд после голого «Джарвис» ждать команду без повторного имени.
    follow_up_s: float = 10.0

    @property
    def phrase(self) -> str:
        """Основное написание имени — для логов и событий."""
        return self.phrases[0] if self.phrases else "джарвис"


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioConfig:
    """Захват и воспроизведение звука."""

    engine: str = "sounddevice"
    input_device: str | int | None = None
    output_device: str | int | None = None
    sample_rate: int = 16000
    frame_ms: int = 30
    #: Сколько тишины считать концом фразы.
    silence_ms: int = 800
    #: Предохранитель от бесконечной записи.
    max_utterance_s: float = 15.0
    #: Минимальная длина фрагмента, который вообще имеет смысл распознавать.
    min_utterance_ms: int = 300
    #: Сколько ещё глушить микрофон после своей реплики: колонки продолжают
    #: звучать, а в комнате остаётся реверберация.
    echo_tail_ms: int = 500
    #: Короткий звук-отклик, когда команда распознана. Пусто — молча.
    activation_sound: Path | None = None
    #: Сколько фрагментов ждут распознавания. Очередь длиннее означает, что
    #: команда выполнится с опозданием; короче — что при фоновой речи
    #: фрагменты теряются чаще.
    pending_limit: int = 2
    vad: VADConfig = field(default_factory=VADConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)

    @property
    def frame_bytes(self) -> int:
        """Размер одного кадра в байтах (моно, 16 бит)."""
        return int(self.sample_rate * self.frame_ms / 1000) * 2

    @property
    def silence_frames(self) -> int:
        """Сколько подряд тихих кадров означают конец фразы."""
        return max(1, int(self.silence_ms / self.frame_ms))

    @property
    def max_utterance_bytes(self) -> int:
        """Максимальный размер накопленной фразы в байтах."""
        return int(self.sample_rate * self.max_utterance_s) * 2

    @property
    def min_utterance_bytes(self) -> int:
        """Минимальный размер фрагмента для распознавания."""
        return int(self.sample_rate * self.min_utterance_ms / 1000) * 2


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonaConfig:
    """Манера речи: обращение, свои варианты реплик, приветствие и прощание."""

    #: Обращение по языкам; ключ ``*`` перекрывает все языки, пустая строка
    #: убирает обращение вместе с запятой при нём.
    address: Mapping[str, str] = field(default_factory=dict)
    #: Свои варианты: ситуация -> язык -> список фраз.
    phrases: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    #: ``True`` — свои фразы вместо встроенных, иначе в дополнение к ним.
    replace: bool = False
    greet_on_start: bool = True
    farewell_on_stop: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryConfig:
    """Разделы памяти и бюджет контекста."""

    dir: Path = Path("memory")
    documents: tuple[str, ...] = ("profile", "preferences", "studio", "sites", "commands")
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
    persona: PersonaConfig
    memory: MemoryConfig
