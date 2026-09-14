"""Потоковое распознавание: текст готов к концу фразы, а не через секунду после.

**Зачем.** Замер 14.09.2026 на четырёх фразах, от конца фразы до текста:
обычный запрос к Deepgram — 1.24 с, прогретый — 1.23 с, поток — **0.29 с**.
Прогрев соединения не помогает: время уходит на отправку звука целиком и его
расшифровку, а не на подключение. Поток шлёт звук, пока человек ещё говорит, и
к концу фразы расшифровывать почти нечего. Качество то же: слова во всех
четырёх фразах совпали, разница в запятых.

**Приватность прежняя.** В облако уходит только сказанное после имени или в
окне ответа: конвейер копит начало фразы у себя и открывает поток лишь тогда,
когда ворота открылись, — накопленное досылается первым куском.

**Поток — умение, а не обязанность** (как `StreamingBackend` у синтеза). Не
умеет движок, облако после отказа в блокировке или поток сорвался посреди
фразы — та же фраза целиком уходит обычным путём, с его запасным Whisper.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from jarvis.core.contracts import detect_language
from jarvis.core.errors import STTError

from .protocol import Transcript

logger = logging.getLogger(__name__)


class STTStream(Protocol):
    """Одна фраза, распознаваемая по мере того, как её произносят."""

    def feed(self, audio: bytes) -> None:
        """Дослать кусок моно-PCM 16 бит. Не ждёт сети."""
        ...

    def end(self) -> None:
        """Фраза кончилась: попросить итог. Не ждёт ответа."""
        ...

    async def finish(self) -> Transcript:
        """Дождаться итогового текста. Сбой — `STTError`."""
        ...

    def cancel(self) -> None:
        """Бросить фразу: она оказалась не к ассистенту."""
        ...


@runtime_checkable
class StreamingSTT(Protocol):
    """Распознаватель, умеющий поток."""

    def open_stream(self, *, sample_rate: int = 16000) -> STTStream | None:
        """Начать фразу; ``None`` — сейчас потока нет, идти обычным путём."""
        ...


def live_query(params: Mapping[str, Any]) -> str:
    """Параметры в строку запроса; список повторяет ключ (`keyterm=a&keyterm=b`)."""
    pairs: list[tuple[str, Any]] = []
    for name, value in params.items():
        for item in value if isinstance(value, (list, tuple)) else [value]:
            pairs.append((name, item))
    return urlencode(pairs)


class DeepgramStream:
    """Фраза в потоковом распознавании Deepgram.

    Соединение открывается сразу при создании, в фоне; звук копится в очереди,
    пока оно устанавливается (около 0.8 с — а человек в это время ещё говорит).

    :param connect: чем открыть WebSocket — подменяется в тестах.
    :param on_seconds: кому сказать, сколько звука ушло, — для учёта расхода.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        sample_rate: int = 16000,
        fallback_language: str = "ru",
        connect: Callable[..., Any] | None = None,
        on_seconds: Callable[[float], None] | None = None,
    ) -> None:
        if connect is None:
            import websockets

            connect = websockets.connect
        self._connect = connect
        self._timeout = timeout
        self._rate = sample_rate
        self._fallback_language = fallback_language
        self._on_seconds = on_seconds
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finals: list[str] = []
        self._confidence: list[float] = []
        self._finalized = asyncio.Event()
        self._done = asyncio.Event()
        self._error = ""
        self._bytes = 0
        self._ended = False
        self._task = asyncio.get_running_loop().create_task(self._run(url, dict(headers)))

    def feed(self, audio: bytes) -> None:
        if audio and not self._ended and not self._done.is_set():
            self._bytes += len(audio)
            self._queue.put_nowait(audio)

    def end(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    async def finish(self) -> Transcript:
        self.end()
        try:
            await asyncio.wait_for(self._done.wait(), self._timeout)
        except TimeoutError as exc:
            self.cancel()
            raise STTError(f"поток не ответил за {self._timeout:.0f} с") from exc
        if self._error:
            raise STTError(f"поток сорвался: {self._error}")
        seconds = self._bytes / 2 / self._rate
        if self._on_seconds is not None:
            self._on_seconds(seconds)
        text = " ".join(self._finals).strip()
        if not text:
            return Transcript(text="", duration=seconds)
        return Transcript(
            text=text,
            # Язык по алфавиту, как и у обычного запроса: см. deepgram.py.
            language=detect_language(text, default=self._fallback_language),
            confidence=sum(self._confidence) / len(self._confidence) if self._confidence else 0.0,
            duration=seconds,
        )

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()

    async def _run(self, url: str, headers: dict[str, str]) -> None:
        try:
            async with self._connect(url, additional_headers=headers, open_timeout=self._timeout, close_timeout=1) as socket:
                reader = asyncio.ensure_future(self._read(socket))
                try:
                    while True:
                        chunk = await self._queue.get()
                        if chunk is None:
                            await socket.send(json.dumps({"type": "Finalize"}))
                            break
                        await socket.send(chunk)
                    await asyncio.wait_for(self._finalized.wait(), self._timeout)
                    try:
                        await socket.send(json.dumps({"type": "CloseStream"}))
                    except Exception:  # noqa: BLE001 — итог уже получен, закрытие не важно
                        pass
                finally:
                    reader.cancel()
        except asyncio.CancelledError:
            self._error = self._error or "отменён"
            raise
        except Exception as exc:  # noqa: BLE001 — сбой потока не роняет конвейер: его ждёт обычный путь
            self._error = self._error or f"{type(exc).__name__}: {exc}"
        finally:
            self._done.set()

    async def _read(self, socket: Any) -> None:
        try:
            async for message in socket:
                if isinstance(message, bytes):
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                if event.get("type") != "Results":
                    continue
                alternatives = (event.get("channel") or {}).get("alternatives") or [{}]
                best = alternatives[0] if alternatives else {}
                text = str(best.get("transcript") or "").strip()
                if event.get("is_final") and text:
                    self._finals.append(text)
                    self._confidence.append(float(best.get("confidence") or 0.0))
                if event.get("from_finalize"):
                    self._finalized.set()
                    return
            self._error = self._error or "соединение закрылось раньше итога"
        except Exception as exc:  # noqa: BLE001 — обрыв чтения превращается в понятную ошибку
            self._error = self._error or f"чтение: {exc}"
        finally:
            self._finalized.set()
