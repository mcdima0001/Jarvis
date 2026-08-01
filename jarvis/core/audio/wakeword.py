"""Активация по звуку: openWakeWord вместо разбора расшифровки.

Сейчас имя ловится по тексту: фразу целиком расшифровывает Whisper, а потом
смотрим, начата ли она с «Джарвис». Это дёшево и работает, но у такого способа
есть предел, который упирается прямо в жизнь под музыку:

* **узнать имя можно только после конца фразы.** Пока Whisper считает, команда
  уже произнесена, и приглушать музыку поздно — она попала в запись целиком;
* **в шуме Whisper врёт именно на имени.** Оно короткое, редкое и стоит первым,
  то есть в самой невыгодной позиции.

Модель активации решает обе задачи: она слушает поток кадрами, срабатывает
через доли секунды после самого слова и обучена на своём имени вместе с фоном.
Как её натаскать — в `docs/wakeword.md`; готовой модели на «Джарвис» не
существует, её делают под свой голос и свою комнату.

Модели нет — режим не включается, и ассистент продолжает жить на текстовом
гейте. Это правило общее для всех тяжёлых адаптеров: отсутствие модели ломать
запуск не должно.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jarvis.core.errors import AudioError

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Сколько сэмплов openWakeWord ждёт за раз: 80 мс на 16 кГц. Кадр захвата
#: короче (30 мс), поэтому кадры пересобираются здесь же — по той же причине,
#: что и у Silero: длина кадра принадлежит захвату, а не модели.
CHUNK_SAMPLES = 1280

#: Порог срабатывания по умолчанию. Ошибка в одну сторону — ассистент
#: откликается на постороннее слово, в другую — не откликается вовсе.
DEFAULT_THRESHOLD = 0.5


class OpenWakeWord:
    """Детектор активационной фразы по звуку."""

    def __init__(
        self,
        model: Path,
        *,
        phrase: str = "джарвис",
        sample_rate: int = 16000,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        if sample_rate != 16000:
            raise AudioError(
                f"openWakeWord работает на 16 кГц, а в конфиге {sample_rate}. "
                f"Поставь audio.sample_rate: 16000."
            )
        if not model.is_file():
            raise AudioError(
                f"Нет модели активации: {model}. Как её обучить — docs/wakeword.md, "
                f"либо верни audio.wake_word.mode: text."
            )

        try:
            from openwakeword.model import Model
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError(
                "Для активации по звуку нужен openwakeword: pip install -e \".[wakeword]\""
            ) from exc

        try:
            import numpy
        except ImportError as exc:  # pragma: no cover — зависит от установки
            raise AudioError("Для активации по звуку нужен numpy") from exc

        self._numpy = numpy
        self._model = Model(wakeword_models=[str(model)], inference_framework="onnx")
        self._phrase = phrase
        self._threshold = threshold if threshold > 0 else DEFAULT_THRESHOLD
        self._buffer = bytearray()
        self._score = 0.0
        logger.info(
            "Активация по звуку: %s, порог %.2f", model.name, self._threshold
        )

    @property
    def phrase(self) -> str:
        """Фраза активации — для логов и событий."""
        return self._phrase

    @property
    def score(self) -> float:
        """Насколько уверенно сработало в последний раз."""
        return self._score

    def detect(self, frame: AudioFrame) -> bool:
        """Прозвучало ли имя.

        Кадр короче куска модели, поэтому решение принимается не на каждом
        кадре. Ответ ``False`` означает «пока нет», а не «точно нет».
        """
        self._buffer.extend(frame.data)
        window = CHUNK_SAMPLES * 2
        heard = False

        while len(self._buffer) >= window:
            chunk = bytes(self._buffer[:window])
            del self._buffer[:window]
            samples = self._numpy.frombuffer(chunk, dtype=self._numpy.int16)
            scores: dict[str, Any] = self._model.predict(samples)
            best = max((float(value) for value in scores.values()), default=0.0)
            self._score = best
            if best >= self._threshold:
                heard = True

        return heard

    def reset(self) -> None:
        """Забыть накопленное после срабатывания.

        Без этого одно слово срабатывает несколько раз подряд: внутри у модели
        своя история, и имя остаётся в ней ещё на секунду.
        """
        self._buffer.clear()
        self._score = 0.0
        reset = getattr(self._model, "reset", None)
        if callable(reset):
            reset()
