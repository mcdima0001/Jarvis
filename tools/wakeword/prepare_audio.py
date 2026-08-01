"""Перегнать свою музыку в тот вид, который нужен обучению.

openWakeWord подмешивает фон к каждому примеру произношения, и фон этот должен
быть **твоим**: не «улица и офис» из готовых наборов, а то, что реально играет в
комнате. Отсюда и весь смысл этого скрипта — превратить обычные треки в моно
16 кГц кусками по пятнадцать секунд.

    python prepare_audio.py music music_16k

Читает mp3, flac, m4a, ogg, wav — всё, что умеет PyAV (`pip install av`). Без
него берутся только wav, и только уже 16-килогерцовые: пересчитывать частоту
самодельно нельзя, отбрасывание сэмплов даёт алиасинг, а с ним обучение будет
готовиться к шуму, которого в жизни нет.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from typing import Iterator

RATE = 16000
#: Длина куска. Пятнадцать секунд — достаточно, чтобы в кусок попал и куплет, и
#: пауза между ними, и при этом файлов получается разумное число.
CHUNK_S = 15
#: Тише этого кусок — тишина между треками, в фон её класть незачем.
QUIET = 200

SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}


def read_with_av(path: Path) -> Iterator[bytes]:
    """Прочитать что угодно и отдать моно 16 кГц кусками PCM."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            for piece in resampler.resample(frame):
                yield piece.to_ndarray().tobytes()
        for piece in resampler.resample(None):
            yield piece.to_ndarray().tobytes()


def read_wav(path: Path) -> Iterator[bytes]:
    """Запасной путь без PyAV: только wav и только нужной частоты."""
    with wave.open(str(path)) as source:
        if source.getframerate() != RATE or source.getsampwidth() != 2:
            raise ValueError(
                f"{path.name}: {source.getframerate()} Гц, а нужно {RATE}. "
                f"Пересчитать частоту без PyAV нельзя — поставь: pip install av"
            )
        channels = source.getnchannels()
        while True:
            data = source.readframes(RATE)
            if not data:
                return
            yield data if channels == 1 else _to_mono(data, channels)


def _to_mono(data: bytes, channels: int) -> bytes:
    """Свести каналы в один усреднением."""
    import array

    samples = array.array("h", data)
    mixed = array.array(
        "h",
        (
            sum(samples[at : at + channels]) // channels
            for at in range(0, len(samples) - channels + 1, channels)
        ),
    )
    return mixed.tobytes()


def loud_enough(chunk: bytes) -> bool:
    """Есть ли в куске звук вообще."""
    import array

    samples = array.array("h", chunk)
    if not samples:
        return False
    return max(abs(value) for value in samples) > QUIET


def save(chunk: bytes, path: Path) -> None:
    """Записать кусок в wav 16 бит моно."""
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(chunk)


def convert(source: Path, target: Path) -> int:
    """Перегнать все файлы из каталога. Возвращает число получившихся кусков."""
    target.mkdir(parents=True, exist_ok=True)
    try:
        import av  # noqa: F401

        reader = read_with_av
    except ImportError:
        print("PyAV не установлен — беру только wav 16 кГц (pip install av)")
        reader = read_wav

    size = RATE * CHUNK_S * 2
    written = 0
    files = sorted(item for item in source.rglob("*") if item.suffix.lower() in SUFFIXES)
    if not files:
        print(f"В {source} не нашлось ни одного звукового файла")
        return 0

    for number, path in enumerate(files, 1):
        print(f"[{number}/{len(files)}] {path.name}")
        buffer = b""
        piece = 0
        try:
            for data in reader(path):
                buffer += data
                while len(buffer) >= size:
                    chunk, buffer = buffer[:size], buffer[size:]
                    if not loud_enough(chunk):
                        continue
                    save(chunk, target / f"{path.stem[:40]}_{piece:04d}.wav")
                    piece += 1
                    written += 1
        except Exception as error:  # noqa: BLE001 — битый файл не повод падать
            print(f"  пропускаю: {type(error).__name__}: {error}")
    return written


def main() -> int:
    """Точка входа: откуда и куда."""
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    if not source.is_dir():
        print(f"Нет каталога {source}")
        return 2
    written = convert(source, target)
    minutes = written * CHUNK_S / 60
    print(f"\nГотово: {written} кусков, {minutes:.0f} минут в {target}")
    if minutes < 20:
        print("Маловато. Полчаса — нижняя граница, час заметно лучше.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
