"""Silero VAD — детектор речи нейросетью вместо громкости.

Зачем он вместо энергетического. Энергетический меряет громкость, а музыка
громкая: она проходит гейт целиком, набивается в буфер до предела в пятнадцать
секунд и уезжает в Whisper. Тот считает примерно в реальном времени, поэтому
при непрерывном фоне очередь занята всегда — и живая команда встаёт в её конец.
В логе это выглядит как «не успеваю распознавать», а на слух — как «ассистент
тупит под музыку».

Silero отвечает на другой вопрос: **речь ли это**, а не «громко ли это». Музыка,
щелчки клавиатуры и гул вентилятора речью не считаются, и до распознавания не
доходят вовсе. Модель весит два мегабайта и считает кадр за доли миллисекунды,
то есть платы за это никакой.

Тонкостей реализации две, и обе про формат входа.

Первая безобидная: модель берёт 512 сэмплов на 16 кГц, а кадр у нас 30 мс, то
есть 480. Пересобирать кадры приходится внутри — менять `audio.frame_ms` под
чужую модель неправильно: это настройка захвата, а не детектора. Поэтому
решение живёт между вызовами: часть кадров вывода не даёт, и `is_speech`
отвечает тем, что модель сказала в последний раз.

Вторая стоила разбора: к куску обязан быть приклеен **хвост предыдущего**
(`CONTEXT_SAMPLES`). Забудешь — модель не ругается, а тихо отвечает «речи нет»
на что угодно, и ассистент перестаёт слышать вообще.
"""

from __future__ import annotations

import array
import logging
from pathlib import Path
from typing import Any

from jarvis.core.errors import AudioError

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Сколько сэмплов модель берёт за раз. Значение не наше — так обучена сеть.
CHUNK_SAMPLES: dict[int, int] = {16000: 512, 8000: 256}

#: Сколько сэмплов **предыдущего** куска подаётся вместе с текущим.
#:
#: Это не оптимизация и не сглаживание, а часть контракта модели, и стоила она
#: целого разбора. Пятая версия Silero обучена на входе «контекст + кусок», но в
#: ONNX размер входа объявлен как [None, None]: подашь ровно 512 сэмплов — она
#: молча примет их и вернёт вероятность речи **около нуля на любом звуке**. Ни
#: ошибки, ни предупреждения; со стороны выглядит как «ассистент оглох».
CONTEXT_SAMPLES: dict[int, int] = {16000: 64, 8000: 32}

#: Порог по умолчанию, если в конфиге ноль (там ноль означает «на твоё
#: усмотрение»: у энергетического это калибровка по фону, здесь — обычный порог).
DEFAULT_THRESHOLD = 0.5

#: Насколько ниже порога должна упасть вероятность, чтобы речь считалась
#: законченной. Гистерезис нужен из-за пауз между словами: без него одна фраза
#: разваливалась бы на куски по числу вдохов.
RELEASE_RATIO = 0.7


class SileroVAD:
    """Детектор речи на модели Silero (ONNX)."""

    def __init__(
        self,
        model: Path,
        *,
        sample_rate: int = 16000,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._chunk = CHUNK_SAMPLES.get(sample_rate)
        self._context_size = CONTEXT_SAMPLES.get(sample_rate, 0)
        if self._chunk is None:
            raise AudioError(
                f"Silero VAD работает на 16 или 8 кГц, а в конфиге {sample_rate}. "
                f"Поставь audio.sample_rate: 16000 либо audio.vad.engine: energy."
            )
        if not model.is_file():
            raise AudioError(f"Нет модели Silero VAD: {model}")

        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError(
                "Для Silero VAD нужен onnxruntime: pip install -e \".[vad]\""
            ) from exc

        options = onnxruntime.SessionOptions()
        # Модель крошечная, и потоки ей только мешают: кадр считается быстрее,
        # чем раскладывается по ядрам, а ядра нужны Whisper.
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._sample_rate = sample_rate
        self._threshold = threshold if threshold > 0 else DEFAULT_THRESHOLD
        self._release = self._threshold * RELEASE_RATIO
        self._buffer = bytearray()
        self._speaking = False
        self._state: Any = None
        self._context: Any = None
        self._numpy = self._import_numpy()
        self.reset()
        logger.info(
            "Silero VAD готов: %s, порог %.2f", model.name, self._threshold
        )

    @staticmethod
    def _import_numpy() -> Any:
        """numpy приходит вместе с onnxruntime, но проверим явно."""
        try:
            import numpy
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError("Для Silero VAD нужен numpy") from exc
        return numpy

    @property
    def threshold(self) -> float:
        """Порог вероятности речи."""
        return self._threshold

    def is_speech(self, frame: AudioFrame) -> bool:
        """Есть ли речь в кадре.

        Кадр может не дать модели полного куска — тогда возвращается прежнее
        решение. Это не приблизительность: между двумя соседними кадрами по
        30 мс речь не начинается и не кончается.
        """
        self._buffer.extend(frame.data)
        window = self._chunk * 2

        while len(self._buffer) >= window:
            chunk = bytes(self._buffer[:window])
            del self._buffer[:window]
            probability = self._probability(chunk)
            if probability >= self._threshold:
                self._speaking = True
            elif probability < self._release:
                self._speaking = False

        return self._speaking

    def _probability(self, chunk: bytes) -> float:
        """Вероятность речи в куске PCM."""
        numpy = self._numpy
        samples = array.array("h")
        samples.frombytes(chunk)
        wave = numpy.array(samples, dtype=numpy.float32) / 32768.0
        # Кусок подаётся вместе с хвостом предыдущего — см. CONTEXT_SAMPLES.
        window = numpy.concatenate((self._context, wave))
        self._context = wave[-self._context_size :] if self._context_size else wave[:0]

        feed: dict[str, Any] = {"input": window.reshape(1, -1)}
        if "sr" in self._inputs:
            feed["sr"] = numpy.array(self._sample_rate, dtype=numpy.int64)
        if "state" in self._inputs:
            feed["state"] = self._state
        else:
            # Четвёртая версия держала состояние двумя тензорами.
            feed["h"], feed["c"] = self._state

        outputs = self._session.run(None, feed)
        probability = float(numpy.asarray(outputs[0]).reshape(-1)[0])
        self._state = outputs[1] if "state" in self._inputs else (outputs[1], outputs[2])
        return probability

    def reset(self) -> None:
        """Забыть накопленное и обнулить состояние сети между фразами."""
        numpy = self._numpy
        self._buffer.clear()
        self._speaking = False
        self._context = numpy.zeros(self._context_size, dtype=numpy.float32)
        if "state" in self._inputs:
            self._state = numpy.zeros((2, 1, 128), dtype=numpy.float32)
        else:
            zeros = numpy.zeros((2, 1, 64), dtype=numpy.float32)
            self._state = (zeros, zeros.copy())
