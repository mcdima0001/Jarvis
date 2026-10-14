"""Прокси для ноутбука: внешние API ноута ходят через сервер.

Просьба владельца 10.10.2026 («отсюда напрямую не получится»): OpenAI,
Deepgram, Fish и Telegram с ноута доступны только через сервер. Сначала это
делал tinyproxy, с 14.10.2026 — серверный Jarvis сам (решение владельца: одна
служба вместо двух). Ноут ничего не замечает: в его `.env` тот же
`HTTPS_PROXY`, адрес и порт прежние.

Скилл, а не ядро: к голосу прокси отношения не имеет, это фоновая работа,
которой у скиллов как раз и место. Выключен по умолчанию и включается только
в настройках серверного экземпляра — ноуту прокси поднимать незачем.

Умеет ровно то, что умел tinyproxy в нашей настройке, и не больше:

* только метод ``CONNECT`` (туннель TLS) — содержимого прокси не видит и не
  разбирает, обычный HTTP не пропускает;
* только с адресов из ``allow`` (по умолчанию Tailscale, ``100.64.0.0/10``);
* только на порты из ``ports`` (443);
* только в интернет: адрес назначения, разрешившийся во внутреннюю сеть,
  отвергается — иначе через прокси можно было бы постучаться в сам сервер.

Память — два буфера по `BUFFER` на соединение и ничего сверх, соединений не
больше ``max_clients``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass

from jarvis.core.skills import HealthStatus, Skill, SkillMeta

logger = logging.getLogger(__name__)

#: Сколько читать за раз в каждую сторону туннеля.
BUFFER = 64 * 1024
#: Предел заголовка запроса: CONNECT весит сотню байт.
MAX_HEAD = 8 * 1024
#: Сколько ждать заголовка от клиента и соединения с адресатом, с.
HANDSHAKE_S = 10.0

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True, slots=True)
class Target:
    """Куда просят туннель."""

    host: str
    port: int


def parse_connect(head: bytes) -> Target | None:
    """Разобрать ``CONNECT host:port HTTP/1.1``. Не CONNECT или мусор — ``None``."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].startswith("HTTP/"):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not port.isdigit():
        return None
    host = host.strip("[]")
    if not host:
        return None
    return Target(host, int(port))


def networks(values: object) -> tuple[Network, ...]:
    """Сети из настройки ``allow``; неверные пропускаются с предупреждением."""
    items = values if isinstance(values, (list, tuple)) else [values]
    result: list[Network] = []
    for item in items:
        try:
            result.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError:
            logger.warning("Прокси: не сеть %r в allow — пропускаю", item)
    return tuple(result)


def allowed_peer(address: str, allow: tuple[Network, ...]) -> bool:
    """Пускать ли клиента с этого адреса."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in allow)


def public(address: str) -> bool:
    """Адрес в интернете, а не во внутренней сети и не на этой машине."""
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


class RelaySkill(Skill):
    meta = SkillMeta(
        name="relay",
        description="Прокси для ноутбука: туннели к внешним API через сервер",
        version="0.1.0",
        spoken=("прокси", "relay"),
    )

    async def on_setup(self) -> None:
        setting = self.context.setting
        self._enabled = bool(setting("enabled", False))
        self._host = str(setting("host", "127.0.0.1"))
        self._port = int(setting("port", 8888))
        self._allow = networks(setting("allow", ["100.64.0.0/10"]))
        self._ports = frozenset(int(port) for port in (setting("ports", [443]) or ()))
        self._limit = max(1, int(setting("max_clients", 200)))
        self._idle = max(10.0, float(setting("idle_s", 600)))
        self._server: asyncio.AbstractServer | None = None
        self._active = 0
        self._served = 0
        self._refused = 0
        self._error = ""

    async def on_start(self) -> None:
        if not self._enabled:
            return
        try:
            self._server = await asyncio.start_server(self._serve, self._host, self._port)
        except OSError as exc:
            self._error = f"порт {self._host}:{self._port} не открылся: {exc}"
            logger.warning("Прокси не поднялся: %s", self._error)
            return
        logger.info(
            "Прокси для ноутбука на %s:%d (клиенты %s, порты %s)",
            self._host, self._port,
            ", ".join(map(str, self._allow)) or "никто",
            ", ".join(map(str, sorted(self._ports))) or "никакие",
        )

    async def on_stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.close_clients()
            try:
                await asyncio.wait_for(server.wait_closed(), 3)
            except TimeoutError:
                logger.warning("Прокси закрывался дольше трёх секунд — бросаю ожидание")

    async def health(self) -> HealthStatus:
        if not self._enabled:
            return HealthStatus.healthy("выключен")
        if self._error:
            return HealthStatus.degraded(self._error)
        return HealthStatus.healthy(
            f"соединений сейчас {self._active}, обслужено {self._served}, отказано {self._refused}"
        )

    # --- соединение ---------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            if not allowed_peer(str(peer[0]), self._allow):
                self._refused += 1
                logger.warning("Прокси: чужой адрес %s — отказ", peer[0])
                return
            if self._active >= self._limit:
                self._refused += 1
                await _answer(writer, "503 Service Unavailable")
                return
            self._active += 1
            try:
                await self._tunnel(reader, writer, str(peer[0]))
            finally:
                self._active -= 1
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
        if len(head) > MAX_HEAD:
            await _answer(writer, "431 Request Header Fields Too Large")
            return
        target = parse_connect(head)
        if target is None:
            self._refused += 1
            await _answer(writer, "405 Method Not Allowed")
            return
        if target.port not in self._ports:
            self._refused += 1
            logger.info("Прокси: %s просит %s:%d — порт не разрешён", peer, target.host, target.port)
            await _answer(writer, "403 Forbidden")
            return
        address = await _resolve_public(target)
        if address is None:
            self._refused += 1
            logger.info("Прокси: %s просит %s — не адрес в интернете", peer, target.host)
            await _answer(writer, "403 Forbidden")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(address, target.port), HANDSHAKE_S
            )
        except (OSError, TimeoutError) as exc:
            logger.info("Прокси: %s:%d недоступен — %s", target.host, target.port, exc)
            await _answer(writer, "502 Bad Gateway")
            return
        self._served += 1
        logger.debug("Прокси: %s → %s:%d", peer, target.host, target.port)
        await _answer(writer, "200 Connection Established")
        try:
            await asyncio.gather(
                self._pipe(reader, up_writer),
                self._pipe(up_reader, writer),
            )
        finally:
            up_writer.close()

    async def _pipe(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        """Перекладывать байты, пока одна сторона не закроет или не замолчит на `idle_s`."""
        try:
            while True:
                data = await asyncio.wait_for(source.read(BUFFER), self._idle)
                if not data:
                    break
                sink.write(data)
                await sink.drain()
        except (OSError, TimeoutError):
            pass
        finally:
            # Закрыв одну сторону, будим вторую: иначе она ждала бы `idle_s`.
            sink.close()


async def _answer(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\n\r\n".encode("ascii"))
    await writer.drain()


async def _resolve_public(target: Target) -> str | None:
    """Первый адрес назначения в интернете; внутренние адреса не годятся."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM), HANDSHAKE_S
        )
    except (OSError, TimeoutError):
        return None
    for info in infos:
        address = str(info[4][0])
        if public(address):
            return address
    return None
