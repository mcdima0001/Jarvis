"""Записать своё «Джарвис» — самые ценные примеры из всех.

Синтез даёт разнообразие голосов, а эти записи — точность: модель нужна ровно
одному человеку, с ровно одним микрофоном, в ровно одной комнате. Сотня записей
занимает минут десять и заметно поднимает попадание.

Запускать **на ноутбуке**, тем самым встроенным микрофоном, которым потом и
будешь пользоваться:

    python record_samples.py            # 100 записей в ./my_voice
    python record_samples.py 150 --device 1

Как говорить, чтобы толк был:

* по-разному. Тихо и громко, быстро и врастяжку, с интонацией вопроса и просто
  так. Модель учится по разбросу, а не по идеальному образцу;
* с разного места. Сидя, откинувшись, отвернувшись, из другого конца комнаты;
* **под музыку**. Половину записей сделай с той громкостью, на которой обычно
  слушаешь. Это ровно тот случай, ради которого всё затевается;
* не молчи в паузах специально — обычный фон комнаты полезен.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

RATE = 16000
#: Сколько писать после отсчёта. Имя укладывается в секунду, остальное — запас
#: на «не успел» и на хвост комнаты.
WINDOW_S = 1.8
#: Тише этого — значит, не сказал ничего: запись не считается.
QUIET = 900


def record(sd, numpy, device: int | None) -> "numpy.ndarray":
    """Записать одно окно с микрофона."""
    data = sd.rec(
        int(RATE * WINDOW_S), samplerate=RATE, channels=1, dtype="int16", device=device
    )
    sd.wait()
    return data.reshape(-1)


def trim(numpy, wave_: "numpy.ndarray") -> "numpy.ndarray":
    """Обрезать тишину по краям, оставив немного воздуха.

    Не косметика: openWakeWord смотрит окно фиксированной длины, и если имя
    сидит в самом конце полутора секунд тишины, учиться модель будет тишине.
    """
    loud = numpy.abs(wave_) > QUIET // 3
    if not loud.any():
        return wave_
    first, last = int(numpy.argmax(loud)), len(loud) - int(numpy.argmax(loud[::-1]))
    air = int(RATE * 0.15)
    return wave_[max(0, first - air) : min(len(wave_), last + air)]


def save(numpy, wave_: "numpy.ndarray", path: Path) -> None:
    """Записать в wav 16 бит моно."""
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(wave_.astype(numpy.int16).tobytes())


def main() -> int:
    """Отсчёт, запись, проверка — и так сколько попросили."""
    try:
        import numpy
        import sounddevice as sd
    except Exception as error:  # noqa: BLE001 — sounddevice падает и при импорте
        print(f"Нужны sounddevice и numpy ({type(error).__name__}: {error})")
        print('Поставь: pip install sounddevice numpy')
        return 2

    total = 100
    device: int | None = None
    rest = sys.argv[1:]
    if rest and rest[0].isdigit():
        total = int(rest.pop(0))
    if "--device" in rest:
        device = int(rest[rest.index("--device") + 1])

    target = Path(__file__).parent / "my_voice"
    target.mkdir(exist_ok=True)
    done = len(list(target.glob("*.wav")))

    print(f"Пишу {total} записей в {target} (уже есть {done})")
    print("Микрофон:", sd.query_devices(device, "input")["name"] if device is not None else "по умолчанию")
    print("\nПосле «говори» скажи: Джарвис. Ctrl+C — закончить.\n")
    if done == 0:
        print("Первые тридцать — в тишине. Дальше включи музыку и продолжай.\n")

    saved = 0
    try:
        while saved < total:
            number = done + saved + 1
            if saved and saved % 30 == 0:
                print("\n--- смени обстановку: громкость, поза, расстояние ---\n")
            print(f"[{number}] ", end="", flush=True)
            for count in (3, 2, 1):
                print(f"{count}… ", end="", flush=True)
                time.sleep(0.6)
            print("говори!", end="", flush=True)
            heard = record(sd, numpy, device)
            peak = int(numpy.abs(heard).max())
            if peak < QUIET:
                print(f"  тихо ({peak}), не считаю")
                continue
            cut = trim(numpy, heard)
            save(numpy, cut, target / f"jarvis_{int(time.time() * 1000)}.wav")
            saved += 1
            print(f"  ок ({len(cut) / RATE:.1f} с, пик {peak})")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nОстановился.")

    print(f"\nЗаписей всего: {len(list(target.glob('*.wav')))}")
    print("Папку my_voice целиком забирай с собой к обучающей машине.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
