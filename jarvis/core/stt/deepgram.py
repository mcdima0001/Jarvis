"""Распознавание речи облаком Deepgram.

**Зачем оно вообще.** Whisper на процессоре ноутбука работает медленнее
реального времени: в живом логе 28.08.2026 подряд шли «распознано 5.3 с речи за
6.5 с» и «очередь аудио переполнена, кадры отбрасываются (всего 301)». Команда
вставала в конец очереди и выполнялась с опозданием на её длину, а владелец
сказал это вслух прямо в микрофон — «толго распознаёт очень». Облако отвечает за
доли секунды и не занимает оба ядра, которых у ноутбука всего два.

**Что здесь намеренно сделано не так, как в примерах Deepgram.**

*Звук уходит в WAV, а не сырым PCM.* Сырой поток пришлось бы описывать
параметрами запроса (`encoding`, `sample_rate`, `channels`), то есть держать
согласованными две записи одного и того же — в конфиге и в строке URL. WAV
самоописателен: сорок четыре байта заголовка, и целый класс ошибок «прислали не
то, что обещали» исчезает. Для секундного фрагмента накладные расходы нулевые.

*Язык определяется по алфавиту, а не по полю ответа.* Deepgram умеет
code-switching (`language=multi`), но где именно он возвращает распознанный
язык — в документации не написано, а гадать про недокументированное поле в коде,
который потом никто не перечитает, дороже, чем посчитать буквы. Русский и
английский не пересекаются алфавитами, и `detect_language` отвечает на этот
вопрос точно. Поле из ответа всё же берётся, если оно там есть, — но опорой
служит не оно.

**Сеть — не исключение, а обычный режим.** Ошибка тут не гасится: её ждёт
`FallbackSTT`, который переключится на локальный Whisper. Поэтому таймаут
короткий: лучше через несколько секунд ответить локально, чем через полминуты
не ответить вовсе.
"""

from __future__ import annotations

import io
import logging
import wave
from typing import Any

import httpx

from jarvis.core.config import STTConfig
from jarvis.core.contracts import detect_language
from jarvis.core.errors import STTError

from .protocol import Transcript

logger = logging.getLogger(__name__)

#: Куда отправлять запись.
_URL = "https://api.deepgram.com/v1/listen"

#: Что просить у Deepgram, когда язык заранее неизвестен. Code-switching:
#: модель сама разбирается, на каком языке говорят, и не отбрасывает второй.
_MULTILINGUAL = "multi"


