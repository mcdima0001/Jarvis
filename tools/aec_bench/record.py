"""Запись для стенда эхоподавления: микрофон и копия звука колонки одновременно.

Жалоба владельца 29.09.2026: «при громкой музыке приходится чуть ли не кричать,
чтобы он услышал». Эхоподавление выключено 11.09.2026 — на живой машине оно
портило сигнал (убрано −4.4 дБ). Прежде чем возвращать его хотя бы на имя, надо
знать, способен ли фильтр вычесть **эту** колонку в **этой** комнате, — и мерить
на записи, а не на слух: одна запись, сколько угодно прогонов (`run.py`).

    python tools/aec_bench/record.py --seconds 120

Пока идёт запись: музыка погромче **через колонку**, и обычным голосом, не
повышая его, «Джарвис» раз в 4–5 секунд. Сколько раз сказано — записать и
передать `run.py --said N`. Работающий Jarvis на это время выключить: он
приглушит музыку на первом же имени, и запись перестанет быть той, что нужна.

Каждый кусок пишется с моментом прихода (`time.monotonic`): петлевой захват
Windows в тишине молчит, а не отдаёт нули, и склеенная подряд опора уезжает от
микрофона — по отметкам это видно, а по склейке нет.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis.core.audio.aec import BLOCK, to_float  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402

OUT = ROOT / "bench" / "aec"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--name", default="", help="имя записи; по умолчанию дата и время")
    args = parser.parse_args()

    audio = load_config(ROOT / "config" / "config.yaml").audio
    rate = audio.sample_rate

    from jarvis.core.audio.devices import _import_sounddevice
    from jarvis.core.audio.loopback import LoopbackSource

    mic_chunks: list[np.ndarray] = []
    mic_times: list[float] = []
    ref_chunks: list[np.ndarray] = []
    ref_times: list[float] = []

    def on_reference(block: np.ndarray) -> None:
        ref_times.append(time.monotonic())
        ref_chunks.append(np.asarray(block, dtype=np.float32))

    capture = LoopbackSource(
        sample_rate=rate,
        device=None if audio.aec.reference in ("auto", "", "off", "none") else audio.aec.reference,
        on_audio=on_reference,
    )
    if not capture.start():
        print(f"Копия звука колонки не открылась: {capture.failure}")
        return 1

    sd = _import_sounddevice()

    def on_mic(data: object, frames: int, info: object, status: object) -> None:
        mic_times.append(time.monotonic())
        mic_chunks.append(to_float(bytes(data)).astype(np.float32))  # type: ignore[call-overload]

    stream = sd.RawInputStream(
        samplerate=rate, blocksize=BLOCK, device=audio.input_device, channels=1, dtype="int16", callback=on_mic,
    )
    print(f"Колонка: {capture.name}")
    print(f"Микрофон: {audio.input_device or 'по умолчанию'}")
    print(f"Пишу {args.seconds:.0f} с. Музыка погромче, «Джарвис» обычным голосом раз в 4–5 секунд.")
    started = time.monotonic()
    stream.start()
    try:
        told = int(args.seconds)
        while (left := args.seconds - (time.monotonic() - started)) > 0:
            time.sleep(min(1.0, left))
            if left - 1 < told - 15:
                told -= 15
                print(f"  осталось {max(0, told)} с", flush=True)
    except KeyboardInterrupt:
        print("Остановлено руками — сохраняю, что есть.")
    stream.stop()
    stream.close()
    capture.stop()

    if not mic_chunks or not ref_chunks:
        print(f"Пусто: микрофон {len(mic_chunks)} кусков, колонка {len(ref_chunks)} — сохранять нечего.")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.name or time.strftime('%Y%m%d-%H%M%S')}.npz"
    np.savez_compressed(
        path,
        rate=rate,
        mic=np.concatenate(mic_chunks), mic_sizes=np.array([len(c) for c in mic_chunks]), mic_times=np.array(mic_times),
        ref=np.concatenate(ref_chunks), ref_sizes=np.array([len(c) for c in ref_chunks]), ref_times=np.array(ref_times),
        speaker=np.array(capture.name),
    )
    mic_s, ref_s = sum(len(c) for c in mic_chunks) / rate, sum(len(c) for c in ref_chunks) / rate
    print(f"Сохранено: {path}  (микрофон {mic_s:.1f} с, колонка {ref_s:.1f} с за {time.monotonic() - started:.1f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
