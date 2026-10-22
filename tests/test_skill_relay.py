"""Прокси для ноутбука: разбор CONNECT, кого пускать, куда пускать, туннель целиком."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load() -> Any:
    path = Path(__file__).resolve().parent.parent / "skills" / "relay" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_relay_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


relay = _load()


def test_connect_is_parsed_and_everything_else_is_not() -> None:
    assert relay.parse_connect(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: x\r\n\r\n") == relay.Target(
        "api.openai.com", 443
    )
    assert relay.parse_connect(b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\n") == relay.Target("2001:db8::1", 443)
    assert relay.parse_connect(b"GET http://example.com/ HTTP/1.1\r\n\r\n") is None
    assert relay.parse_connect(b"CONNECT example.com HTTP/1.1\r\n\r\n") is None
    assert relay.parse_connect(b"\xff\xfe\r\n\r\n") is None


def test_only_tailscale_clients_by_default() -> None:
    allow = relay.networks(["100.64.0.0/10", "не сеть"])
    assert relay.allowed_peer("100.101.1.2", allow)
    assert not relay.allowed_peer("8.8.8.8", allow)
    assert not relay.allowed_peer("127.0.0.1", allow)


def test_tunnel_goes_only_to_the_internet() -> None:
    """Иначе через прокси можно постучаться в сам сервер и в его внутреннюю сеть."""
    assert relay.public("104.18.33.45")
    assert not relay.public("127.0.0.1")
    assert not relay.public("100.98.141.99")
    assert not relay.public("192.168.1.1")
    assert not relay.public("::1")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _started(**settings: Any) -> Any:
    skill = relay.RelaySkill()
    values = {"enabled": True, "host": "127.0.0.1", "port": _free_port(), "allow": ["127.0.0.0/8"], **settings}
    skill._context = SimpleNamespace(setting=lambda key, default=None: values.get(key, default))
    await skill.on_setup()
    await skill.on_start()
    return skill


async def _ask(port: int, request: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    return reader, writer, head


async def test_tunnel_carries_bytes_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сквозной круг: CONNECT, 200, байты туда и обратно."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"pong:" + await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    async def loopback(target: Any) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(relay, "_resolve_public", loopback)
    skill = await _started(ports=[up_port])
    try:
        reader, writer, head = await _ask(skill._port, f"CONNECT example.com:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 2) == b"pong:ping"
        writer.close()
        health = await skill.health()
        assert "обслужено 1" in health.detail
    finally:
        await skill.on_stop()
        upstream.close()


async def test_wrong_port_and_plain_http_are_refused() -> None:
    skill = await _started(ports=[443])
    try:
        _, writer, head = await _ask(skill._port, b"CONNECT example.com:22 HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")
        writer.close()
        _, writer, head = await _ask(skill._port, b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 405")
        writer.close()
    finally:
        await skill.on_stop()


async def test_internal_destination_is_refused() -> None:
    skill = await _started(ports=[443])
    try:
        _, writer, head = await _ask(skill._port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        await skill.on_stop()


async def test_stranger_gets_nothing() -> None:
    skill = await _started(allow=["100.64.0.0/10"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", skill._port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        try:
            answer = await asyncio.wait_for(reader.read(100), 2)
        except ConnectionResetError:
            answer = b""  # закрыли, не дочитав: ядро шлёт RST — тоже ничего
        assert answer == b""
        writer.close()
    finally:
        await skill.on_stop()


async def test_disabled_relay_opens_no_port() -> None:
    skill = await _started(enabled=False)
    try:
        assert skill._server is None
        assert (await skill.health()).detail == "выключен"
    finally:
        await skill.on_stop()


async def test_busy_port_is_retried_until_it_frees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адреса Tailscale при старте может ещё не быть — прокси пробует снова."""
    monkeypatch.setattr(relay, "RETRY_S", 0.05)
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    tasks: list[asyncio.Task[Any]] = []
    skill = relay.RelaySkill()
    values = {"enabled": True, "host": "127.0.0.1", "port": port, "allow": ["127.0.0.0/8"]}
    skill._context = SimpleNamespace(
        setting=lambda key, default=None: values.get(key, default),
        scope=SimpleNamespace(spawn=lambda coro, name=None: tasks.append(asyncio.create_task(coro)) or tasks[-1]),
    )
    await skill.on_setup()
    await skill.on_start()
    try:
        assert skill._server is None and "не открылся" in (await skill.health()).detail
        blocker.close()
        await blocker.wait_closed()
        await asyncio.wait_for(tasks[0], 2)
        assert skill._server is not None
    finally:
        await skill.on_stop()


def test_log_post_is_parsed_and_the_name_cannot_leave_the_folder() -> None:
    head = (
        b"POST /log HTTP/1.1\r\nContent-Length: 12\r\nX-Log-Date: 2026-10-22\r\n"
        b"X-Log-Name: ../../etc/DIMA-PC\r\n\r\n"
    )
    assert relay.parse_log_post(head) == relay.LogPost(name="etcDIMA-PC", day="2026-10-22", length=12)
    assert relay.parse_log_post(b"POST /log HTTP/1.1\r\nContent-Length: 1\r\nX-Log-Date: 22.10\r\n\r\n") is None
    assert relay.parse_log_post(b"POST /other HTTP/1.1\r\n\r\n") is None


async def test_laptop_log_lands_on_the_server(tmp_path: Path) -> None:
    """Сквозной круг: обработчик на ноуте → прокси → дневной файл на сервере."""
    import logging

    from jarvis.core.logging.ship import ShipHandler

    skill = await _started(logs_dir=str(tmp_path))
    port = skill._port
    handler = ShipHandler("127.0.0.1", port, every_s=0.5, name="DIMA-PC")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("ship-test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("Распознано: включи музыку")
        for _ in range(50):
            await asyncio.sleep(0.1)
            files = list(tmp_path.glob("DIMA-PC-*.log"))
            if files and files[0].read_text(encoding="utf-8"):
                break
        assert files, "файл не появился"
        assert files[0].read_text(encoding="utf-8") == "Распознано: включи музыку\n"
    finally:
        log.removeHandler(handler)
        await asyncio.to_thread(handler.close)
        await skill.on_stop()


async def test_without_logs_dir_the_proxy_takes_no_logs(tmp_path: Path) -> None:
    skill = await _started()
    try:
        _, writer, head = await _ask(
            skill._port,
            b"POST /log HTTP/1.1\r\nContent-Length: 1\r\nX-Log-Date: 2026-10-22\r\n\r\nx",
        )
        assert head.startswith(b"HTTP/1.1 405")
        writer.close()
    finally:
        await skill.on_stop()


def test_ship_address_comes_from_the_proxy_url() -> None:
    from jarvis.core.logging.ship import proxy_address

    assert proxy_address("http://127.0.0.1:8888") == ("127.0.0.1", 8888)
    assert proxy_address("127.0.0.1:8888") == ("127.0.0.1", 8888)
    assert proxy_address("") is None
    assert proxy_address("socks5://127.0.0.1:1080") is None