def to_wav(audio: bytes, sample_rate: int) -> bytes:
    """Завернуть моно-PCM 16 бит в WAV.

    Сорок четыре байта заголовка вместо трёх параметров в URL — и запись сама
    рассказывает о себе всё, что нужно.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(audio)
    return buffer.getvalue()


def read_answer(data: dict) -> tuple[str, float, str, float]:
    """Достать из ответа текст, уверенность, язык и длительность.

    Ответ разбирается **бережно**: чужой формат может измениться, а падение
    распознавания из-за переставленного ключа — это потерянная команда. Нет
    ожидаемого поля — считаем, что распознать не удалось, и говорим об этом
    пустым текстом, а не исключением.
    """
    results = data.get("results") or {}
    channels = results.get("channels") or []
    best = ((channels[0] if channels else {}).get("alternatives") or [{}])[0]

    text = str(best.get("transcript") or "").strip()
    confidence = float(best.get("confidence") or 0.0)
    duration = float((data.get("metadata") or {}).get("duration") or 0.0)
    # Поле языка Deepgram кладёт в разные места в зависимости от модели и
    # режима, а в документации его нет вовсе. Берём, если нашлось; опорой оно
    # не служит — язык всё равно перепроверяется по алфавиту.
    language = str(
        (channels[0] if channels else {}).get("detected_language")
        or best.get("language")
        or ""
    )
    return text, confidence, language, duration


class DeepgramSTT:
    """Распознавание речи через Deepgram."""

    def __init__(self, config: STTConfig, *, api_key: str) -> None:
        self._config = config
        self._key = api_key
        self._client: httpx.AsyncClient | None = None
        #: Сколько секунд звука уже отправлено. Тариф считается по времени, и
        #: расход должен быть виден так же, как у языковой модели: иначе лимит
        #: кончится незаметно, посреди вечера.
        self._seconds = 0.0
        self._requests = 0

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "stt(deepgram)"

    @property
    def ready(self) -> bool:
        """Готов ли распознавать.

        Модели тут нет, поэтому вопрос сводится к ключу: без него запрос не
        уйдёт, и притворяться готовым нельзя.
        """
        return bool(self._key)

    @property
    def spent(self) -> tuple[int, float]:
        """Сколько запросов и секунд звука ушло за сеанс."""
        return self._requests, self._seconds

    async def start(self) -> None:
        """Поднять HTTP-клиент.

        Модель не грузится, сеть не проверяется: проверка на старте всё равно
        ничего не гарантирует к моменту первой команды, а лишний круг ожидания
        стоит секунд.
        """
        if not self._key:
            raise STTError(
                "Deepgram без ключа. Задай JARVIS_DEEPGRAM_KEY в .env "
                "либо смени stt.engine на faster-whisper."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Token {self._key}"},
                timeout=self._config.timeout,
            )
        logger.info(
            "Распознавание: Deepgram %s, язык %s",
            self._config.model,
            _MULTILINGUAL if self._config.auto_detect else self._config.language,
        )

    async def stop(self) -> None:
        """Закрыть клиент и сказать, сколько потрачено."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._requests:
            logger.info(
                "Deepgram: %d запрос(ов), %.1f с звука",
                self._requests,
                self._seconds,
            )

    def _params(self) -> dict[str, Any]:
        """Параметры запроса: модель, язык, подсказка словаря, оформление.

        **Подсказка словаря нужна из-за имени, и это измерено.** При
        `language: auto` модель ищет слово сразу в двух языках, и «Джарвис»
        уходит в латиницу: в живом логе 11.09.2026 он приходил как «Darles»,
        «Harvest», «Чарльз», «Jarda». На одной и той же фразе без подсказки
        выходит «Jarvis», с подсказкой — «Джарвис». Отказываться ради этого от
        второго языка не пришлось.
        """
        params: dict[str, Any] = {
            "model": self._config.model,
            "language": (
                _MULTILINGUAL if self._config.auto_detect else self._config.language
            ),
            # Числа цифрами и знаки препинания: реплика уходит в роутер, где
            # «громкость 50» разбирается шаблоном, а «громкость пятьдесят» —
            # через словарь числительных. Первое дешевле.
            "smart_format": "true",
        }
        if self._config.keyterms:
            # Список, а не строка: httpx повторит параметр для каждого слова,
            # как того и ждёт сервис.
            params["keyterm"] = list(self._config.keyterms)
        return params

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Распознать моно-PCM 16 бит.

        Сетевые ошибки **не гасятся**: их ждёт `FallbackSTT`, который на них и
        переключается на локальное распознавание. Проглотить ошибку здесь
        значило бы превратить обрыв связи в «ассистент не расслышал».
        """
        if self._client is None:
            raise STTError("Deepgram не поднят: сперва start()")
        if not audio:
            return Transcript(text="")

        try:
            response = await self._client.post(
                _URL,
                params=self._params(),
                content=to_wav(audio, sample_rate),
                headers={"Content-Type": "audio/wav"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200]
            raise STTError(
                f"Deepgram вернул {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise STTError(f"Сеть недоступна при обращении к Deepgram: {exc}") from exc
        except ValueError as exc:
            raise STTError(f"Deepgram вернул не-JSON: {exc}") from exc

        text, confidence, language, duration = read_answer(data)
        self._requests += 1
        self._seconds += duration or len(audio) / 2 / sample_rate

        if not text:
            return Transcript(text="")
        return Transcript(
            text=text,
            # Язык по алфавиту надёжнее: см. докстринг модуля.
            language=detect_language(text, default=language or self._config.fallback_language),
            confidence=confidence,
            duration=duration,
        )
