"""Колонка по сети: микрофон и динамик на ESP32, голова — здесь.

Колонка (корпус Станции Лайт, ESP32-S3) ничего не распознаёт и не решает: она
отдаёт звук микрофона и играет то, что пришло. Для конвейера это ещё один
источник и ещё один вывод, поэтому вся сеть спрятана за обычными протоколами
`AudioSource` и `StreamingAudioSink` — конвейер не знает, что микрофон в другой
квартире.

Колонка подключается сама (как расширение браузера): WebSocket с токеном
``?token=…``. Снаружи её встречает Caddy с TLS, сюда доходит уже
``ws://127.0.0.1``. Токен обязателен всегда: порт смотрит в интернет через
Caddy, и без токена микрофон в квартире мог бы подменить кто угодно.

Протокол (всё двоичное — моно PCM 16 бит, little-endian):

* колонка → Jarvis, двоичное: микрофон с частотой ``audio.sample_rate``;
* колонка → Jarvis, текст: ``{"type": "hello", "rate": 16000, "device": "…"}``
  при подключении и ``{"type": "played"}``, когда доиграла реплику до конца;
* Jarvis → колонка, текст: ``{"type": "play", "rate": 24000}`` — начало
  реплики с её частотой, затем двоичные куски, затем ``{"type": "end"}``;
  ``{"type": "stop"}`` — оборвать звучащее («замолчи»).

**`play` возвращается, когда колонка доиграла, а не когда байты ушли.** На
этом держится приглушение микрофона: конвейер глушит его на время речи плюс
хвост, и вернись `play` раньше — колонка услышала бы саму себя и выполнила
собственную реплику как команду. Колонка говорит ``played``; не сказала —
ждём по длительности звука.

**Звук уходит с опережением, а не весь сразу**: не больше `LEAD_S` впереди
того, что уже должно было прозвучать. Памяти у ESP мало, а «замолчи» должно
обрывать речь сразу, а не после буфера на десять секунд.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from jarvis.core.net import WebSocketServer

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Насколько звук может опережать воспроизведение, с.
LEAD_S = 0.5
#: Кусок реплики в одном сообщении, с. Мельче — больше накладных, крупнее —
#: колонке нужен буфер больше.
CHUNK_S = 0.1
#: Сколько ждать ``played`` сверх длительности звука: сеть и буфер колонки.
PLAYED_GRACE_S = 1.5
#: Сколько секунд микрофона держать в очереди. Конвейер отстал дольше — старое
#: выбрасывается: команда, сказанная три секунды назад, уже не новость.
QUEUE_S = 3.0


class NetworkAudio:
    """Одно соединение с колонкой на двоих: вход и выход делят сокет.

    `source` и `sink` — тонкие обёртки, их и отдают конвейеру. Порт поднимает
    тот, кого запустили первым, закрывает — первый остановленный: на выключении
    это всё равно.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        sample_rate: int,
        frame_ms: int,
    ) -> None:
        self._rate = sample_rate
        self._frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self._frames: deque[bytes] = deque(maxlen=max(1, int(QUEUE_S * 1000 / frame_ms)))
        self._arrived = asyncio.Event()
        self._buffer = bytearray()
        #: Микрофон колонки пишет не с той частотой — звук не принимаем.
        self._refused = False
        self._played = asyncio.Event()
        self._interrupted = False
        self._started = False
        self.dropped = 0
        self.device = ""
        self._server = WebSocketServer(
            host=host,
            port=port,
            token=token,
            on_message=self._on_text,
            on_binary=self._on_audio,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            peer="колонка",
        )
        self.source = NetworkSource(self)
        self.sink = NetworkSink(self)

    @property
    def connected(self) -> bool:
        """Колонка на связи."""
        return self._server.connected

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._server.start()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._server.stop()
        self._played.set()

    # --- от колонки ---------------------------------------------------------

    async def _on_connect(self) -> None:
        self._refused = False
        self._buffer.clear()

    async def _on_disconnect(self) -> None:
        # Ждать «доиграла» больше не от кого.
        self._played.set()

    async def _on_text(self, text: str) -> None:
        try:
            message: Any = json.loads(text)
        except ValueError:
            logger.debug("Колонка прислала не JSON: %.80s", text)
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "played":
            self._played.set()
        elif kind == "hello":
            self.device = str(message.get("device") or "")
            rate = message.get("rate", self._rate)
            self._refused = rate != self._rate
            if self._refused:
                logger.warning(
                    "Колонка %s пишет микрофон с частотой %s Гц, а нужна %d — звук не принимаю",
                    self.device or "без имени", rate, self._rate,
                )
            else:
                logger.info("Колонка представилась: %s, %d Гц", self.device or "без имени", self._rate)

    async def _on_audio(self, data: bytes) -> None:
        if self._refused:
            return
        self._buffer += data
        size = self._frame_bytes
        while len(self._buffer) >= size:
            if len(self._frames) == self._frames.maxlen:
                self.dropped += 1
            self._frames.append(bytes(self._buffer[:size]))
            del self._buffer[:size]
        self._arrived.set()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            while not self._frames:
                self._arrived.clear()
                await self._arrived.wait()
            yield AudioFrame(data=self._frames.popleft(), sample_rate=self._rate, timestamp=time.time())

    # --- к колонке ----------------------------------------------------------

    async def play_stream(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> None:
        if not self.connected:
            logger.debug("Колонки нет на связи — реплика не прозвучит")
            async for _ in chunks:
                pass
            return
        self._interrupted = False
        self._played.clear()
        await self._send({"type": "play", "rate": sample_rate})
        piece = max(2, int(sample_rate * CHUNK_S) * 2)
        began = time.monotonic()
        sent = 0.0
        tail = bytearray()
        async for chunk in chunks:
            tail += chunk
            while len(tail) >= piece and not self._interrupted:
                sent = await self._paced(bytes(tail[:piece]), sample_rate, began, sent)
                del tail[:piece]
            if self._interrupted:
                break
        if tail and not self._interrupted:
            sent = await self._paced(bytes(tail), sample_rate, began, sent)
        if self._interrupted:
            await self._send({"type": "stop"})
            return
        await self._send({"type": "end"})
        left = max(0.0, sent - (time.monotonic() - began))
        try:
            await asyncio.wait_for(self._played.wait(), left + PLAYED_GRACE_S)
        except TimeoutError:
            logger.debug("Колонка не сказала «доиграла» — считаю по длительности")

    async def _paced(self, data: bytes, rate: int, began: float, sent: float) -> float:
        """Отправить кусок, не уходя вперёд воспроизведения дальше `LEAD_S`."""
        ahead = sent - (time.monotonic() - began)
        if ahead > LEAD_S:
            await asyncio.sleep(ahead - LEAD_S)
        await self._server.send_binary(data)
        return sent + len(data) / 2 / rate

    def interrupt(self) -> None:
        self._interrupted = True
        self._played.set()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._server.send(json.dumps(message))


class NetworkSource:
    """Микрофон колонки."""

    def __init__(self, hub: NetworkAudio) -> None:
        self._hub = hub

    @property
    def service_name(self) -> str:
        return "audio-in(колонка)"

    async def start(self) -> None:
        await self._hub.start()

    async def stop(self) -> None:
        await self._hub.stop()

    def frames(self) -> AsyncIterator[AudioFrame]:
        return self._hub.frames()


class NetworkSink:
    """Динамик колонки."""

    def __init__(self, hub: NetworkAudio) -> None:
        self._hub = hub

    @property
    def service_name(self) -> str:
        return "audio-out(колонка)"

    async def start(self) -> None:
        await self._hub.start()

    async def stop(self) -> None:
        await self._hub.stop()

    async def play(self, audio: bytes, *, sample_rate: int) -> None:
        async def single() -> AsyncIterator[bytes]:
            yield audio

        await self._hub.play_stream(single(), sample_rate=sample_rate)

    async def play_stream(self, chunks: AsyncIterator[bytes], *, sample_rate: int) -> None:
        await self._hub.play_stream(chunks, sample_rate=sample_rate)

    def interrupt(self) -> None:
        self._hub.interrupt()
