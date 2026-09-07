"""Облачное распознавание и откат на местную модель.

Ни ключа, ни сети тут нет: HTTP подделан, а местная модель — счётчиком вызовов.
Проверяется то, что решает код, — как собран запрос, как разобран ответ и когда
происходит переключение.
"""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from jarvis.core.config import STTConfig
from jarvis.core.errors import STTError
from jarvis.core.stt.deepgram import DeepgramSTT, read_answer, to_wav
from jarvis.core.stt.fallback import FallbackSTT
from jarvis.core.stt.protocol import Transcript

RATE = 16000
#: Полсекунды тишины — содержимое не важно, важен размер и формат.
AUDIO = b"\0\0" * (RATE // 2)


def _answer(text: str, *, confidence: float = 0.97, duration: float = 0.5) -> dict:
    """Ответ Deepgram в том виде, в каком он приходит."""
    return {
        "metadata": {"duration": duration},
        "results": {
            "channels": [
                {"alternatives": [{"transcript": text, "confidence": confidence}]}
            ]
        },
    }


def _stt(handler, **overrides) -> DeepgramSTT:
    """Собрать распознаватель на поддельном HTTP."""
    # Ключ латиницей не для красоты: заголовки HTTP обязаны быть ASCII, и
    # кириллица в них роняет запрос ещё до отправки.
    config = STTConfig(engine="deepgram", model="nova-3", api_key="test-key", **overrides)
    stt = DeepgramSTT(config, api_key="test-key")
    stt._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Token test-key"},
    )
    return stt


# --- как собран запрос ------------------------------------------------------


def test_audio_goes_as_wav() -> None:
    """Звук уходит самоописательным файлом, а не сырым потоком.

    Сырой PCM пришлось бы описывать параметрами запроса, то есть держать
    согласованными две записи одного и того же — в конфиге и в строке URL.
    """
    data = to_wav(AUDIO, RATE)

    with wave.open(io.BytesIO(data)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == RATE
        assert handle.readframes(handle.getnframes()) == AUDIO


async def test_unknown_language_asks_for_code_switching() -> None:
    """При `language: auto` просим разбирать оба языка, а не гадаем сами.

    Иначе Deepgram распознаёт **только** названный язык и молча отбрасывает
    второй — а владелец говорит и по-русски, и по-английски.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=_answer("привет"))

    stt = _stt(handler, language="auto")
    await stt.transcribe(AUDIO, sample_rate=RATE)

    assert seen["language"] == "multi"
    assert seen["model"] == "nova-3"


async def test_named_language_is_passed_as_is() -> None:
    """Язык задан жёстко — его и просим."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=_answer("привет"))

    stt = _stt(handler, language="ru")
    await stt.transcribe(AUDIO, sample_rate=RATE)

    assert seen["language"] == "ru"


# --- как разобран ответ -----------------------------------------------------


def test_answer_is_read_gently() -> None:
    """Чужой формат может измениться, и это не повод падать.

    Потерянная команда хуже пустого ответа: на пустой ассистент переспросит,
    а на исключение промолчит.
    """
    assert read_answer({}) == ("", 0.0, "", 0.0)
    assert read_answer({"results": {"channels": []}}) == ("", 0.0, "", 0.0)


async def test_language_is_decided_by_alphabet() -> None:
    """Язык определяется по буквам, а не по полю ответа.

    Где именно Deepgram возвращает распознанный язык, в документации не
    написано, а русский и английский не пересекаются алфавитами — считать буквы
    надёжнее, чем полагаться на недокументированное поле.
    """
    stt = _stt(lambda request: httpx.Response(200, json=_answer("включи музыку")))
    russian = await stt.transcribe(AUDIO, sample_rate=RATE)

    stt = _stt(lambda request: httpx.Response(200, json=_answer("play the music")))
    english = await stt.transcribe(AUDIO, sample_rate=RATE)

    assert russian.language == "ru"
    assert english.language == "en"


async def test_seconds_are_counted() -> None:
    """Тариф считается по времени, значит и расход должен быть виден.

    Иначе лимит кончится незаметно, посреди вечера.
    """
    stt = _stt(lambda request: httpx.Response(200, json=_answer("да", duration=1.5)))

    await stt.transcribe(AUDIO, sample_rate=RATE)
    await stt.transcribe(AUDIO, sample_rate=RATE)

    assert stt.spent == (2, 3.0)


async def test_http_error_is_raised_not_swallowed() -> None:
    """Отказ облака обязан дойти до того, кто умеет переключиться.

    Проглотить его здесь значило бы превратить обрыв связи в «ассистент не
    расслышал» — и остаться без распознавания молча.
    """
    stt = _stt(lambda request: httpx.Response(401, text="no key"))

    with pytest.raises(STTError, match="401"):
        await stt.transcribe(AUDIO, sample_rate=RATE)


async def test_network_failure_is_raised() -> None:
    """Сеть пропала — это тоже отказ, а не пустая реплика."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    stt = _stt(handler)
    with pytest.raises(STTError, match="Сеть недоступна"):
        await stt.transcribe(AUDIO, sample_rate=RATE)


# --- откат на местную модель ------------------------------------------------


class _Fake:
    """Распознаватель-счётчик: отвечает заданным текстом или отказывает."""

    def __init__(self, text: str = "", *, fails: bool = False) -> None:
        self.text = text
        self.fails = fails
        self.calls = 0
        self.started = 0

    @property
    def service_name(self) -> str:
        return "fake"

    @property
    def ready(self) -> bool:
        return True

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        self.calls += 1
        if self.fails:
            raise STTError("облако молчит")
        return Transcript(text=self.text)


async def test_local_model_sleeps_until_it_is_needed() -> None:
    """Местная модель не поднимается, пока облако справляется.

    Держать её загруженной всегда значило бы потерять то, ради чего облако и
    затевалось: Whisper занимает полгигабайта и оба ядра.
    """
    cloud, local = _Fake("облако"), _Fake("местное")
    stt = FallbackSTT(cloud, local)

    await stt.start()
    result = await stt.transcribe(AUDIO)

    assert result.text == "облако"
    assert local.started == 0, "местная модель поднялась зря"


async def test_refusal_switches_to_the_local_model() -> None:
    """Облако отказало — отвечает местная модель, а не тишина."""
    cloud, local = _Fake(fails=True), _Fake("местное")
    stt = FallbackSTT(cloud, local)

    result = await stt.transcribe(AUDIO)

    assert result.text == "местное"
    assert local.started == 1


async def test_cloud_is_not_asked_again_right_away() -> None:
    """После отказа облако не трогают некоторое время.

    Иначе каждая фраза начиналась бы с ожидания таймаута, и обрыв связи
    превращался бы в «ассистент задумывается перед каждым ответом».
    """
    cloud, local = _Fake(fails=True), _Fake("местное")
    stt = FallbackSTT(cloud, local, retry_after_s=60.0)

    for _ in range(3):
        await stt.transcribe(AUDIO)

    assert cloud.calls == 1, "облако спрашивали повторно, не выждав"
    assert local.calls == 3


async def test_cloud_gets_another_chance_later() -> None:
    """Сеть вернулась — возвращаемся в облако, а не сидим на медленном."""
    cloud, local = _Fake(fails=True), _Fake("местное")
    stt = FallbackSTT(cloud, local, retry_after_s=0.0)

    await stt.transcribe(AUDIO)
    cloud.fails = False
    cloud.text = "облако"
    result = await stt.transcribe(AUDIO)

    assert result.text == "облако"
    assert cloud.calls == 2
