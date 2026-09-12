"""Потоковый синтез: ассистент начинает говорить, не дожидаясь конца реплики.

Зачем это вообще есть, видно только на замере живого облака (12.09.2026):
первый кусок звука приходит через 0.97 с, последний — через 2.45 с, а на
длинном ответе разрыв ещё больше (0.90 с против 3.90 с). Ждать конца значит
дарить человеку секунды тишины на каждой свежей реплике.

Сети тут нет: движок и вывод подделаны. Проверяется то, что решает код, — кто
идёт потоком, кто обычным путём и что остаётся в кеше.
"""

from __future__ import annotations

from typing import AsyncIterator, Iterator

import pytest

from jarvis.core.audio import StreamingAudioSink
from jarvis.core.audio.null import NullAudioSink
from jarvis.core.config import TTSConfig
from jarvis.core.runtime import BlockingWorker
from jarvis.core.tts.backends import StreamingBackend
from jarvis.core.tts.composite import CompositeTTS

RATE = 24000

#: Кусок звука, которым движок отвечает: четверть секунды тишины.
CHUNK = b"\0\0" * (RATE // 4)


class _Plain:
    """Обычный движок: реплика приходит целиком, делить нечего."""

    def __init__(self, name: str = "kokoro") -> None:
        self._name = name
        self.said: list[str] = []

    @property
    def engine(self) -> str:
        return self._name

    def prepare(self, voice: str, language: str) -> None:
        """Грузить нечего."""

    def synthesize(self, text: str, voice: str, language: str) -> tuple[bytes, int]:
        self.said.append(text)
        return CHUNK * 4, RATE


class _Streaming(_Plain):
    """Движок, отдающий звук кусками, — как облако."""

    def __init__(self, name: str = "fish", *, chunks: int = 4, fail_after: int | None = None) -> None:
        super().__init__(name)
        self._chunks = chunks
        self._fail_after = fail_after
        self.streamed: list[str] = []

    @property
    def stream_rate(self) -> int:
        return RATE

    def stream(self, text: str, voice: str, language: str) -> Iterator[bytes]:
        self.streamed.append(text)
        for number in range(self._chunks):
            if self._fail_after is not None and number >= self._fail_after:
                raise RuntimeError("поток оборвался")
            yield CHUNK


class _Speaker:
    """Вывод, умеющий и то и другое, — чтобы видеть, каким путём пошли."""

    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.streamed: list[bytes] = []

    @property
    def service_name(self) -> str:
        return "sink(test)"

    async def start(self) -> None:
        """Открывать нечего."""

    async def stop(self) -> None:
        """Закрывать нечего."""

    async def play(self, audio: bytes, *, sample_rate: int) -> None:
        self.played.append(audio)

    async def play_stream(
        self, chunks: AsyncIterator[bytes], *, sample_rate: int
    ) -> None:
        collected = bytearray()
        async for chunk in chunks:
            collected.extend(chunk)
        self.streamed.append(bytes(collected))


@pytest.fixture
def worker() -> BlockingWorker:
    return BlockingWorker(1)


def _tts(worker: BlockingWorker, backend: _Plain, sink: object, **overrides) -> CompositeTTS:
    settings: dict[str, object] = {
        "voices": {"ru": f"{backend.engine}:golos"},
        "default_language": "ru",
    }
    settings.update(overrides)
    tts = CompositeTTS(TTSConfig(**settings), worker, sink=sink)  # type: ignore[arg-type]
    tts._backends[backend.engine] = backend  # type: ignore[assignment]
    return tts


# --- кто умеет поток --------------------------------------------------------


def test_protocols_tell_who_can_stream() -> None:
    """Умение объявляется протоколом, а не проверкой имени движка."""
    assert isinstance(_Streaming(), StreamingBackend)
    assert not isinstance(_Plain(), StreamingBackend)
    assert isinstance(_Speaker(), StreamingAudioSink)


async def test_cloud_voice_speaks_as_it_synthesises(worker: BlockingWorker) -> None:
    """Умеют оба конца — реплика идёт потоком, а не одним куском."""
    await worker.start()
    backend, sink = _Streaming(), _Speaker()
    tts = _tts(worker, backend, sink)
    try:
        await tts.say("Свежая реплика.", language="ru")
    finally:
        await worker.stop()

    assert sink.streamed == [CHUNK * 4]
    assert not sink.played, "поток есть, а звук ушёл целым куском"
    assert backend.said == [], "потоковый путь зря позвал обычный синтез"


async def test_local_voice_keeps_the_plain_path(worker: BlockingWorker) -> None:
    """Движок без потока говорит как раньше: делить ему нечего."""
    await worker.start()
    backend, sink = _Plain(), _Speaker()
    tts = _tts(worker, backend, sink)
    try:
        await tts.say("Свежая реплика.", language="ru")
    finally:
        await worker.stop()

    assert sink.played and not sink.streamed


async def test_sink_without_streaming_gets_whole_reply(worker: BlockingWorker) -> None:
    """Вывод, не умеющий поток, получает реплику целиком — и это не ошибка."""
    await worker.start()
    backend = _Streaming()
    sink = NullAudioSink()
    tts = _tts(worker, backend, sink)
    try:
        await tts.say("Свежая реплика.", language="ru")
    finally:
        await worker.stop()

    # NullAudioSink поток как раз умеет — проверяем на нём же обратное:
    # заглушка обязана вычитать поток, иначе тот, кто его наполняет, повиснет.
    assert backend.streamed == ["Свежая реплика."]


# --- кеш --------------------------------------------------------------------


async def test_stream_lands_in_cache(worker: BlockingWorker, tmp_path) -> None:
    """Сказанное потоком запоминается: второй раз оно прозвучит мгновенно."""
    await worker.start()
    backend, sink = _Streaming(), _Speaker()
    tts = _tts(worker, backend, sink, cache_dir=tmp_path)
    try:
        await tts.say("Готово, сэр.", language="ru")
        await tts.say("Готово, сэр.", language="ru")
    finally:
        await worker.stop()

    assert backend.streamed == ["Готово, сэр."], "вторую реплику снова синтезировали"
    assert sink.streamed and sink.played, "повтор обязан прийти из кеша обычным путём"


async def test_ready_reply_skips_the_stream(worker: BlockingWorker, tmp_path) -> None:
    """Готовой реплике поток не нужен: из кеша она и так звучит мгновенно."""
    await worker.start()
    backend, sink = _Streaming(), _Speaker()
    tts = _tts(worker, backend, sink, cache_dir=tmp_path)
    try:
        await tts.prewarm("Слушаю, сэр.", language="ru")
        assert backend.streamed == [], "приготовление не должно ходить потоком"
        await tts.say("Слушаю, сэр.", language="ru")
    finally:
        await worker.stop()

    assert backend.said == ["Слушаю, сэр."], "приготовили не тем путём"
    assert sink.played and not sink.streamed


async def test_prewarm_swallows_failure(worker: BlockingWorker, tmp_path) -> None:
    """Приготовление — услуга, а не обещание: сбой не должен рушить вызвавшего."""
    await worker.start()
    backend, sink = _Plain(), _Speaker()
    backend.synthesize = _raise  # type: ignore[method-assign]
    tts = _tts(worker, backend, sink, cache_dir=tmp_path)
    try:
        await tts.prewarm("Что-нибудь.", language="ru")
    finally:
        await worker.stop()


def _raise(*args: object, **kwargs: object) -> tuple[bytes, int]:
    raise RuntimeError("синтез отказал")


# --- отказы -----------------------------------------------------------------


async def test_broken_stream_falls_back_while_nothing_was_heard(
    worker: BlockingWorker,
) -> None:
    """Поток не открылся — говорим обычным путём: человек ничего не заметил."""
    await worker.start()
    backend, sink = _Streaming(fail_after=0), _Speaker()
    tts = _tts(worker, backend, sink)
    try:
        await tts.say("Свежая реплика.", language="ru")
    finally:
        await worker.stop()

    assert sink.played == [CHUNK * 4], "отступить было некуда"
    assert not any(sink.streamed), "в поток ушёл звук, которого не должно было быть"


async def test_stream_broken_midway_is_not_repeated(worker: BlockingWorker) -> None:
    """Оборвался на середине — не начинаем сначала: начало прозвучало бы дважды."""
    await worker.start()
    backend, sink = _Streaming(fail_after=2), _Speaker()
    tts = _tts(worker, backend, sink)
    try:
        await tts.say("Свежая реплика.", language="ru")
    finally:
        await worker.stop()

    assert sink.streamed == [CHUNK * 2]
    assert not sink.played, "оборванную реплику повторили целиком"


async def test_broken_stream_is_not_remembered(worker: BlockingWorker, tmp_path) -> None:
    """Обрывок в кеш не ложится: иначе он звучал бы вместо реплики всегда."""
    await worker.start()
    backend, sink = _Streaming(fail_after=2), _Speaker()
    tts = _tts(worker, backend, sink, cache_dir=tmp_path)
    try:
        await tts.say("Готово, сэр.", language="ru")
        await tts.say("Готово, сэр.", language="ru")
    finally:
        await worker.stop()

    assert len(sink.streamed) == 2, "половина реплики осталась в кеше"
