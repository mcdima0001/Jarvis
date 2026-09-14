"""Маленький HTTP-сервер для панели управления.

Своя реализация по той же причине, что и WebSocket у расширения: запросов
у панели десяток видов, а каждая зависимость в голосовом ассистенте
оплачивается установкой на живой машине. Здесь только разбор запроса и отдача
ответа; что отвечать и кому можно — решает панель.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

#: Предохранители: заголовки и тело одного запроса.
MAX_HEADERS = 16 * 1024
MAX_BODY = 64 * 1024

_REASONS = {
    200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
    500: "Internal Server Error",
}


@dataclass(frozen=True, slots=True)
class Request:
    """Разобранный запрос."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""

    def json(self) -> Any:
        """Тело как JSON; пустое тело — пустой объект."""
        return json.loads(self.body.decode("utf-8")) if self.body else {}


@dataclass(slots=True)
class Response:
    """Ответ."""

    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200) -> Response:
    """Ответ JSON."""
    return Response(
        status=status,
        body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )


def parse_head(raw: bytes) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Разобрать стартовую строку и заголовки.

    :return: метод, путь без строки запроса, параметры запроса, заголовки
        в нижнем регистре.
    """
    text = raw.decode("latin-1", errors="replace")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    method = parts[0].upper() if parts else ""
    target = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        if value:
            headers[name.strip().lower()] = value.strip()
    split = urlsplit(target)
    query = {key: values[0] for key, values in parse_qs(split.query).items() if values}
    return method, split.path or "/", query, headers


def encode_response(response: Response) -> bytes:
    """Собрать ответ для отправки."""
    reason = _REASONS.get(response.status, "")
    headers = {
        "Content-Type": response.content_type,
        "Content-Length": str(len(response.body)),
        "Cache-Control": "no-store",
        # Страница панели не встраивается в чужие: иначе сайт мог бы подсунуть
        # её в рамку и заставить щёлкнуть.
        "X-Frame-Options": "DENY",
        "Connection": "close",
        **response.headers,
    }
    head = f"HTTP/1.1 {response.status} {reason}\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in headers.items()
    )
    return head.encode("utf-8") + b"\r\n" + response.body


Handler = Callable[[Request], Awaitable[Response]]


class HttpServer:
    """Слушает локальный порт; на каждый запрос — одно соединение."""

    def __init__(self, handler: Handler, *, host: str = "127.0.0.1", port: int = 8766) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Настоящий порт: при ``port=0`` его выбирает ОС."""
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def host(self) -> str:
        return self._host

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._serve, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self._respond(reader)
            writer.write(encode_response(response))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("Панель: сбой обработки запроса")
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> Response:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            return Response(status=413)
        if len(raw) > MAX_HEADERS:
            return Response(status=413)
        method, path, query, headers = parse_head(raw)
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            return Response(status=413)
        body = await reader.readexactly(length) if length else b""
        try:
            return await self._handler(Request(method, path, query, headers, body))
        except Exception as exc:  # noqa: BLE001 — сбой одного запроса не гасит панель
            logger.exception("Панель: запрос %s %s упал", method, path)
            return json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)
