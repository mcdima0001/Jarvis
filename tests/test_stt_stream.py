"""Потоковое распознавание: на локальном сервере, который отвечает как Deepgram.

Сети тут нет: WebSocket поднимается на 127.0.0.1 и ведёт себя по протоколу
Deepgram — копит звук, на `Finalize` отдаёт итог с `from_finalize`.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

pytest.importorskip("websockets")
import websockets  # noqa: E402

from jarvis.core.config import STTConfig  # noqa: E402
from jarvis.core.errors import STTError  # noqa: E402
from jarvis.core.stt import FallbackSTT  # noqa: E402
from jarvis.core.stt.deepgram import DeepgramSTT  # noqa: E402
from jarvis.core.stt.stream import DeepgramStream, live_query  # noqa: E402

RATE = 16000


async def _server(behaviour: str = "answer") -> tuple[Any, str, dict[str, Any]]:
    seen: dict[str, Any] = {"audio": bytearray(), "control": [], "headers": {}, "path": ""}

    async def handler(connection: Any) -> None:
        # Имена заголовков websockets хранит маленькими буквами.
        seen["headers"] = {name.lower(): value for name, value in connection.request.headers.raw_items()}
        seen["path"] = connection.request.path
        async for message in connection:
            if isinstance(message, bytes):
                seen["audio"].extend(message)
                continue
            kind = json.loads(message)["type"]
            seen["control"].append(kind)
            if kind == "Finalize" and behaviour == "answer":
                await connection.send(json.dumps({
                    "type": "Results", "is_final": True,
                    "channel": {"alternatives": [{"transcript": "Джарвис, включи свет", "confidence": 0.9}]},
                }))
                await connection.send(json.dumps({
                    "type": "Results", "is_final": True, "from_finalize": True,
                    "channel": {"alternatives": [{"transcript": "", "confidence": 0.0}]},
                }))
            if kind == "CloseStream":
                return

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/v1/listen", seen


async def test_stream_returns_the_final_text_and_counts_seconds() -> None:
    server, url, seen = await _server()
    counted: list[float] = []
    try:
        stream = DeepgramStream(url, headers={"Authorization": "Token k"}, timeout=3, sample_rate=RATE, on_seconds=counted.append)
        stream.feed(b"\x01\x00" * RATE)  # секунда звука
        stream.feed(b"\x02\x00" * (RATE // 2))
        stream.end()
        transcript = await stream.finish()
    finally:
        server.close()
        await server.wait_closed()
    assert transcript.text == "Джарвис, включи свет" and transcript.language == "ru"
    assert len(seen["audio"]) == RATE * 3, "звук дошёл весь и в том порядке"
    assert seen["control"] == ["Finalize", "CloseStream"]
    assert counted == [1.5] and transcript.duration == 1.5


async def test_silent_server_is_an_error_not_a_hang() -> None:
    server, url, _ = await _server(behaviour="silent")
    try:
        stream = DeepgramStream(url, headers={}, timeout=0.3, sample_rate=RATE)
        stream.feed(b"\x00\x00" * 100)
        with pytest.raises(STTError):
            await asyncio.wait_for(stream.finish(), 2)
    finally:
        server.close()
        await server.wait_closed()


async def test_unreachable_server_is_an_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stream = DeepgramStream(f"ws://127.0.0.1:{port}/", headers={}, timeout=1, sample_rate=RATE)
    stream.feed(b"\x00\x00" * 100)
    with pytest.raises(STTError):
        await asyncio.wait_for(stream.finish(), 3)


def test_query_repeats_list_parameters() -> None:
    assert live_query({"model": "nova-3", "keyterm": ["Джарвис", "OBS"]}) == (
        "model=nova-3&keyterm=%D0%94%D0%B6%D0%B0%D1%80%D0%B2%D0%B8%D1%81&keyterm=OBS"
    )


async def test_deepgram_opens_the_stream_with_its_request_parameters() -> None:
    server, url, seen = await _server()
    try:
        config = STTConfig(engine="deepgram", model="nova-3", language="auto", keyterms=("Джарвис",), timeout=3)
        stt = DeepgramSTT(config, api_key="secret", live_url=url)
        stream = stt.open_stream(sample_rate=RATE)
        assert stream is not None
        stream.feed(b"\x00\x00" * RATE)
        transcript = await stream.finish()
    finally:
        server.close()
        await server.wait_closed()
    assert transcript.text
    assert seen["headers"].get("authorization") == "Token secret"
    for part in ("model=nova-3", "language=multi", "encoding=linear16", "sample_rate=16000", "keyterm="):
        assert part in seen["path"], part
    assert stt.spent == (1, 1.0)


def test_streaming_can_be_switched_off() -> None:
    stt = DeepgramSTT(STTConfig(engine="deepgram", streaming=False), api_key="k")
    assert stt.open_stream() is None


class _Primary:
    service_name = "cloud"
    ready = True

    def __init__(self) -> None:
        self.opened = 0

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Any:
        raise STTError("нет сети")

    def open_stream(self, *, sample_rate: int = 16000) -> Any:
        self.opened += 1
        return object()


def test_fallback_gives_no_stream_while_the_cloud_is_blocked() -> None:
    primary = _Primary()
    fallback = FallbackSTT(primary, _Primary())
    assert fallback.open_stream() is not None and primary.opened == 1
    fallback._blocked_until = 10**12
    assert fallback.open_stream() is None, "после отказа облака — обычным путём"
