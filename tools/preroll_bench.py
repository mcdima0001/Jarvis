"""Замер: теряется ли начало фразы и помогает ли запас звука перед речью.

    python tools/preroll_bench.py                 # фразы синтезирует Fish Audio
    python tools/preroll_bench.py --preroll 0 150 300 450

Фраза синтезируется, к ней спереди добавляется секунда тихого шума (как у
микрофона в комнате), а дальше она режется **ровно так, как режет конвейер**:
с первого кадра, где Silero сказал «речь», до 800 мс тишины. Каждый вариант
запаса уходит в Deepgram, и печатается, что он расслышал.

Звук владельца тут не нужен и не пишется: синтез говорит те же слова, а
обрезка зависит от детектора, а не от голоса. Живой микрофон мягче синтеза на
первом звуке, поэтому потеря на стенде — нижняя граница настоящей.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import wave
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PHRASES = (
    "Джарвис, добавь басов.",
    "Джарвис, открой эквалайзер.",
    "Джарвис, какая погода?",
    "Джарвис, сделай тише.",
    "Джарвис, включи плейлист любимое.",
)
RATE = 16000
FRAME_MS = 30


def _env(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get(name, "")


def synthesize(text: str, voice: str) -> np.ndarray:
    """Фраза голосом Fish, моно float32 на 16 кГц."""
    reply = httpx.post(
        "https://api.fish.audio/v1/tts",
        headers={"Authorization": f"Bearer {_env('JARVIS_FISH_KEY')}", "model": "s2.1-pro-free"},
        json={"text": text, "reference_id": voice, "format": "wav"},
        timeout=60,
    )
    reply.raise_for_status()
    with wave.open(io.BytesIO(reply.content)) as file:
        rate, channels = file.getframerate(), file.getnchannels()
        samples = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    positions = np.arange(0, len(samples), rate / RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def onset(speech: np.ndarray) -> int:
    """Настоящее начало речи в синтезе: первый отсчёт громче -40 дБ от пика."""
    level = np.abs(speech)
    loud = np.nonzero(level > level.max() * 10 ** (-40 / 20))[0]
    return int(loud[0]) if len(loud) else 0


def with_room(speech: np.ndarray, rng: np.random.Generator, gain_db: float, noise_db: float) -> np.ndarray:
    """Речь с ослаблением (далеко от микрофона) и шум комнаты по всей записи."""
    speech = speech / (np.abs(speech).max() or 1) * 10 ** (gain_db / 20)
    body = np.concatenate([np.zeros(RATE), speech, np.zeros(RATE // 2)])
    return (body + rng.normal(0, 10 ** (noise_db / 20), len(body))).astype(np.float32)


def to_pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()


def cut(audio: bytes, vad: object, preroll_ms: int, truth_ms: float) -> tuple[bytes, float]:
    """Фраза, как её собирает конвейер, плюс запас спереди. Возвращает звук и
    сколько миллисекунд от начала речи детектор пропустил."""
    from jarvis.core.audio import AudioFrame

    step = RATE * FRAME_MS // 1000 * 2
    frames = [audio[i : i + step] for i in range(0, len(audio) - step + 1, step)]
    keep = preroll_ms // FRAME_MS
    start = None
    for index, frame in enumerate(frames):
        if vad.is_speech(AudioFrame(data=frame, sample_rate=RATE)):  # type: ignore[attr-defined]
            start = index
            break
    if start is None:
        return b"", 0.0
    first = max(0, start - keep)
    return b"".join(frames[first:]), start * FRAME_MS - truth_ms


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preroll", type=int, nargs="+", default=[0, 150, 300, 450])
    parser.add_argument("--voice", default="3fc339d7c6474b11a221708e2a7b1c4b")
    parser.add_argument("--gain", type=float, default=-26.0, help="пик речи, дБ: далеко от микрофона тише")
    parser.add_argument("--noise", type=float, default=-50.0, help="шум комнаты, дБ")
    parser.add_argument("--boost", type=float, default=0.0, help="цифровое усиление перед детектором и распознаванием, дБ")
    args = parser.parse_args()

    from jarvis.core.assets import ensure_vad_model
    from jarvis.core.audio.silero import SileroVAD
    from jarvis.core.config import load_config
    from jarvis.core.stt.deepgram import DeepgramSTT

    config = load_config()
    stt = DeepgramSTT(config.stt, api_key=config.stt.api_key)
    await stt.start()
    model = ensure_vad_model(config.audio.vad.models_dir)
    rng = np.random.default_rng(7)

    for phrase in PHRASES:
        speech = synthesize(phrase, args.voice)
        truth_ms = 1000 + onset(speech) * 1000 / RATE
        audio = to_pcm(with_room(speech, rng, args.gain, args.noise) * 10 ** (args.boost / 20))
        print(f"\n{phrase}")
        for preroll in args.preroll:
            vad = SileroVAD(model, sample_rate=RATE, threshold=config.audio.vad.threshold)
            piece, late = cut(audio, vad, preroll, truth_ms)
            heard = (await stt.transcribe(piece, sample_rate=RATE)).text if piece else "—"
            print(f"  запас {preroll:3d} мс (детектор опоздал на {late:.0f} мс): {heard}")
    await stt.stop()


if __name__ == "__main__":
    asyncio.run(main())
