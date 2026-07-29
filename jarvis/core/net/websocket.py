"""Минимальный WebSocket-сервер для локальных клиентов.

Нужен ровно для одного: расширение браузера должно постоянно висеть на связи
с Jarvis. Обычный HTTP тут не подходит — команды идут от Jarvis к браузеру, а
слушать порт расширение не умеет, только подключаться.

Своя реализация вместо библиотеки по двум причинам. Первая: протокол
(RFC 6455) в нужном объёме — это разбор кадров и рукопожатие, полторы сотни
строк, а каждая внешняя зависимость в голосовом ассистенте оплачивается
установкой на боевой машине. Вторая: разбор кадров — чистые функции, их видно
насквозь и проверять их можно где угодно.

**Безопасность.** Порт на 127.0.0.1 доступен любой странице в браузере: код
на сайте может открыть ``ws://127.0.0.1:8765`` и притвориться расширением.
Поэтому проверок две и обе обязательны:

* ``Origin`` — браузер подставляет его сам и подделать со страницы нельзя.
  У расширения он вида ``chrome-extension://<id>``, у сайта — его домен;
* токен, который знает только расширение. Jarvis кладёт его в файл внутри
  каталога расширения, а страницы к файлам расширения доступа не имеют.

Одной проверки мало: origin не спасёт от постороннего приложения на этой же
машине, токен — от расширения, которое кто-то поставил мимо нас.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from typing import Awaitable, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

#: Константа из RFC 6455: подмешивается к ключу клиента при рукопожатии.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Коды операций, которые нас интересуют.
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Предохранители: заголовок рукопожатия и одно сообщение.
_MAX_HANDSHAKE = 16 * 1024
_MAX_MESSAGE = 1024 * 1024


def accept_key(key: str) -> str:
    """Ответ на ключ клиента: base64 от SHA-1 ключа с константой протокола."""
    digest = hashlib.sha1(f"{key}{_GUID}".encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_handshake(raw: bytes) -> tuple[str, dict[str, str]]:
    """Разобрать HTTP-запрос апгрейда.

    :return: путь с параметрами и заголовки в нижнем регистре.
    """
    text = raw.decode("latin-1", errors="replace")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    target = parts[1] if len(parts) > 1 else "/"

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        if value:
            headers[name.strip().lower()] = value.strip()
    return target, headers


def query_token(target: str) -> str:
    """Достать токен из строки запроса: ``/?token=...``."""
    values = parse_qs(urlsplit(target).query).get("token", [])
    return values[0] if values else ""


def origin_allowed(origin: str, allowed: Iterable[str]) -> bool:
    """Разрешён ли источник соединения.

    Сравнение по началу строки: у расширения origin — это
    ``chrome-extension://<идентификатор>``, а идентификатор меняется при
    переустановке, и знать его заранее мы не можем.
    """
    prefixes = tuple(allowed)
    if not prefixes:
        return True
    return any(origin.startswith(prefix) for prefix in prefixes)


def unmask(payload: bytes, mask: bytes) -> bytes:
    """Снять маску клиента. Клиент обязан маскировать каждый кадр."""
    if not mask:
        return payload
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def encode_frame(payload: bytes, *, opcode: int = OP_TEXT) -> bytes:
    """Собрать кадр для отправки клиенту.

    Сервер маскировать не должен — в отличие от клиента, который обязан.
    """
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += length.to_bytes(2, "big")
    else:
        header.append(127)
        header += length.to_bytes(8, "big")
    return bytes(header) + payload


class _Client:
    """Одно соединение: куда писать и как закрыть."""

    def __init__(self, writer: asyncio.StreamWriter, origin: str) -> None:
        self.writer = writer
        self.origin = origin

    async def send(self, payload: bytes, *, opcode: int = OP_TEXT) -> None:
        """Отправить кадр."""
        self.writer.write(encode_frame(payload, opcode=opcode))
        await self.writer.drain()


class WebSocketServer:
    """Слушает локальный порт и держит соединения с доверенными клиентами.

    :param on_message: вызывается на каждое текстовое сообщение клиента.
    :param token: общий секрет; пустой — проверка отключена.
    :param origins: разрешённые начала ``Origin``; пусто — проверка отключена.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str = "",
        origins: Iterable[str] = (),
        on_message: Callable[[str], Awaitable[None]] | None = None,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._origins = tuple(origins)
        #: Обработчики намеренно публичные: клиент сервера обычно создаётся
        #: после него самого, и подставить их конструктором не получается.
        self.on_message = on_message
        self.on_connect = on_connect
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[_Client] = set()
        #: Задачи-обработчики всех соединений, включая ещё не прошедшие
        #: рукопожатие. Нужны для остановки: `wait_closed` с версии 3.12 ждёт
        #: завершения каждого обработчика, и один зависший на чтении клиент
        #: подвесил бы выключение всего ассистента.
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        """Есть ли хоть один живой клиент."""
        return bool(self._clients)

    @property
    def address(self) -> str:
        """Адрес для логов и подсказок в документации."""
        return f"ws://{self._host}:{self._port}"

    async def start(self) -> None:
        """Начать слушать порт."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._serve, self._host, self._port)
        logger.info("Жду расширение на %s", self.address)

    async def stop(self) -> None:
        """Закрыть порт и все соединения."""
        for client in list(self._clients):
            await self._drop(client)
        for handler in list(self._handlers):
            handler.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
            self._handlers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def send(self, text: str) -> int:
        """Разослать сообщение всем клиентам.

        :return: сколько клиентов его получили.
        """
        payload = text.encode("utf-8")
        delivered = 0
        for client in list(self._clients):
            try:
                await client.send(payload)
            except OSError as exc:
                logger.debug("Клиент отвалился при отправке: %s", exc)
                await self._drop(client)
            else:
                delivered += 1
        return delivered

    # --- соединение --------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Обслужить одно соединение от рукопожатия до закрытия."""
        client: _Client | None = None
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        try:
            client = await self._handshake(reader, writer)
            if client is None:
                return
            self._clients.add(client)
            logger.info("Расширение подключилось (%s)", client.origin or "без origin")
            if self.on_connect is not None:
                await self.on_connect()
            await self._read_loop(reader, client)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.debug("Соединение закрыто клиентом")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка в соединении с расширением")
        finally:
            if handler is not None:
                self._handlers.discard(handler)
            if client is not None:
                await self._drop(client)
                logger.info("Расширение отключилось")
            else:
                writer.close()

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> _Client | None:
        """Проверить клиента и ответить на апгрейд. ``None`` — отказ."""
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > _MAX_HANDSHAKE:
            return await self._reject(writer, "431 Request Header Fields Too Large")

        target, headers = parse_handshake(raw)
        key = headers.get("sec-websocket-key", "")
        if "websocket" not in headers.get("upgrade", "").lower() or not key:
            return await self._reject(writer, "400 Bad Request")

        origin = headers.get("origin", "")
        if not origin_allowed(origin, self._origins):
            # Самый вероятный случай — страница сайта, открывшая наш порт.
            logger.warning("Отказано в подключении: origin %r не разрешён", origin)
            return await self._reject(writer, "403 Forbidden")

        if self._token and not hmac.compare_digest(query_token(target), self._token):
            logger.warning("Отказано в подключении: неверный токен (origin %r)", origin)
            return await self._reject(writer, "403 Forbidden")

        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key(key).encode("ascii") + b"\r\n\r\n"
        )
        await writer.drain()
        return _Client(writer, origin)

    @staticmethod
    async def _reject(writer: asyncio.StreamWriter, status: str) -> None:
        """Ответить отказом и закрыть соединение."""
        writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))
        try:
            await writer.drain()
        except OSError:
            pass
        writer.close()
        return None

    async def _read_loop(self, reader: asyncio.StreamReader, client: _Client) -> None:
        """Читать кадры и собирать из них сообщения."""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = await self._read_frame(reader)

            if opcode == OP_CLOSE:
                await client.send(b"", opcode=OP_CLOSE)
                return
            if opcode == OP_PING:
                await client.send(payload, opcode=OP_PONG)
                continue
            if opcode == OP_PONG:
                continue

            parts.append(payload)
            if not fin:
                # Длинное сообщение разбито на куски — ждём последний.
                continue

            message = b"".join(parts)
            parts.clear()
            if self.on_message is not None:
                await self.on_message(message.decode("utf-8", errors="replace"))

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
        """Прочитать один кадр: признак конца сообщения, код и данные."""
        header = await reader.readexactly(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        if length > _MAX_MESSAGE:
            raise ValueError(f"кадр длиной {length} байт превышает предел")

        mask = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        return fin, opcode, unmask(payload, mask)

    async def _drop(self, client: _Client) -> None:
        """Забыть клиента и закрыть его соединение."""
        self._clients.discard(client)
        try:
            client.writer.close()
        except OSError:
            pass
