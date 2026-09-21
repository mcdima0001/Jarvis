"""Вторая глубина: место определяет агент-кодер из панели.

Замер 21.09.2026 (`tools/place_agent_bench.py`) на десяти снимках с известными
координатами: медиана промаха **60 метров** против двух километров у обычного
пути через зрячую модель, восемь снимков из десяти — ближе ста метров, с
опознанием до здания. Плата — время: медиана три минуты, худший случай сорок
семь. Отсюда и две глубины: быстрый примерный ответ и медленный точный.

**Картинку в запрос не положить** — у `/api/agent` нет поля для файлов, а
base64 в теле ломает агента. Зато он скачивает по ссылке, а ноутбук и сервер
видят друг друга в Tailscale. Поэтому снимок отдаётся **одноразовой раздачей**:
случайный путь, один файл, слушаем только свой адрес в Tailscale и закрываемся
сразу, как отдали. Ссылка живёт секунды, и наружу из сети она не видна.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

#: Как агент обязан ответить. Три строки, ничего лишнего: ответ читает код.
ASK = """Посмотри на снимок и определи, где он снят.

Скачай его: curl -sS --max-time 30 -o /tmp/{name} "{url}"

Смотри на сам снимок: вывески и надписи, архитектура, транспорт, дорожная
разметка, растительность, номера машин. По прочитанным надписям искать в
интернете можно — это и есть работа. Постарайся уложиться в {budget} секунд:
лучше честный район сейчас, чем точный адрес через полчаса.
{clue}
Отвечай {language}: ответ произносят вслух, и язык тут не украшение — его
читает синтез, а чужой алфавит он прочтёт с акцентом или не прочтёт вовсе.

