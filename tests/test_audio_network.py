"""Колонка по сети: микрофон кадрами, реплика с ожиданием «доиграла», обрыв."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from urllib.parse import quote

import pytest

from jarvis.core.audio import NullAudioSource, build_audio
from jarvis.core.audio import network as network_module
from jarvis.core.audio.network import NetworkAudio, RemoteWakeWord
from jarvis.core.audio.protocol import AudioFrame
from jarvis.core.config import AudioConfig, NetworkAudioConfig, WakeWordConfig
from jarvis.core.net import OP_BINARY, OP_TEXT, unmask

TOKEN = "секрет-колонки"
RATE = 16000


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Speaker:
    """Колонка для тестов: подключается, шлёт и читает кадры."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port: int, token: str = TOKEN) -> tuple["Speaker | None", str]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET /?token={quote(token)} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode()
        if "101" not in status:
            writer.close()
            return None, status
        return cls(reader, writer), status

    async def send(self, payload: bytes, opcode: int) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        else:
            header.append(0x80 | 126)
            header += len(payload).to_bytes(2, "big")
        self.writer.write(bytes(header) + mask + unmask(payload, mask))
        await self.writer.drain()

    async def say(self, message: dict[str, object]) -> None:
        await self.send(json.dumps(message).encode(), OP_TEXT)

    async def receive(self) -> tuple[int, bytes]:
        header = await self.reader.readexactly(2)
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), "big")
        return header[0] & 0x0F, await self.reader.readexactly(length) if length else b""

    async def until(self, kind: str) -> list[tuple[int, bytes]]:
        """Читать до текстового сообщения нужного типа включительно."""
        got: list[tuple[int, bytes]] = []
        while True:
            opcode, payload = await asyncio.wait_for(self.receive(), 3)
            got.append((opcode, payload))
            if opcode == OP_TEXT and json.loads(payload).get("type") == kind:
                return got

    def close(self) -> None:
        self.writer.close()


async def _hub() -> tuple[NetworkAudio, int]:
    port = _free_port()
    hub = NetworkAudio(host="127.0.0.1", port=port, token=TOKEN, sample_rate=RATE, frame_ms=30)
    await hub.source.start()
    await hub.sink.start()
    return hub, port


async def _connected(port: int) -> Speaker:
    speaker, status = await Speaker.connect(port)
    assert speaker is not None, status
    await asyncio.sleep(0.05)
    return speaker


