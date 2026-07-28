"""Движки синтеза речи.

Каждый движок закрыт одним маленьким интерфейсом: подготовить голос и
синтезировать текст. Всё синхронное — вызывается из `BlockingWorker`, потому
что нейросетевой синтез это CPU-bound работа, которая заморозила бы event loop.

Три движка с разными сильными сторонами:

* **Piper** — самый лёгкий и быстрый, 22 кГц. Хорош, когда важна нагрузка.
* **Kokoro** — 24 кГц, заметно естественнее, есть британские мужские голоса.
  Модель 311 МБ, RTF около 0.9 на слабом процессоре.
* **Silero** — 48 кГц, русские голоса живее, чем у Piper. Тянет за собой torch.

Голос в конфиге пишется как ``движок:голос`` (``kokoro:bm_george``), а без
префикса берётся движок по умолчанию.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)

#: Во что переводить float-выход нейросетевых движков.
_PCM16_SCALE = 32767


@runtime_checkable
class SpeechBackend(Protocol):
    """Синхронный движок синтеза. Работает внутри `BlockingWorker`."""

    @property
    def engine(self) -> str:
        """Имя движка."""
        ...

    def prepare(self, voice: str, language: str) -> None:
        """Загрузить всё, что нужно для этого голоса."""
        ...

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        """Синтезировать речь. Возвращает моно-PCM 16 бит и частоту."""
        ...


def _to_pcm16(samples: Any) -> bytes:
    """Привести float-выход модели к PCM 16 бит."""
    array = np.asarray(samples, dtype=np.float32)
    return (np.clip(array, -1.0, 1.0) * _PCM16_SCALE).astype(np.int16).tobytes()


class PiperBackend:
    """Piper: лёгкий и быстрый, отдельный файл модели на каждый голос."""

    def __init__(self, models_dir: Path, *, length_scale: float = 1.0) -> None:
        self._dir = models_dir
        self._length_scale = length_scale
        self._voices: dict[str, Any] = {}

    @property
    def engine(self) -> str:
        """Имя движка."""
        return "piper"

    def prepare(self, voice: str, language: str) -> None:
        """Загрузить голос из файла."""
        if voice in self._voices:
            return
        from piper import PiperVoice

        path = self._dir / f"{voice}.onnx"
        if not path.is_file():
            raise FileNotFoundError(
                f"Голос Piper не найден: {path}. "
                f"Скачай: python -m jarvis --download-voice piper:{voice}"
            )
        self._voices[voice] = PiperVoice.load(str(path))

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        """Синтезировать речь голосом Piper."""
        from piper import SynthesisConfig

        self.prepare(voice, language)
        chunks = list(
            self._voices[voice].synthesize(
                text, syn_config=SynthesisConfig(length_scale=self._length_scale)
            )
        )
        if not chunks:
            return b"", 22050
        return b"".join(c.audio_int16_bytes for c in chunks), int(chunks[0].sample_rate)


class KokoroBackend:
    """Kokoro: 24 кГц, естественная интонация, британские мужские голоса.

    Модель одна на все голоса, поэтому грузится один раз. Фонемизация идёт
    через espeak-ng, который приезжает вместе с пакетом — доустанавливать
    ничего не нужно даже на Windows.
    """

    #: Какой акцент просить у модели для кода языка.
    _LANGS = {"en": "en-gb", "en-us": "en-us", "fr": "fr-fr", "it": "it", "es": "es"}

    def __init__(self, models_dir: Path, *, speed: float = 1.0) -> None:
        self._dir = models_dir
        self._speed = speed
        self._model: Any = None

    @property
    def engine(self) -> str:
        """Имя движка."""
        return "kokoro"

    @property
    def model_path(self) -> Path:
        """Файл модели."""
        return self._dir / "kokoro-v1.0.onnx"

    @property
    def voices_path(self) -> Path:
        """Файл со стилями голосов."""
        return self._dir / "voices-v1.0.bin"

    def prepare(self, voice: str, language: str) -> None:
        """Загрузить модель (общую для всех голосов)."""
        if self._model is not None:
            return
        from kokoro_onnx import Kokoro

        if not self.model_path.is_file() or not self.voices_path.is_file():
            raise FileNotFoundError(
                f"Модель Kokoro не найдена в {self._dir}. "
                f"Скачай: python -m jarvis --download-voice kokoro:{voice}"
            )
        self._model = Kokoro(str(self.model_path), str(self.voices_path))

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        """Синтезировать речь голосом Kokoro."""
        self.prepare(voice, language)
        code = self._LANGS.get(language.lower(), "en-gb")
        samples, rate = self._model.create(text, voice=voice, speed=self._speed, lang=code)
        return _to_pcm16(samples), int(rate)

    def voices(self) -> list[str]:
        """Все доступные голоса модели."""
        self.prepare("bm_george", "en")
        return sorted(self._model.get_voices())


class SileroBackend:
    """Silero: 48 кГц, русские голоса живее Piper. Требует torch."""

    #: Пакет моделей на каждый язык.
    _PACKS = {"ru": "v4_ru.pt", "en": "v3_en.pt", "de": "v3_de.pt"}
    _SAMPLE_RATE = 48000

    def __init__(self, models_dir: Path) -> None:
        self._dir = models_dir
        self._models: dict[str, Any] = {}

    @property
    def engine(self) -> str:
        """Имя движка."""
        return "silero"

    def pack_path(self, language: str) -> Path:
        """Файл пакета моделей для языка."""
        return self._dir / self._PACKS.get(language, self._PACKS["ru"])

    def prepare(self, voice: str, language: str) -> None:
        """Загрузить пакет моделей нужного языка."""
        if language in self._models:
            return
        import torch

        path = self.pack_path(language)
        if not path.is_file():
            raise FileNotFoundError(
                f"Модель Silero не найдена: {path}. "
                f"Скачай: python -m jarvis --download-voice silero:{voice}"
            )
        model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
        model.to(torch.device("cpu"))
        self._models[language] = model

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        """Синтезировать речь голосом Silero."""
        self.prepare(voice, language)
        audio = self._models[language].apply_tts(
            text=text,
            speaker=voice,
            sample_rate=self._SAMPLE_RATE,
        )
        return _to_pcm16(audio.numpy()), self._SAMPLE_RATE

    def voices(self, language: str = "ru") -> list[str]:
        """Голоса, доступные в пакете языка."""
        self.prepare("aidar", language)
        return [s for s in self._models[language].speakers if s != "random"]


#: Известные движки.
BACKENDS = ("piper", "kokoro", "silero")


def parse_voice(spec: str, *, default_engine: str = "piper") -> tuple[str, str]:
    """Разобрать запись голоса ``движок:голос``.

    :return: пара «движок» и «имя голоса». Без префикса берётся движок
        по умолчанию — так старые конфиги продолжают работать.
    """
    if ":" in spec:
        engine, _, voice = spec.partition(":")
        engine = engine.strip().lower()
        if engine in BACKENDS:
            return engine, voice.strip()
        logger.warning("Неизвестный движок %r в голосе %r — беру %s", engine, spec, default_engine)
    return default_engine, spec.strip()


def build_backend(engine: str, models_dir: Path, *, length_scale: float = 1.0) -> SpeechBackend:
    """Создать движок по имени."""
    if engine == "kokoro":
        # length_scale больше единицы означает «медленнее», у Kokoro наоборот.
        return KokoroBackend(models_dir / "kokoro", speed=1.0 / max(length_scale, 0.1))
    if engine == "silero":
        return SileroBackend(models_dir / "silero")
    return PiperBackend(models_dir / "piper", length_scale=length_scale)
