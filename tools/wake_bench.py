"""Замер детектора имени: сколько обращений ловит и сколько раз срабатывает зря.

    python tools/wake_bench.py                  # все варианты детектора
    python tools/wake_bench.py --noise -50      # то же, с шумом комнаты

Три набора звука, и все без голоса владельца (его звук не пишется):

* **обращения** — фразы «Джарвис, …», синтез Fish, кешируются в `bench/wake/`;
* **чужая речь из жизни** — синтез тех фраз, на которых детектор ложно
  сработал в живых логах (разговоры, созвоны, песни);
* **речь самого ассистента** — все реплики из кеша синтеза (`models/tts-cache`).

Печатается доля пойманных обращений и ложные срабатывания на минуту речи.
Замер 19.09.2026 — в `docs/lessons.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.core.audio.wakeword import RIVALS  # noqa: E402
from tools.preroll_bench import RATE, synthesize  # noqa: E402

CACHE = ROOT / "bench" / "wake"

NAMED = (
    "Джарвис.",
    "Джарвис, включи музыку.",
    "Джарвис, какая погода?",
    "Джарвис, сделай громче.",
    "Джарвис, пауза.",
    "Джарвис, открой панель.",
    "Джарвис, добавь басов.",
    "Джарвис, что на экране?",
    "Джарвис, напиши Роме привет.",
    "Джарвис, как дела?",
    "Джарвис, сколько времени?",
    "Джарвис, выключи эквалайзер.",
    "Джарвис, следующий трек.",
    "Джарвис, открой браузер.",
    "Джарвис, заблокируй компьютер.",
    "Эй, Джарвис, ты тут?",
    "Джарвис, поставь таймер на десять минут.",
    "Джарвис, курс доллара.",
    "Джарвис, найди рецепт борща.",
    "Джарвис, закрой телеграм.",
)

#: Фразы, на которых детектор сработал зря в живом логе 14–18.09.2026.
LIVE_FALSE = (
    "Парыскать Арыскать дела.",
    "Какой-нибудь видосик, чтобы отдохнуть.",
    "Использовать, опять же, бустер удачи где-то на часик, наверное. Локальный бустер монет тоже где-то на часик.",
    "Она в любой игре. К тебе подходит какой-то чел, говорит, дай подержать артефакты, я попробую.",
    "Любит своего бойфренда, а ещё она тает на моих руках, мы не ждём с ней хэппи-энд.",
    "Закончится здесь.",
    "Это хорошо, со вкусом белых роз.",
    "Нет, видишь? Где второй десктоп? Какой второй десктоп? Сейчас вот смотри, где.",
    "Ладно, ладно, вы сейчас, блин, ладно.",
    "Тут реально есть другой десктоп, покинь.",
    "Насыщенная система, это фактически полный маркетплейс. То есть здесь могут регистрироваться перевозчики.",
    "Извини, если перебил, поверьте, кажется.",
    "Сначала выбирается перевозчик, машина и водитель. Эти данные у нас также подгружаются в приложении.",
    "Всё-таки получается обмен рассчитан как на перевозчика, так и на экспедитора, да? Да.",
    "Да рассказывай!",
    "Да, говорили, что за ним нет, кто-то искал и не взял такие загрузки. А звонили в процессах?",
    "Ты сидишь. Ой, тут так хорошо. Да, вот именно.",
    "Уводит, уводит, хорошо. Этот же у нас.",
    "Баса звучит, нижняя нота, как бы, нижние, ну, как, типа, аккорды, да? Да.",
    "Ты такая, ты такая красивая. Не плачь, не плачь, умоляю, мы убежим с тобой за края.",
    "Почему бы и да.",
    "Терция, она начинается как будто бы с квинты. Для гитары там, короче, свои приколы.",
)


def _cached(text: str, voice: str) -> np.ndarray:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{hashlib.sha1((voice + text).encode()).hexdigest()[:16]}.npy"
    if path.exists():
        return np.load(path)
    audio = synthesize(text, voice)
    np.save(path, audio)
    return audio


def _tts_cache() -> list[np.ndarray]:
    found = []
    for path in sorted((ROOT / "models" / "tts-cache").glob("*.wav")):
        with wave.open(str(path)) as file:
            rate, channels = file.getframerate(), file.getnchannels()
            data = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        positions = np.arange(0, len(data), rate / RATE)
        found.append(np.interp(positions, np.arange(len(data)), data).astype(np.float32))
    return found


def _room(speech: np.ndarray, rng: np.random.Generator, noise_db: float | None) -> bytes:
    body = np.concatenate([np.zeros(RATE // 2), speech / (np.abs(speech).max() or 1) * 0.3, np.zeros(RATE)])
    if noise_db is not None:
        body = body + rng.normal(0, 10 ** (noise_db / 20), len(body))
    return (np.clip(body, -1, 1) * 32767).astype(np.int16).tobytes()


class Detector:
    """Вариант детектора: грамматика (``None`` — полный словарь) и выдержка."""

    def __init__(self, model: Any, words: list[str] | None, hold_ms: float) -> None:
        from vosk import KaldiRecognizer

        self._make = lambda: (
            KaldiRecognizer(model, float(RATE), json.dumps(words, ensure_ascii=False))
            if words is not None
            else KaldiRecognizer(model, float(RATE))
        )
        self._hold = hold_ms

    def count(self, pcm: bytes) -> int:
        """Сколько раз сработал бы на этом звуке (после срабатывания — новый декодер)."""
        recognizer, held, fired, step = self._make(), 0.0, 0, RATE * 30 // 1000 * 2
        for start in range(0, len(pcm) - step + 1, step):
            recognizer.AcceptWaveform(pcm[start : start + step])
            partial = json.loads(recognizer.PartialResult()).get("partial", "")
            if "джарвис" in partial.split():
                held += 30
                if held >= self._hold:
                    fired += 1
                    recognizer, held = self._make(), 0.0
            else:
                held = 0.0
        return fired


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--voice", default="3fc339d7c6474b11a221708e2a7b1c4b")
    parser.add_argument("--noise", type=float, default=None, help="шум комнаты, дБ (по умолчанию без шума)")
    parser.add_argument("--only", default="", help="гнать только варианты, в названии которых есть эта строка")
    args = parser.parse_args()

    from vosk import Model, SetLogLevel

    from jarvis.core.assets import ensure_wakeword_model
    from jarvis.core.config import load_config

    SetLogLevel(-1)
    config = load_config().audio.wake_word
    model = Model(str(config.model or ensure_wakeword_model(config.models_dir)))
    rng = np.random.default_rng(3)

    named = [_room(_cached(text, args.voice), rng, args.noise) for text in NAMED]
    live = [_room(_cached(text, args.voice), rng, args.noise) for text in LIVE_FALSE]
    own = [_room(audio, rng, args.noise) for audio in _tts_cache()]

    def minutes(pieces: list[bytes]) -> float:
        return sum(len(piece) for piece in pieces) / 2 / RATE / 60

    variants: dict[str, Callable[[], Detector]] = {}
    base = ["джарвис", "[unk]"]
    for hold in (300, 500, 700):
        variants[f"сейчас: имя+[unk], выдержка {hold}"] = lambda hold=hold: Detector(model, base, hold)
    rivals = ["джарвис", *RIVALS, "[unk]"]
    for hold in (300, 500):
        variants[f"соперники ({len(RIVALS)} слов), выдержка {hold}"] = lambda hold=hold: Detector(model, rivals, hold)
    variants["полный словарь, выдержка 300"] = lambda: Detector(model, None, 300)

    print(f"обращений {len(named)}; чужой речи {minutes(live):.1f} мин; речи ассистента {minutes(own):.1f} мин")
    import time

    for name, make in variants.items():
        if args.only and args.only not in name:
            continue
        detector = make()
        started = time.process_time()
        caught = sum(1 for piece in named if detector.count(piece))
        false_live = sum(detector.count(piece) for piece in live)
        false_own = sum(detector.count(piece) for piece in own)
        spent = time.process_time() - started
        audio_s = (minutes(named) + minutes(live) + minutes(own)) * 60
        print(
            f"{name:40s} процессор {spent / audio_s * 100:.1f}% ядра; ловит {caught}/{len(named)}; ложных: из жизни {false_live} "
            f"({false_live / minutes(live):.1f}/мин), на ассистенте {false_own} ({false_own / minutes(own):.1f}/мин)"
        )


if __name__ == "__main__":
    main()