async def test_microphone_arrives_as_frames_of_the_configured_length() -> None:
    """Колонка шлёт куски как получится, конвейер получает ровно по 30 мс."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        frame = RATE * 30 // 1000 * 2
        await speaker.send(b"\x01\x00" * 700, OP_BINARY)  # 1400 байт: один кадр и хвост
        await speaker.send(b"\x02\x00" * 300, OP_BINARY)  # хвост дорос до второго кадра
        frames = hub.source.frames()
        first = await asyncio.wait_for(anext(frames), 1)
        second = await asyncio.wait_for(anext(frames), 1)
        assert len(first.data) == len(second.data) == frame
        assert first.sample_rate == RATE
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_wrong_microphone_rate_is_refused() -> None:
    """Звук не той частоты конвейер испортил бы молча — его не принимаем."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        await speaker.say({"type": "hello", "rate": 44100, "device": "кухня"})
        await speaker.send(b"\x01\x00" * 2000, OP_BINARY)
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(hub.source.frames()), 0.1)
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_play_waits_until_the_speaker_says_played() -> None:
    """`play` возвращается, когда колонка доиграла: на этом держится приглушение микрофона."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        audio = b"\x00\x01" * 2400  # 0.1 с при 24 кГц
        playing = asyncio.create_task(hub.sink.play(audio, sample_rate=24000))
        got = await speaker.until("end")
        assert json.loads(got[0][1]) == {"type": "play", "rate": 24000}
        assert b"".join(payload for opcode, payload in got if opcode == OP_BINARY) == audio
        await asyncio.sleep(0.05)
        assert not playing.done()  # отправлено, но ещё не доиграно
        await speaker.say({"type": "played"})
        await asyncio.wait_for(playing, 1)
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_silent_speaker_is_waited_by_the_length_of_the_sound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Колонка не сказала «доиграла» — ждём по длительности, а не вечно."""
    monkeypatch.setattr(network_module, "PLAYED_GRACE_S", 0.1)
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        await asyncio.wait_for(hub.sink.play(b"\x00\x00" * 1600, sample_rate=16000), 1)
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_interrupt_stops_sending_and_tells_the_speaker() -> None:
    """«Замолчи»: остаток реплики не уходит, колонке — `stop`."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)

        async def endless() -> AsyncIterator[bytes]:
            while True:
                yield b"\x00\x00" * 1600
                await asyncio.sleep(0.01)

        playing = asyncio.create_task(hub.sink.play_stream(endless(), sample_rate=16000))
        await asyncio.sleep(0.1)
        hub.sink.interrupt()
        await asyncio.wait_for(playing, 1)
        got = await speaker.until("stop")
        assert all(json.loads(p).get("type") != "end" for o, p in got if o == OP_TEXT)
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_play_without_a_speaker_returns_at_once() -> None:
    hub, _ = await _hub()
    try:
        await asyncio.wait_for(hub.sink.play(b"\x00\x00" * 16000, sample_rate=16000), 0.5)
    finally:
        await hub.sink.stop()


async def test_wrong_token_is_refused() -> None:
    hub, port = await _hub()
    try:
        speaker, status = await Speaker.connect(port, token="чужой")
        assert speaker is None and "403" in status
    finally:
        await hub.sink.stop()


def test_no_token_means_no_network_audio() -> None:
    """Без токена колонка не поднимается: порт смотрит в интернет."""
    stack = build_audio(AudioConfig(engine="network", network=NetworkAudioConfig(token="")))
    assert isinstance(stack.source, NullAudioSource)


def test_network_engine_builds_the_speaker() -> None:
    stack = build_audio(AudioConfig(engine="network", network=NetworkAudioConfig(token=TOKEN)))
    assert stack.source.service_name == "audio-in(колонка)"
    assert stack.sink.service_name == "audio-out(колонка)"


async def test_wake_from_the_speaker_marks_the_first_frame_after_it() -> None:
    """Имя слышит колонка: срабатывает кадр, пришедший после `wake`, а не раньше."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        frame = RATE * 30 // 1000 * 2
        await speaker.send(b"\x01\x00" * (frame // 2), OP_BINARY)  # до имени
        await asyncio.sleep(0.05)
        await speaker.say({"type": "wake"})
        await asyncio.sleep(0.05)
        await speaker.send(b"\x02\x00" * (frame // 2), OP_BINARY)  # после
        frames = hub.source.frames()
        before = await asyncio.wait_for(anext(frames), 1)
        after = await asyncio.wait_for(anext(frames), 1)
        assert not hub.wake_word.detect(before)
        assert hub.wake_word.detect(after)
        assert not hub.wake_word.detect(after), "одно имя — одно срабатывание"
        speaker.close()
    finally:
        await hub.sink.stop()


async def test_end_from_the_speaker_closes_the_phrase_with_silence() -> None:
    """Колонка замолчала: конвейеру подкладывается тишина длиннее `silence_ms`."""
    hub, port = await _hub()
    try:
        speaker = await _connected(port)
        await speaker.say({"type": "end"})
        frames = hub.source.frames()
        got = [await asyncio.wait_for(anext(frames), 1) for _ in range(hub._end_frames)]
        assert all(frame.data == bytes(len(frame.data)) for frame in got)
        assert len(got) * 30 > 800
        speaker.close()
    finally:
        await hub.sink.stop()


def test_remote_name_without_a_speaker_hears_nothing() -> None:
    """Без колонки «имя слышит колонка» — значит не слышит никто, а не «всегда»."""
    stack = build_audio(AudioConfig(engine="none", wake_word=WakeWordConfig(mode="acoustic", engine="remote")))
    assert not stack.wake_word.detect(AudioFrame(data=b"\x00\x00" * 480, sample_rate=RATE))


def test_remote_name_with_a_speaker_listens_to_it() -> None:
    config = AudioConfig(
        engine="network",
        network=NetworkAudioConfig(token=TOKEN),
        wake_word=WakeWordConfig(mode="acoustic", engine="remote"),
    )
    assert isinstance(build_audio(config).wake_word, RemoteWakeWord)
