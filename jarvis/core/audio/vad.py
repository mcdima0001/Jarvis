"""Энергетический детектор речи.

Считает громкость кадра и сравнивает с порогом. Не нейросеть, но решает главную
задачу: не гонять Whisper на тишине и шуме вентиляторов. Для студии этого
достаточно, а когда понадобится точнее — на это же место встаёт Silero,
конвейер не меняется.

Порог можно задать вручную, а можно оставить ноль — тогда первые секунды
работы уходят на замер фонового шума, и порог выставляется от него. Второе
надёжнее: в каждой комнате свой уровень фона.

Numpy намеренно не используется: VAD должен работать даже в минимальной
установке, а 480 сэмплов на кадр посчитать несложно и штатными средствами.
"""

from __future__ import annotations

import array
import logging
import math

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Во сколько раз речь должна быть громче измеренного фона.
_NOISE_FACTOR = 3.0
#: Ниже этого порога не опускаемся даже в идеальной тишине.
_MIN_THRESHOLD = 0.006


def frame_rms(frame: AudioFrame) -> float:
    """Среднеквадратичная громкость кадра, нормированная к диапазону 0..1."""
    if not frame.data:
        return 0.0
    samples = array.array("h")
    samples.frombytes(frame.data[: len(frame.data) // 2 * 2])
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples)) / 32768.0


class EnergyVAD:
    """Детектор речи по громкости с автоматической калибровкой фона."""

    def __init__(self, *, threshold: float = 0.0, calibrate_frames: int = 33) -> None:
        self._configured = threshold
        self._threshold = threshold if threshold > 0 else _MIN_THRESHOLD
        self._calibrate_frames = calibrate_frames if threshold <= 0 else 0
        self._noise: list[float] = []
        self._calibrated = threshold > 0

    @property
    def threshold(self) -> float:
        """Текущий порог срабатывания."""
        return self._threshold

    def is_speech(self, frame: AudioFrame) -> bool:
        """Есть ли речь в кадре."""
        level = frame_rms(frame)

        if not self._calibrated:
            self._noise.append(level)
            if len(self._noise) >= self._calibrate_frames:
                self._finish_calibration()
            return False

        return level >= self._threshold

    def _finish_calibration(self) -> None:
        """Выставить порог по измеренному уровню фона."""
        ordered = sorted(self._noise)
        # Медиана вместо среднего: случайный хлопок не задерёт порог.
        median = ordered[len(ordered) // 2]
        self._threshold = max(median * _NOISE_FACTOR, _MIN_THRESHOLD)
        self._calibrated = True
        logger.info(
            "VAD откалиброван: фон %.4f, порог %.4f",
            median,
            self._threshold,
        )

    def reset(self) -> None:
        """Сбросить состояние между фразами. Калибровка сохраняется."""
