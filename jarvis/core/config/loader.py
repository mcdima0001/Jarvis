"""Загрузка config.yaml: подстановка переменных окружения и сборка датаклассов.

Секреты в конфиге не хранятся — только ссылки вида ``${JARVIS_OPENROUTER_KEY}``.
Значения берутся из окружения; файл `.env` рядом с конфигом подхватывается
автоматически и лежит в .gitignore.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from jarvis.core.errors import ConfigError

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
    STTConfig,
    SkillsConfig,
    TaskProfile,
    TTSConfig,
    VADConfig,
    WakeWordConfig,
)

# ${VAR} и ${VAR:-значение по умолчанию}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def load_dotenv(path: Path) -> None:
    """Загрузить пары KEY=VALUE из .env, не затирая уже заданное окружение."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _expand(value: Any) -> Any:
    """Рекурсивно подставить переменные окружения в строки конфига."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is None:
                if default is None:
                    raise ConfigError(
                        f"Переменная окружения {name!r} не задана и не имеет "
                        f"значения по умолчанию. Задай её в .env или используй "
                        f"синтаксис ${{{name}:-значение}}."
                    )
                resolved = default
            return resolved

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Достать секцию верхнего уровня, вернув пустую при её отсутствии."""
    value = data.get(name) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"Секция {name!r} должна быть словарём, а не {type(value).__name__}")
    return value


def _resolve(root: Path, value: Any) -> Path:
    """Превратить путь из конфига в абсолютный относительно корня проекта."""
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def _prompts(value: Any) -> dict[str, str]:
    """Разобрать подсказку словаря: одна строка или карта по языкам."""
    if not value:
        return {}
    if isinstance(value, str):
        return {"*": value}
    return {str(code): str(text) for code, text in value.items()}


def _voices(section: Mapping[str, Any]) -> dict[str, str]:
    """Разобрать голоса: карта язык -> голос, с поддержкой старого ``voice``."""
    voices = {str(code): str(name) for code, name in (section.get("voices") or {}).items()}
    single = section.get("voice")
    if single and not voices:
        # Конфиг с одним голосом: считаем его голосом языка по умолчанию.
        voices[str(section.get("default_language", "ru"))] = str(single)
    return voices


def _wake_phrases(section: Mapping[str, Any]) -> tuple[str, ...]:
    """Разобрать написания имени: список ``phrases`` или одиночное ``phrase``."""
    listed = section.get("phrases")
    if listed:
        return tuple(str(item).lower() for item in listed)
    single = section.get("phrase")
    if single:
        return (str(single).lower(),)
    return ("джарвис", "jarvis")


def _address(value: Any) -> dict[str, str]:
    """Разобрать обращение: одна строка на все языки или карта по языкам."""
    if value is None:
        return {}
    if isinstance(value, str):
        # «address: сэр» — значит, так обращаться на любом языке.
        return {"*": value}
    return {str(code): str(text) for code, text in value.items()}


