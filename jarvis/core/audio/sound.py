"""Загрузка коротких звуков — отклик на активацию и подобное.

Вынесено отдельно, чтобы формат файла не протекал в голосовой конвейер: тому
всё равно, лежит на диске WAV или MP3, ему нужен моно-PCM 16 бит и частота.

WAV читается стандартной библиотекой, остальное — через PyAV, если он
установлен. Отсутствие файла или кодека не считается ошибкой: звук
необязателен, ассистент работает и без него.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

#: Частота, к которой приводим сжатые форматы.
_TARGET_RATE = 24000


#: Длиннее этого отклик мешает: всё время звучания микрофон заглушён.
_LONG_SOUND_S = 2.0


def load_sound(path: Path) -> tuple[bytes, int] | None:
    """Прочитать звуковой файл в моно-PCM 16 бит.

    Тишина по краям срезается. Это не косметика: пока играет отклик, микрофон
    заглушён, и хвост тишины превращается в паузу, когда ассистент уже не
    слушает. В `activation.mp3` из корня проекта звук занимает 0.8 секунды из
    четырёх — остальное как раз такая тишина.

    :param path: путь к файлу; поддерживаются WAV, а с PyAV — и MP3, OGG, M4A.
    :return: пара «данные» и «частота», либо ``None``, если прочитать нечем.
    """
    if not path.is_file():
        logger.warning("Звуковой файл не найден: %s", path)
        return None

    loaded = _load_wav(path) if path.suffix.lower() == ".wav" else _decode(path)
    if loaded is None:
        return None

    audio, rate = loaded
    trimmed = trim_silence(audio, rate)
    if len(trimmed) != len(audio):
        logger.debug(
            "%s: обрезана тишина, %.2f с -> %.2f с",
            path.name,
            len(audio) / 2 / rate,
            len(trimmed) / 2 / rate,
        )
    if not trimmed:
        logger.warning("В файле %s одна тишина", path.name)
        return None

    seconds = len(trimmed) / 2 / rate
    if seconds > _LONG_SOUND_S:
        logger.warning(
            "Отклик %s длится %.1f с — всё это время микрофон не слушает. "
            "Для отклика лучше звук короче секунды.",
            path.name,
            seconds,
        )
    return trimmed, rate


def trim_silence(audio: bytes, rate: int, *, margin_ms: int = 30) -> bytes:
    """Срезать тишину в начале и конце.

    Порог берётся от собственного пика записи, а не абсолютный: тихий звук
    иначе срезался бы целиком.

    :param audio: моно-PCM 16 бит.
    :param rate: частота дискретизации.
    :param margin_ms: сколько тишины оставить по краям, чтобы не щёлкало.
    """
    import array

    samples = array.array("h")
    samples.frombytes(audio[: len(audio) // 2 * 2])
    if not samples:
        return audio

    window = max(1, rate // 100)  # 10 мс
    levels = [
        max(abs(value) for value in samples[start : start + window])
        for start in range(0, len(samples), window)
    ]
    peak = max(levels)
    if peak == 0:
        return b""

    threshold = max(peak // 50, 32)
    loud = [index for index, level in enumerate(levels) if level >= threshold]
    if not loud:
        return b""

    margin = max(1, margin_ms * rate // 1000 // window)
    first = max(0, loud[0] - margin) * window
    last = min(len(samples), (loud[-1] + 1 + margin) * window)
    return samples[first:last].tobytes()


def _load_wav(path: Path) -> tuple[bytes, int] | None:
    """Прочитать WAV; стерео и разрядность отличную от 16 бит — через PyAV."""
    try:
        with wave.open(str(path)) as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return _decode(path)
            return handle.readframes(handle.getnframes()), handle.getframerate()
    except (OSError, wave.Error) as exc:
        logger.warning("Не удалось прочитать %s: %s", path.name, exc)
        return None


def _decode(path: Path) -> tuple[bytes, int] | None:
    """Раскодировать сжатый формат в моно-PCM 16 бит."""
    try:
        import av
    except ImportError:
        logger.warning(
            "Чтобы играть %s, нужен пакет av: pip install -e '.[edge]'. "
            "Либо положи рядом WAV моно 16 бит.",
            path.name,
        )
        return None

    try:
        container = av.open(str(path))
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=_TARGET_RATE
        )
        pcm = bytearray()
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                # У кадра ровно один план (моно); буфер бывает выровнен с
                # запасом, поэтому режем по числу сэмплов.
                pcm += bytes(resampled.planes[0])[: resampled.samples * 2]
    except Exception as exc:  # noqa: BLE001 — сбой кодека не должен ронять запуск
        logger.warning("Не удалось раскодировать %s: %s: %s", path.name, type(exc).__name__, exc)
        return None

    if not pcm:
        logger.warning("В файле %s не нашлось звука", path.name)
        return None
    return bytes(pcm), _TARGET_RATE
