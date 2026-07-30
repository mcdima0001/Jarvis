"""WebSocket-сервер: разбор кадров, рукопожатие и защита порта.

Порт на 127.0.0.1 открыт любой странице в браузере, поэтому проверки origin и
токена — не формальность, а единственное, что отделяет расширение от кода на
случайном сайте. Тесты поднимают настоящий сервер и ходят в него настоящим
клиентом: рукопожатие и маскирование кадров ошибиться не дают.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from jarvis.core.net import (
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WebSocketServer,
    accept_key,
    encode_frame,
    origin_allowed,
    parse_handshake,
    query_token,
    unmask,
)

EXTENSION = "chrome-extension://abcdefghijklmnop"
ORIGINS = ("chrome-extension://", "moz-extension://")


# --- чистые функции ---------------------------------------------------------


def test_accept_key_matches_the_rfc_example() -> None:
    """Пример из RFC 6455: ключ клиента и ожидаемый ответ."""
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_parsed() -> None:
    """Из запроса апгрейда нужны путь с токеном и заголовки."""
    raw = (
        b"GET /?token=secret HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8765\r\n"
        b"Upgrade: websocket\r\n"
        b"Origin: chrome-extension://abc\r\n\r\n"
    )
    target, headers = parse_handshake(raw)

    assert target == "/?token=secret"
    assert headers["upgrade"] == "websocket"
    assert headers["origin"] == "chrome-extension://abc"
    assert query_token(target) == "secret"


@pytest.mark.parametrize(
    "origin, allowed",
    [
        ("chrome-extension://abc", True),
        ("moz-extension://abc", True),
        ("https://evil.example", False),
        ("", False),
        ("chrome-extension", False),
    ],
)
def test_origin_checked_by_prefix(origin: str, allowed: bool) -> None:
    """Идентификатор расширения меняется при переустановке, схема — нет."""
    assert origin_allowed(origin, ORIGINS) is allowed


def test_mask_is_symmetric() -> None:
    """Маска снимается тем же XOR, которым накладывалась."""
    payload = "привет, мир".encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    assert unmask(unmask(payload, mask), mask) == payload


@pytest.mark.parametrize("size", [0, 5, 125, 126, 200, 65535, 65536])
def test_frame_lengths_encoded(size: int) -> None:
    """Длина пишется тремя способами, и граничные значения — самое интересное."""
    frame = encode_frame(b"x" * size)
    header = frame[1] & 0x7F

    assert frame[0] == 0x80 | OP_TEXT
    assert not frame[1] & 0x80, "сервер не должен маскировать"
    if size < 126:
        assert header == size and len(frame) == size + 2
    elif size < 65536:
        assert header == 126 and len(frame) == size + 4
    else:
        assert header == 127 and len(frame) == size + 10


# --- настоящее соединение ---------------------------------------------------


class Client:
    """Клиент, который ведёт себя как браузер: маскирует кадры и шлёт origin."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(
        cls, port: int, *, origin: str = EXTENSION, token: str = ""
    ) -> tuple["Client | None", str]:
        """Подключиться. Возвращает клиента и строку статуса ответа."""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        target = f"/?token={token}" if token else "/"
        writer.write(
            f"GET {target} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Origin: {origin}\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode("ascii")
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode("ascii")
        if "101" not in status:
            writer.close()
            return None, status
        return cls(reader, writer), status

    async def send(self, text: str, *, opcode: int = OP_TEXT) -> None:
        """Отправить кадр с маской — клиент обязан маскировать."""
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        else:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        self.writer.write(bytes(header) + mask + unmask(payload, mask))
        await self.writer.drain()

    async def receive(self) -> tuple[int, bytes]:
        """Прочитать ответ сервера (он приходит без маски)."""
        header = await self.reader.readexactly(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        payload = await self.reader.readexactly(length) if length else b""
        return opcode, payload

    async def close(self) -> None:
        """Закрыть соединение."""
        self.writer.close()


async def _serve(**kwargs) -> tuple[WebSocketServer, int, list[str]]:
    """Поднять сервер на свободном порту и собирать полученные сообщения."""
    received: list[str] = []

    async def collect(text: str) -> None:
        received.append(text)

    port = _free_port()
    server = WebSocketServer(port=port, on_message=collect, origins=ORIGINS, **kwargs)
    await server.start()
    return server, port, received


def _free_port() -> int:
    """Занять и сразу отпустить порт, чтобы узнать свободный номер."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_extension_connects_and_exchanges_messages() -> None:
    """Полный круг: рукопожатие, сообщение туда и обратно."""
    server, port, received = await _serve()
    try:
        client, _ = await Client.connect(port)
        assert client is not None

        await client.send(json.dumps({"event": "hello"}))
        await asyncio.sleep(0.05)
        assert received == ['{"event": "hello"}']

        assert await server.send('{"action": "tabs"}') == 1
        opcode, payload = await client.receive()
        assert opcode == OP_TEXT
        assert json.loads(payload) == {"action": "tabs"}
        await client.close()
    finally:
        await server.stop()


async def test_command_goes_to_one_client_only() -> None:
    """Команда — приказ, а не новость: разослать её веером нельзя.

    Живой случай: расширение подключилось дважды (служебный поток успевал
    открыть второй сокет, пока первый ещё договаривался), и каждая команда
    выполнялась дважды — «открой вкладку» открывало две одинаковые вкладки.
    """
    server, port, _ = await _serve()
    try:
        first, _ = await Client.connect(port, origin="moz-extension://one")
        second, _ = await Client.connect(port, origin="moz-extension://two")
        assert first is not None and second is not None
        await asyncio.sleep(0.05)

        assert await server.send('{"action": "tabs"}') == 1
        # Отвечает последний подключившийся: старое соединение вполне могло
        # остаться от уснувшего служебного потока.
        opcode, payload = await second.receive()
        assert opcode == OP_TEXT
        assert json.loads(payload) == {"action": "tabs"}

        await first.close()
        await second.close()
    finally:
        await server.stop()


async def test_second_connection_of_the_same_extension_replaces_the_first() -> None:
    """Одно расширение — одно соединение: прежнее закрывается само."""
    server, port, _ = await _serve()
    try:
        first, _ = await Client.connect(port)
        second, _ = await Client.connect(port)
        assert first is not None and second is not None
        await asyncio.sleep(0.05)

        assert await server.send('{"action": "tabs"}') == 1
        opcode, _ = await second.receive()
        assert opcode == OP_TEXT

        await second.close()
        await asyncio.sleep(0.05)
        # Первое соединение сервер закрыл сам, поэтому живых клиентов не осталось.
        assert not server.connected
        await first.close()
    finally:
        await server.stop()


async def test_page_from_a_website_is_refused() -> None:
    """Страница сайта тоже может открыть локальный порт — и получает отказ.

    Origin браузер проставляет сам, подделать его со страницы нельзя. Это
    главная защита: без неё любой сайт управлял бы браузером через Jarvis.
    """
    server, port, _ = await _serve()
    try:
        client, status = await Client.connect(port, origin="https://evil.example")
        assert client is None
        assert "403" in status
    finally:
        await server.stop()


async def test_wrong_token_is_refused() -> None:
    """Токен отсекает чужое расширение, которому origin ничего не стоит."""
    server, port, _ = await _serve(token="right-token")
    try:
        client, status = await Client.connect(port, token="wrong-token")
        assert client is None
        assert "403" in status

        allowed, status = await Client.connect(port, token="right-token")
        assert allowed is not None
        await allowed.close()
    finally:
        await server.stop()


async def test_ping_is_answered() -> None:
    """Расширение подтверждает жизнь пингом — сервер обязан отвечать понгом."""
    server, port, _ = await _serve()
    try:
        client, _ = await Client.connect(port)
        assert client is not None
        await client.send("проверка", opcode=OP_PING)
        opcode, payload = await client.receive()
        assert opcode == OP_PONG
        assert payload.decode("utf-8") == "проверка"
        await client.close()
    finally:
        await server.stop()


async def test_disconnect_is_noticed() -> None:
    """После обрыва сервер не должен считать клиента живым."""
    server, port, _ = await _serve()
    try:
        client, _ = await Client.connect(port)
        assert client is not None
        await asyncio.sleep(0.05)
        assert server.connected

        await client.send("", opcode=OP_CLOSE)
        await asyncio.sleep(0.05)
        assert not server.connected
        assert await server.send("никому") == 0
    finally:
        await server.stop()


async def test_long_message_survives_fragmentation() -> None:
    """Длинные сообщения приходят по кускам и собираются обратно."""
    server, port, received = await _serve()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            f"Origin: {EXTENSION}\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n".encode("ascii")
        )
        await reader.readuntil(b"\r\n\r\n")

        mask = b"\x01\x02\x03\x04"
        pieces = (
            (True, False, "нача".encode("utf-8")),
            (False, True, "ло".encode("utf-8")),
        )
        for first, fin, chunk in pieces:
            opcode = OP_TEXT if first else 0x0
            header = bytes([(0x80 if fin else 0x00) | opcode, 0x80 | len(chunk)])
            writer.write(header + mask + unmask(chunk, mask))
        await writer.drain()
        await asyncio.sleep(0.05)

        assert received == ["начало"]
        writer.close()
    finally:
        await server.stop()


def test_base64_key_is_valid() -> None:
    """Ответ на ключ — корректный base64 от двадцати байт SHA-1."""
    assert len(base64.b64decode(accept_key("dGhlIHNhbXBsZSBub25jZQ=="))) == 20