def _persona_phrases(value: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    """Разобрать свои реплики: ситуация -> язык -> список фраз."""
    phrases: dict[str, dict[str, tuple[str, ...]]] = {}
    for situation, languages in (value or {}).items():
        if not isinstance(languages, Mapping):
            raise ConfigError(
                f"persona.phrases.{situation}: ожидалась карта «язык: список фраз»"
            )
        phrases[str(situation)] = {
            str(code): tuple(str(line) for line in (lines or ()))
            for code, lines in languages.items()
        }
    return phrases


def _build_llm(section: Mapping[str, Any]) -> LLMConfig:
    """Собрать конфигурацию LLM: провайдеры и профили задач."""
    providers: dict[str, ProviderConfig] = {}
    for name, raw in _section(section, "providers").items():
        providers[name] = ProviderConfig(
            name=name,
            type=str(raw.get("type", name)),
            api_key=str(raw.get("api_key") or ""),
            base_url=str(raw.get("base_url") or ""),
            timeout=float(raw.get("timeout", 60.0)),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        )

    profiles: dict[str, TaskProfile] = {}
    for task, raw in _section(section, "profiles").items():
        provider = str(raw.get("provider", ""))
        if provider and provider not in providers:
            raise ConfigError(
                f"Профиль {task!r} ссылается на провайдера {provider!r}, "
                f"которого нет в llm.providers"
            )
        profiles[task] = TaskProfile(
            task=task,
            provider=provider,
            model=str(raw.get("model", "")),
            temperature=float(raw.get("temperature", 0.7)),
            max_tokens=int(raw.get("max_tokens", 1024)),
            system=raw.get("system"),
        )

    default_task = str(section.get("default_task", "dialog"))
    if profiles and default_task not in profiles:
        raise ConfigError(
            f"llm.default_task={default_task!r} не найден среди профилей: "
            f"{', '.join(sorted(profiles))}"
        )
    return LLMConfig(default_task=default_task, providers=providers, profiles=profiles)


def load_config(path: Path | str | None = None, *, root: Path | None = None) -> JarvisConfig:
    """Прочитать конфиг и вернуть проверенную структуру настроек.

    :param path: путь к config.yaml; по умолчанию ``config/config.yaml``.
    :param root: корень проекта, относительно которого разрешаются пути.
    """
    config_path = Path(path or os.environ.get("JARVIS_CONFIG") or DEFAULT_CONFIG_PATH)
    config_path = config_path.expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {config_path}")

    project_root = (root or config_path.parent.parent).resolve()
    load_dotenv(project_root / ".env")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Не удалось разобрать {config_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{config_path}: ожидался словарь на верхнем уровне")

    data = _expand(raw)

    app = _section(data, "app")
    log = _section(data, "logging")
    runtime = _section(data, "runtime")
    skills = _section(data, "skills")
    router = _section(data, "router")
    stt = _section(data, "stt")
    tts = _section(data, "tts")
    audio = _section(data, "audio")
    persona = _section(data, "persona")
    memory = _section(data, "memory")

    return JarvisConfig(
        root=project_root,
        source=config_path,
        app=AppConfig(
            name=str(app.get("name", "Jarvis")),
            language=str(app.get("language", "ru")),
        ),
        logging=LoggingConfig(
            level=str(log.get("level", "INFO")).upper(),
            dir=_resolve(project_root, log.get("dir", "logs")),
            file=str(log.get("file", "jarvis.log")),
            console=bool(log.get("console", True)),
            time_format=str(log.get("time_format", "%d.%m.%y, %H:%M:%S")),
            keep_days=int(log.get("keep_days", 14)),
        ),
        runtime=RuntimeConfig(
            worker_threads=int(runtime.get("worker_threads", 2)),
            tool_timeout=float(runtime.get("tool_timeout", 30.0)),
        ),
        skills=SkillsConfig(
            paths=tuple(_resolve(project_root, p) for p in skills.get("paths", ["skills"])),
            disabled=frozenset(str(n) for n in skills.get("disabled", ())),
            settings={str(k): dict(v or {}) for k, v in (skills.get("settings") or {}).items()},
        ),
        router=RouterConfig(
            confidence_threshold=float(router.get("confidence_threshold", 0.6)),
            resolvers=tuple(str(r) for r in router.get("resolvers", ())),
            aliases={str(k).lower(): str(v) for k, v in (router.get("aliases") or {}).items()},
            intent_tasks=tuple(
                str(task) for task in (router.get("intent_tasks") or ("intent",))
            ),
            learn_commands=bool(router.get("learn_commands", True)),
            learned_section=str(router.get("learned_section", "commands")),
        ),
        llm=_build_llm(_section(data, "llm")),
        stt=STTConfig(
            engine=str(stt.get("engine", "faster-whisper")),
            model=str(stt.get("model", "base")),
            device=str(stt.get("device", "auto")),
            compute_type=str(stt.get("compute_type", "int8")),
            language=str(stt.get("language", "ru")),
            languages=tuple(str(code) for code in stt.get("languages", ("ru", "en"))),
            language_min_probability=float(stt.get("language_min_probability", 0.6)),
            fallback_language=str(stt.get("fallback_language", "ru")),
            beam_size=int(stt.get("beam_size", 1)),
            vad_filter=bool(stt.get("vad_filter", True)),
            cpu_threads=int(stt.get("cpu_threads", 0)),
            models_dir=_resolve(project_root, stt.get("models_dir", "models/whisper")),
            initial_prompt=_prompts(stt.get("initial_prompt")),
        ),
        tts=TTSConfig(
            engine=str(tts.get("engine", "piper")),
            voices=_voices(tts),
            default_language=str(tts.get("default_language", "ru")),
            models_dir=_resolve(project_root, tts.get("models_dir", "models/piper")),
            length_scale=float(tts.get("length_scale", 1.0)),
            sample_rate=int(tts.get("sample_rate", 22050)),
            device=str(tts.get("device", "auto")),
            pronounce={str(k): str(v) for k, v in (tts.get("pronounce") or {}).items()},
        ),
        audio=AudioConfig(
            engine=str(audio.get("engine", "sounddevice")),
            input_device=audio.get("input_device"),
            output_device=audio.get("output_device"),
            sample_rate=int(audio.get("sample_rate", 16000)),
            frame_ms=int(audio.get("frame_ms", 30)),
            silence_ms=int(audio.get("silence_ms", 800)),
            max_utterance_s=float(audio.get("max_utterance_s", 15.0)),
            min_utterance_ms=int(audio.get("min_utterance_ms", 300)),
            echo_tail_ms=int(audio.get("echo_tail_ms", 500)),
            pending_limit=int(audio.get("pending_limit", 2)),
            activation_sound=(
                _resolve(project_root, audio["activation_sound"])
                if audio.get("activation_sound")
                else None
            ),
            vad=VADConfig(
                engine=str(_section(audio, "vad").get("engine") or "energy"),
                threshold=float(_section(audio, "vad").get("threshold", 0.0)),
                calibrate_seconds=float(_section(audio, "vad").get("calibrate_seconds", 1.0)),
                models_dir=_resolve(
                    project_root, _section(audio, "vad").get("models_dir", "models/vad")
                ),
            ),
            wake_word=WakeWordConfig(
                mode=str(_section(audio, "wake_word").get("mode") or "text"),
                phrases=_wake_phrases(_section(audio, "wake_word")),
                aliases=tuple(
                    str(a).lower() for a in (_section(audio, "wake_word").get("aliases") or ())
                ),
                similarity=float(_section(audio, "wake_word").get("similarity", 0.7)),
                follow_up_s=float(_section(audio, "wake_word").get("follow_up_s", 10.0)),
            ),
        ),
        persona=PersonaConfig(
            address=_address(persona.get("address")),
            phrases=_persona_phrases(persona.get("phrases")),
            replace=bool(persona.get("replace", False)),
            greet_on_start=bool(persona.get("greet_on_start", True)),
            farewell_on_stop=bool(persona.get("farewell_on_stop", True)),
        ),
        memory=MemoryConfig(
            dir=_resolve(project_root, memory.get("dir", "memory")),
            documents=tuple(str(n) for n in memory.get("documents", ())),
            journals=tuple(str(n) for n in memory.get("journals", ())),
            context_budget_tokens=int(memory.get("context_budget_tokens", 2000)),
        ),
    )
