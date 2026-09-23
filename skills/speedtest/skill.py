"""Измерение скорости интернет-соединения."""

from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

# Файл для замера скорости загрузки: отдаётся Cloudflare, размер задаётся в URL.
DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={size}"
# Приёмник для замера отдачи: тело запроса отбрасывается, возвращается пустой ответ.
UPLOAD_URL = "https://speed.cloudflare.com/__up"
# Хост и порт для проверки задержки обычным TCP-рукопожатием.
PING_HOST = "1.1.1.1"
PING_PORT = 443
# Размеры проб в байтах: маленькая прогревает соединение, большая считается.
WARMUP_BYTES = 256 * 1024
PROBE_BYTES = 8 * 1024 * 1024
UPLOAD_BYTES = 2 * 1024 * 1024
# Cloudflare отвечает 403 на подпись по умолчанию «Python-urllib» (замер
# 19.09.2026): замер скорости молча срывался и назывался «сеть недоступна».
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis-speedtest"
# Ограничение на одну пробу, чтобы голосовой круг не ждал вечно.
PROBE_TIMEOUT = 20.0


class SpeedtestSkill(Skill):
    """Замеряет скорость загрузки, отдачи и задержку соединения."""

    meta = SkillMeta(
        name="speedtest",
        description="Измеряет скорость загрузки, отдачи и задержку интернета.",
        version="0.1.1",
        spoken=("скорость интернета", "speedtest"),
    )

    @tool(
        phrases=[
            "проверь скорость интернета",
            "какая у меня скорость интернета",
            "замерь скорость соединения",
            # Как это спрашивают вслух на самом деле — из лога за 18–23.09.2026,
            # когда все три формулировки ушли в модель и пропали вместе с ней.
            "что со скоростью интернета",
            "что со скоростью в интернете",
            "что по скорости интернета",
            "что по скорости с интернетом",
            "скорость интернета",
        ],
        reversible=True,
    )
    async def measure_speed(self, mode: str = "download") -> ToolResult:
        """Измеряет скорость интернет-соединения и задержку до сервера.

        :param mode: что мерить — "download", "upload", "latency" или "full".
        """
        normalized = mode.strip().lower() or "download"
        if normalized in {"полная", "все", "всё", "all"}:
            normalized = "full"
        if normalized not in {"download", "upload", "latency", "full"}:
            return ToolResult.failure(
                f"Неизвестный режим замера: {mode}.",
                speech={
                    "ru": "Не понимаю такой режим замера, скажи загрузка, отдача или задержка.",
                    "en": "Unknown test mode, say download, upload or latency.",
                },
            )

        try:
            payload = await asyncio.to_thread(self._run_probes, normalized)
        except urllib.error.HTTPError as exc:
            # Сеть есть, отказал сервер замера — это не «сеть недоступна».
            self.log.warning("Сервер замера скорости отказал: %s", exc)
            return ToolResult.failure(
                f"Сервер замера отказал: {exc}",
                speech={
                    "ru": "Сервер замера скорости отказал, сеть при этом есть.",
                    "en": "The speed test server refused, but the network is up.",
                },
            )
        except (urllib.error.URLError, http.client.HTTPException, OSError, ssl.SSLError) as exc:
            self.log.warning("Замер скорости не удался: %s", exc)
            return ToolResult.failure(
                f"Не удалось выполнить замер: {exc}",
                speech={
                    "ru": "Не получилось замерить скорость, сеть недоступна.",
                    "en": "Could not measure the speed, the network is unavailable.",
                },
            )

        return ToolResult.success(payload, speech=self._speech(payload))

    @tool(phrases=["есть ли интернет", "проверь связь", "что с интернетом",
                   "что там с интернетом", "интернет работает"], reversible=True)
    async def check_latency(self) -> ToolResult:
        """Быстро проверяет доступность сети и задержку без замера скорости."""
        try:
            latency_ms = await asyncio.to_thread(self._measure_latency)
        except OSError as exc:
            return ToolResult.failure(
                f"Сеть недоступна: {exc}",
                speech={
                    "ru": "Интернета нет, сеть не отвечает.",
                    "en": "There is no internet, the network is not responding.",
                },
            )

        return ToolResult.success(
            {"latency_ms": latency_ms, "online": True},
            speech={
                "ru": f"Связь есть, задержка {self._say_number(latency_ms)} миллисекунд.",
                "en": f"Network is up, latency {self._say_number(latency_ms)} milliseconds.",
            },
        )

    async def health(self) -> HealthStatus:
        """Считает скилл рабочим, пока сервер замеров отзывается."""
        try:
            await asyncio.to_thread(self._measure_latency)
        except OSError as exc:
            return HealthStatus.degraded(f"сеть недоступна: {exc}")
        return HealthStatus.healthy()

    def _run_probes(self, mode: str) -> dict[str, Any]:
        """Выполняет нужные пробы по порядку, всё блокирующее внутри потока."""
        payload: dict[str, Any] = {"mode": mode}

        if mode in {"latency", "full"}:
            payload["latency_ms"] = self._measure_latency()
        if mode in {"download", "full"}:
            payload["download_mbps"] = self._measure_download()
        if mode in {"upload", "full"}:
            payload["upload_mbps"] = self._measure_upload()

        return payload

    def _measure_latency(self) -> float:
        """Возвращает время TCP-рукопожатия в миллисекундах."""
        started = time.perf_counter()
        with socket.create_connection((PING_HOST, PING_PORT), timeout=5.0):
            elapsed = time.perf_counter() - started
        return round(elapsed * 1000, 1)

    def _measure_download(self) -> float:
        """Скачивает пробный файл и возвращает скорость в мегабитах в секунду."""
        # Первая короткая проба нужна, чтобы не считать разгон соединения.
        self._download_bytes(WARMUP_BYTES)
        received, elapsed = self._download_bytes(PROBE_BYTES)
        return self._to_mbps(received, elapsed)

    def _download_bytes(self, size: int) -> tuple[int, float]:
        """Качает заданное число байт и возвращает объём и затраченное время."""
        request = urllib.request.Request(
            DOWNLOAD_URL.format(size=size),
            headers={"Cache-Control": "no-cache", "User-Agent": USER_AGENT},
        )
        received = 0
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                # Обрываем пробу, если сеть настолько медленная, что упрёмся в таймаут.
                if time.perf_counter() - started > PROBE_TIMEOUT:
                    break
        return received, time.perf_counter() - started

    def _measure_upload(self) -> float:
        """Отправляет пробный блок данных и возвращает скорость отдачи."""
        body = b"0" * UPLOAD_BYTES
        request = urllib.request.Request(
            UPLOAD_URL,
            data=body,
            headers={"Content-Type": "application/octet-stream", "User-Agent": USER_AGENT},
            method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            response.read()
        return self._to_mbps(len(body), time.perf_counter() - started)

    def _to_mbps(self, size: int, elapsed: float) -> float:
        """Переводит байты за секунды в мегабиты в секунду."""
        if elapsed <= 0 or size <= 0:
            return 0.0
        return round(size * 8 / elapsed / 1_000_000, 1)

    def _say_number(self, value: float) -> str:
        """Округляет число для произношения, чтобы не диктовать дроби без нужды."""
        if value >= 10:
            return str(int(round(value)))
        return str(value).replace(".", ",")

    def _speech(self, payload: dict[str, Any]) -> dict[str, str]:
        """Собирает короткую фразу по результатам замера."""
        download = payload.get("download_mbps")
        upload = payload.get("upload_mbps")
        latency = payload.get("latency_ms")

        if download is not None and upload is not None:
            return {
                "ru": (
                    f"Загрузка {self._say_number(download)}, "
                    f"отдача {self._say_number(upload)} мегабит в секунду."
                ),
                "en": (
                    f"Download {self._say_number(download)}, "
                    f"upload {self._say_number(upload)} megabits per second."
                ),
            }
        if download is not None:
            return {
                "ru": f"Скорость загрузки {self._say_number(download)} мегабит в секунду.",
                "en": f"Download speed is {self._say_number(download)} megabits per second.",
            }
        if upload is not None:
            return {
                "ru": f"Скорость отдачи {self._say_number(upload)} мегабит в секунду.",
                "en": f"Upload speed is {self._say_number(upload)} megabits per second.",
            }
        if latency is not None:
            return {
                "ru": f"Задержка {self._say_number(latency)} миллисекунд.",
                "en": f"Latency is {self._say_number(latency)} milliseconds.",
            }
        return {
            "ru": "Замер не дал результата.",
            "en": "The measurement returned nothing.",
        }
