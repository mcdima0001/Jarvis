"""Скачивание голосов Piper.

Модели Whisper faster-whisper тянет сам при первом запуске, а голоса Piper —
нет: их нужно положить рядом руками. Этот модуль убирает ручной шаг, чтобы
настройка на новой машине не превращалась в поиск ссылок.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from jarvis.core.errors import JarvisError

logger = logging.getLogger(__name__)

_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_LISTING = "https://huggingface.co/api/models/rhasspy/piper-voices/tree/main"

#: Голоса, проверенные для русского языка.
RUSSIAN_VOICES = (
    "ru_RU-dmitri-medium",
    "ru_RU-denis-medium",
    "ru_RU-ruslan-medium",
    "ru_RU-irina-medium",
)

#: Английские голоса. high звучит заметно чище medium, но и файл крупнее.
ENGLISH_VOICES = (
    "en_US-ryan-high",
    "en_US-lessac-high",
    "en_US-amy-medium",
    "en_US-joe-medium",
    "en_US-kristin-medium",
    "en_GB-alan-medium",
)

#: Как называется каждый голос по-человечески.
VOICE_NOTES: dict[str, str] = {
    "ru_RU-dmitri-medium": "мужской, ровный",
    "ru_RU-denis-medium": "мужской, мягче",
    "ru_RU-ruslan-medium": "мужской, ниже",
    "ru_RU-irina-medium": "женский",
    "en_US-ryan-high": "мужской, американский",
    "en_US-lessac-high": "женский, самый чистый",
    "en_US-amy-medium": "женский, спокойный",
    "en_US-joe-medium": "мужской, разговорный",
    "en_US-kristin-medium": "женский, мягкий",
    "en_GB-alan-medium": "мужской, британский",
}


def _voice_url(name: str) -> str:
    """Собрать адрес голоса по его имени вида ``ru_RU-dmitri-medium``."""
    parts = name.split("-")
    if len(parts) != 3:
        raise JarvisError(
            f"Непонятное имя голоса {name!r}. Ожидается вид ru_RU-dmitri-medium. "
            f"Русские голоса: {', '.join(RUSSIAN_VOICES)}"
        )
    locale, speaker, quality = parts
    language = locale.split("_")[0]
    return f"{_REPO}/{language}/{locale}/{speaker}/{quality}/{name}"


def _download(client: httpx.Client, url: str, target: Path) -> int:
    """Скачать файл потоком, показывая прогресс для крупных файлов."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    written = 0

    with client.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        with temporary.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 16):
                handle.write(chunk)
                written += len(chunk)
                if total > 1 << 20 and written % (5 << 20) < (1 << 16):
                    print(f"    {written / 1048576:.0f} из {total / 1048576:.0f} МБ", flush=True)

    temporary.replace(target)
    return written


def download_voice(name: str, models_dir: Path) -> Path:
    """Скачать голос Piper и вернуть путь к модели.

    :param name: имя голоса, например ``ru_RU-dmitri-medium``.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    """
    base = _voice_url(name)
    model = models_dir / f"{name}.onnx"
    config = models_dir / f"{name}.onnx.json"

    if model.is_file() and config.is_file():
        print(f"Голос {name} уже на месте: {model}")
        return model

    print(f"Скачиваю голос {name} в {models_dir}")
    with httpx.Client() as client:
        try:
            size = _download(client, f"{base}.onnx", model)
            _download(client, f"{base}.onnx.json", config)
        except httpx.HTTPStatusError as exc:
            raise JarvisError(
                f"Голос {name} не найден ({exc.response.status_code}). "
                f"Доступные русские: {', '.join(RUSSIAN_VOICES)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise JarvisError(f"Сеть недоступна: {exc}") from exc

    print(f"Готово: {model} ({size / 1048576:.0f} МБ)")
    return model


#: Что произносить при прослушивании голоса.
SAMPLE_TEXT = {
    "ru": "Привет. Я Джарвис. Включаю игровой режим, в студии двадцать два градуса.",
    "en": "Hello. I am Jarvis. Switching to game mode, the studio is at twenty two degrees.",
}


def preview_voices(names: list[str], models_dir: Path, *, text: str | None = None) -> None:
    """Скачать голоса и произнести ими образец, чтобы выбрать на слух.

    :param names: имена голосов либо ``ru`` / ``en`` для всей группы.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    :param text: своя фраза вместо образца.
    """
    import sounddevice as sd
    from piper import PiperVoice, SynthesisConfig

    expanded: list[str] = []
    for name in names:
        if name == "ru":
            expanded.extend(RUSSIAN_VOICES)
        elif name == "en":
            expanded.extend(ENGLISH_VOICES)
        else:
            expanded.append(name)

    for name in expanded:
        model = download_voice(name, models_dir)
        sample = text or SAMPLE_TEXT["en" if name.startswith("en") else "ru"]

        print(f"\n▶ {name}  ({VOICE_NOTES.get(name, '')})")
        print(f"  {sample}")
        voice = PiperVoice.load(str(model))
        chunks = list(voice.synthesize(sample, syn_config=SynthesisConfig()))
        if not chunks:
            print("  (пусто)")
            continue

        audio = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        rate = int(chunks[0].sample_rate)
        with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as stream:
            stream.write(audio)

    print("\nПонравившийся впиши в config.yaml, секция tts.voices:")
    print("  voices:")
    print("    ru: ru_RU-denis-medium")
    print("    en: en_US-ryan-high")


def list_voices() -> str:
    """Текстовый список голосов для подсказки в CLI."""
    lines = ["Голоса Piper", "", "Русские:"]
    for voice in RUSSIAN_VOICES:
        lines.append(f"  {voice:<24} {VOICE_NOTES.get(voice, '')}")
    lines += ["", "Английские:"]
    for voice in ENGLISH_VOICES:
        lines.append(f"  {voice:<24} {VOICE_NOTES.get(voice, '')}")
    lines += [
        "",
        "Послушать:  python -m jarvis --try-voice ru_RU-denis-medium",
        "Все русские: python -m jarvis --try-voice ru",
        "Скачать:    python -m jarvis --download-voice ru_RU-denis-medium",
        "",
        "Выбранные голоса впиши в config.yaml, секция tts.voices.",
    ]
    return "\n".join(lines)
