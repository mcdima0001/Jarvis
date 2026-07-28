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


def list_russian_voices() -> str:
    """Текстовый список русских голосов для подсказки в CLI."""
    lines = ["Русские голоса Piper:", ""]
    for voice in RUSSIAN_VOICES:
        speaker = voice.split("-")[1]
        gender = "женский" if speaker == "irina" else "мужской"
        lines.append(f"  {voice:<24} {gender}")
    lines += ["", "Скачать:  python -m jarvis --download-voice ru_RU-dmitri-medium"]
    return "\n".join(lines)