Ответь ровно тремя строками и ничем больше:
МЕСТО: <город, страна; если не понял — пиши «не знаю»>
ТОЧКА: <широта>, <долгота> — самая вероятная точка съёмки, числами
ТОЧНОСТЬ: <что обещаешь: город / район / улица / здание>
"""

#: Подгонялка в ту же сессию, когда агент думает слишком долго. Панель
#: принимает второе сообщение в работающую сессию: приходит `sessionId`, и
#: запрос с ним попадает в тот же разговор (проверено 21.09.2026).
NUDGE = """Время вышло. Отвечай прямо сейчас тем, что уже выяснил, и больше
ничего не проверяй. Если уверенности мало — так и скажи в строке ТОЧНОСТЬ, но
ответь. Ровно три строки:
МЕСТО: <город, страна; не понял — «не знаю»>
ТОЧКА: <широта>, <долгота> числами
ТОЧНОСТЬ: <город / район / улица / здание>
"""

POINT = re.compile(r"ТОЧКА:\s*[^\d\-]*(-?\d+[.,]\d+)\s*,\s*(-?\d+[.,]\d+)")
PLACE = re.compile(r"МЕСТО:\s*(.+)")
SURE = re.compile(r"ТОЧНОСТЬ:\s*(.+)")
#: Что значит «сам не знаю» в ответе агента.
UNSURE = ("не знаю", "не определ", "не удалось", "unknown")


@dataclass(frozen=True, slots=True)
class Verdict:
    """Что сказал агент."""

    place: str
    point: tuple[float, float] | None
    precision: str
    seconds: float
    nudged: bool = False

    @property
    def sure(self) -> bool:
        """Уверен ли: «не знаю» в ответе — это отказ, а не место."""
        low = self.place.lower()
        return bool(self.point) and not any(mark in low for mark in UNSURE)


def parse_verdict(text: str, *, seconds: float = 0.0, nudged: bool = False) -> Verdict | None:
    """Разобрать три строки ответа. ``None`` — агент ответил не по форме."""
    place, point, sure = PLACE.search(text), POINT.search(text), SURE.search(text)
    if place is None:
        return None
    found: tuple[float, float] | None = None
    if point is not None:
        latitude, longitude = (float(value.replace(",", ".")) for value in point.groups())
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            found = (latitude, longitude)
    return Verdict(
        place=place.group(1).strip(),
        point=found,
        precision=sure.group(1).strip() if sure else "",
        seconds=round(seconds, 1),
        nudged=nudged,
    )


def tailscale_address() -> str:
    """Свой адрес в Tailscale — по нему сервер видит ноутбук. Пусто — нет сети."""
    try:
        done = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = done.stdout.decode("utf-8", "replace").strip().splitlines()
    return first[0].strip() if first else ""


class Handoff:
    """Одноразовая раздача одного файла: только свой адрес, случайный путь.

    Снимок уходит на сервер владельца (он это разрешил 21.09.2026), но не в
    открытый интернет: слушаем адрес в Tailscale, путь случайный, раздача
    закрывается сразу после работы.
    """

    def __init__(self, data: bytes, *, host: str, port: int, kind: str = "jpg") -> None:
        self._data = data
        self._host = host
        self._port = port
        self.name = f"{secrets.token_urlsafe(12)}.{kind}"
        self._server: http.server.HTTPServer | None = None
        #: Забрали ли файл — для лога: молчаливый отказ иначе не отличить от отказа агента.
        self.taken = False

    @property
    def url(self) -> str:
        """Ссылка, которую отдаём агенту."""
        return f"http://{self._host}:{self._port}/{self.name}"

    def __enter__(self) -> "Handoff":
        data, name, owner = self._data, self.name, self

        class Once(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
                if self.path != f"/{name}":
                    self.send_error(404)
                    return
                owner.taken = True
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                """Свой лог: стандартный пишет в stderr, которого у нас нет."""

        self._server = http.server.HTTPServer((self._host, self._port), Once)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class PanelAgent:
    """Агент панели: спросить про снимок и, если затянул, подогнать."""

    def __init__(
        self,
        *,
        url: str,
        key: str,
        model: str = "opus",
        workdir: str = "/home/jarvis/author",
        host: str = "",
        port: int = 8799,
    ) -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._model = model
        self._workdir = workdir
        self._host = host
        self._port = port

    @property
    def ready(self) -> bool:
        """Есть ли всё, чтобы спрашивать: адрес панели, ключ и своя сеть."""
        return bool(self._url and self._key and self.address)

    @property
    def address(self) -> str:
        """Свой адрес в Tailscale: задан настройкой или спрошен у самого Tailscale."""
        if not self._host:
            self._host = tailscale_address()
        return self._host

    def _payload(self, message: str, session: str = "") -> dict[str, object]:
        body: dict[str, object] = {
            "message": message,
            "stream": True,
            "provider": "claude",
            "projectPath": self._workdir,
            "model": self._model,
        }
        if session:
            body["sessionId"] = session
        return body

    async def _collect(
        self, body: dict[str, object], *, timeout: float, session: asyncio.Future[str] | None = None
    ) -> str:
        """Прочитать поток ответа. Только потоком: нестримовый режим панели врёт."""
        chunks: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self._url}/api/agent",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    found = event.get("sessionId")
                    if session is not None and isinstance(found, str) and not session.done():
                        session.set_result(found)
                    if (event.get("kind") or event.get("type")) == "text":
                        chunks.append(str(event.get("content") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)

    async def place(
        self,
        image: bytes,
        *,
        hint: str = "",
        language: str = "ru",
        budget: float = 120.0,
        nudge_after: float = 0.0,
        timeout: float = 1800.0,
        kind: str = "jpg",
    ) -> Verdict | None:
        """Показать снимок агенту и получить место.

        :param language: на каком языке отвечать — ответ произносят вслух.
        :param budget: сколько секунд просим уложиться — это просьба, не предел.
        :param nudge_after: через сколько секунд подогнать; ноль — не подгонять.
            Подгонялка стоит точности (замер: медиана промаха 60 м → 8.7 км на
            пороге в пять минут), поэтому она предохранитель, а не ускоритель.
        """
        if not self.ready:
            return None
        clue = f"\nВладелец подсказывает: {hint.strip()}\n" if hint.strip() else ""
        started = time.perf_counter()
        with Handoff(image, host=self.address, port=self._port, kind=kind) as handoff:
            message = ASK.format(
                name=handoff.name,
                url=handoff.url,
                budget=int(budget),
                clue=clue,
                language="по-английски" if language.startswith("en") else "по-русски",
            )
            session: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            asking = asyncio.create_task(
                self._collect(self._payload(message), timeout=timeout, session=session)
            )
            nudged = False
            try:
                if nudge_after > 0:
                    try:
                        text = await asyncio.wait_for(asyncio.shield(asking), timeout=nudge_after)
                    except TimeoutError:
                        nudged = True
                        if not session.done():
                            asking.cancel()
                            return None
                        text = await self._collect(
                            self._payload(NUDGE, session.result()), timeout=timeout
                        )
                        asking.cancel()
                else:
                    text = await asking
            except Exception as exc:  # noqa: BLE001 — чужая служба, своя работа важнее
                logger.warning("Агент не ответил про снимок: %s", exc)
                asking.cancel()
                return None
            if not handoff.taken:
                logger.warning("Агент не забрал снимок с %s — проверь Tailscale", handoff.url)

        return parse_verdict(text, seconds=time.perf_counter() - started, nudged=nudged)
