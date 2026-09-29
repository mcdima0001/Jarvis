"""Разбор записи `record.py`: слышит ли детектор имя под музыкой — сырой и очищенный.

    python tools/aec_bench/run.py bench/aec/<запись>.npz --said 20

Печатает три вещи, и путать их нельзя:

* **сколько убрано** (дБ) — со знаком: минус значит, что фильтр портит, а не
  вычитает (так было 11.09.2026 на живой машине);
* **опора по отметкам и опора подряд** — склеенная подряд копия колонки
  уезжает, если захват молчал в тишине; разница между ними и есть вопрос,
  чинить ли выравнивание;
* **сколько раз поймано имя** на сыром микрофоне и на каждом очищенном
  варианте, с моментами — сверять со сказанным (`--said`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis.core.audio.aec import BLOCK, EchoCanceller, estimate_delay, to_pcm  # noqa: E402
from jarvis.core.audio.echo import _KEEP_MS, _MAX_MIC_DELAY_MS  # noqa: E402
from jarvis.core.audio.wakeword import RIVALS  # noqa: E402
from jarvis.core.config import load_config  # noqa: E402

STEP_MS = 30


def on_timeline(chunks: np.ndarray, sizes: np.ndarray, times: np.ndarray, *, start: float, length: int, rate: int) -> np.ndarray:
    """Разложить куски по моментам прихода: пропуски захвата остаются тишиной."""
    out = np.zeros(length, dtype=np.float32)
    at = 0
    for size, moment in zip(sizes, times, strict=True):
        piece = chunks[at : at + size]
        at += size
        end = int(round((moment - start) * rate))
        begin = end - size
        if end <= 0 or begin >= length:
            continue
        lo, hi = max(begin, 0), min(end, length)
        out[lo:hi] = piece[lo - begin : hi - begin]
    return out


def clean(mic: np.ndarray, ref: np.ndarray, *, rate: int, tail_ms: float, residual: bool) -> tuple[np.ndarray, float, int]:
    """Прогнать фильтр так же, как живой конвейер: микрофон придержан, чтобы опора шла впереди."""
    size = min(len(mic), len(ref))
    shift, _ = estimate_delay(mic[:size], ref[:size], sample_rate=rate, max_ms=2000.0)
    delay = max(0, min(int(rate * _KEEP_MS / 1000) - shift, int(rate * _MAX_MIC_DELAY_MS / 1000)))
    held = np.concatenate((np.zeros(delay, dtype=np.float32), mic))[:size]
    aec = EchoCanceller(sample_rate=rate, tail_ms=tail_ms, residual=residual)
    out = np.concatenate([aec.process(held[at : at + BLOCK], ref[at : at + BLOCK]) for at in range(0, size - BLOCK, BLOCK)])
    half = len(out) // 2
    erle = 10 * np.log10((np.mean(held[half : len(out)] ** 2) + 1e-12) / (np.mean(out[half:] ** 2) + 1e-12))
    return out, float(erle), delay


def heard(model: Any, pcm: bytes, *, rate: int, hold_ms: float) -> list[float]:
    """Когда детектор имени сработал бы — секунды от начала."""
    from vosk import KaldiRecognizer

    words = json.dumps(["джарвис", *RIVALS, "[unk]"], ensure_ascii=False)
    make = lambda: KaldiRecognizer(model, float(rate), words)  # noqa: E731
    recognizer, held, fired = make(), 0.0, []
    step = rate * STEP_MS // 1000 * 2
    for start in range(0, len(pcm) - step + 1, step):
        recognizer.AcceptWaveform(pcm[start : start + step])
        if "джарвис" in json.loads(recognizer.PartialResult()).get("partial", "").split():
            held += STEP_MS
            if held >= hold_ms:
                fired.append(start / 2 / rate)
                recognizer, held = make(), 0.0
        else:
            held = 0.0
    return fired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recording", type=Path)
    parser.add_argument("--said", type=int, default=0, help="сколько раз сказано «Джарвис»")
    args = parser.parse_args()

    data = np.load(args.recording)
    rate = int(data["rate"])
    config = load_config(ROOT / "config" / "config.yaml").audio
    mic = data["mic"].astype(np.float32)
    start = float(data["mic_times"][0]) - int(data["mic_sizes"][0]) / rate
    timeline = on_timeline(data["ref"], data["ref_sizes"], data["ref_times"], start=start, length=len(mic), rate=rate)
    glued = data["ref"].astype(np.float32)[: len(mic)]
    gaps = np.diff(data["ref_times"])
    expected = float(np.median(data["ref_sizes"])) / rate
    print(f"Запись {args.recording.name}: {len(mic) / rate:.1f} с, колонка {data['speaker']}")
    print(f"Опора: кусков {len(gaps) + 1}, пропусков дольше двух кусков {int(np.sum(gaps > 2 * expected))}, "
          f"склеенная короче отметок на {(len(mic) - len(data['ref'])) / rate:+.1f} с")
    loud = float(np.sqrt(np.mean(timeline**2)))
    print(f"Громкость: колонка {20 * np.log10(loud + 1e-9):.0f} дБ, микрофон {20 * np.log10(np.sqrt(np.mean(mic**2)) + 1e-9):.0f} дБ")

    from vosk import Model, SetLogLevel

    from jarvis.core.assets import ensure_wakeword_model

    SetLogLevel(-1)
    model = Model(str(config.wake_word.model or ensure_wakeword_model(config.wake_word.models_dir)))
    hold = config.wake_word.hold_ms

    def report(title: str, wave: np.ndarray, extra: str = "") -> None:
        times = heard(model, to_pcm(wave), rate=rate, hold_ms=hold)
        said = f" из {args.said}" if args.said else ""
        print(f"  {title:34s} имя поймано {len(times)}{said}{extra}: {', '.join(f'{t:.0f}' for t in times)}")

    print("\nДетектор имени (соперники, выдержка из конфига):")
    report("сырой микрофон", mic)
    for name, ref in (("опора по отметкам", timeline), ("опора подряд (как склеивал живой)", glued)):
        for residual in (True, False):
            cleaned, erle, delay = clean(mic, ref, rate=rate, tail_ms=config.aec.tail_ms, residual=residual)
            report(f"{name}, {'с' if residual else 'без'} подавл. остатка", cleaned,
                   f" [убрано {erle:+.1f} дБ, придержан {delay * 1000 // rate} мс]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
