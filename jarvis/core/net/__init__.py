"""Сетевые примитивы ядра: пока только локальный WebSocket-сервер."""

from .websocket import (
    OP_BINARY,
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

__all__ = [
    "OP_BINARY",
    "OP_CLOSE",
    "OP_PING",
    "OP_PONG",
    "OP_TEXT",
    "WebSocketServer",
    "accept_key",
    "encode_frame",
    "origin_allowed",
    "parse_handshake",
    "query_token",
    "unmask",
]
