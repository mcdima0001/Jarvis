"""Выключение не зависает (живой случай 14.09.2026).

Скилл браузера закрывал сокет расширения, расширение через секунду подключалось
снова, и остановка ждала новое соединение вечно. За ней стояли все остальные
сервисы: микрофон писал в переполненную очередь, процесс не выходил.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from jarvis.core import lifecycle
from jarvis.core.lifecycle import ServiceRunner
from jarvis.core.net.websocket import WebSocketServer


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _silence(text: str) -> None:
    return None


async def test_reconnect_during_stop_does_not_hang() -> None:
    """Клиент подключается заново прямо посреди остановки — как расширение."""
    port = _free_port()
    server = WebSocketServer(port=port, on_message=_silence, origins=("chrome-extension://",))
    await server.start()
    first = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.05)
    reconnected: list[object] = []
    original_drop = server._drop

    async def drop_and_reconnect(client: object) -> None:
        await original_drop(client)  # type: ignore[arg-type]
        try:
            reconnected.append(await asyncio.open_connection("127.0.0.1", port))
        except OSError:
            reconnected.append(None)  # порт уже закрыт — ровно этого и ждём

    server._drop = drop_and_reconnect  # type: ignore[method-assign]
    # Подключение, которое успевает прийти посреди остановки.
    late = asyncio.ensure_future(asyncio.open_connection("127.0.0.1", port))
    await asyncio.sleep(0)
    await asyncio.wait_for(server.stop(), 5.0)

    for pair in [first, *[item for item in reconnected if item]]:
        pair[1].close()  # type: ignore[index]
    if late.done() and not late.exception():
        late.result()[1].close()
    else:
        late.cancel()


async def test_hanging_service_does_not_block_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "SERVICE_STOP_TIMEOUT_S", 0.1)
    stopped: list[str] = []

    class Service:
        def __init__(self, name: str, *, hangs: bool = False) -> None:
            self.service_name = name
            self._hangs = hangs

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            if self._hangs:
                await asyncio.Event().wait()
            stopped.append(self.service_name)

    runner = ServiceRunner()
    runner.add(Service("память"))
    runner.add(Service("скиллы", hangs=True))
    await runner.start_all()
    await asyncio.wait_for(runner.stop_all(), 2.0)
    assert stopped == ["память"], "зависший сервис пропущен, остальные остановлены"
