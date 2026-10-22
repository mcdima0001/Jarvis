"""Копия лога на сервере: строки пачками уходят в прокси ноутбука.

Просьба владельца 22.10.2026: лог сохранять и на сервере, через прокси.
Забирать его по ssh сервер умеет и так (`/root/bin/jarvis-logs`, раз в десять
минут), но это **тяга**: выключился ноут посреди вечера — последние минуты
остались на нём, а разбирать их надо как раз тогда. Отсюда толкание, по
строке почти сразу.

Путь — тот же прокси, через который ноут ходит в облака (скилл `relay` на
сервере): туннель уже поднят, второй порт и второй ключ заводить незачем.
Прокси, получив ``POST /log`` вместо ``CONNECT``, дописывает тело в дневной
файл у себя.

Устройство нарочно на стандартной библиотеке. Отправка через `httpx` сама
пишет в лог — и каждая пачка рождала бы строки о себе, которые ушли бы
следующей пачкой. `http.client` молчит. Строки, рождённые в потоке отправки,
сюда не попадают вовсе.

Сеть легла — строки копятся до `MAX_BACKLOG` байт, лишнее старое
выбрасывается: копия лога не стоит памяти ассистента. Файл на ноуте остаётся
главным, это только копия.
"""

from __future__ import annotations

import http.client
import logging
import sys
import threading
import time
from collections import deque
from datetime import date
from urllib.parse import urlsplit

#: Куда в прокси слать строки.
PATH = "/log"
#: Сколько байт держать, пока сервер недоступен. День подробного лога — около
#: мегабайта, так что это часы обрыва.
MAX_BACKLOG = 4 * 1024 * 1024
#: Больше этого за один запрос не шлём: прокси режет тело на 1 МБ.
MAX_BATCH = 512 * 1024
#: Сколько ждать сервер, с.
TIMEOUT_S = 5.0


def proxy_address(url: str) -> tuple[str, int] | None:
    """Хост и порт прокси из адреса вида ``http://127.0.0.1:8888``. Чистая функция."""
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if parts.scheme != "http" or not parts.hostname:
        return None
    try:
        port = parts.port or 80
    except ValueError:
        return None
    return parts.hostname, port


class ShipHandler(logging.Handler):
    """Копит отформатированные строки и раз в `every_s` отдаёт их прокси."""

    def __init__(self, host: str, port: int, *, every_s: float = 5.0, name: str = "jarvis") -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._every = max(0.5, every_s)
        self._name = name
        self._guard = threading.Lock()
        self._lines: deque[tuple[str, str]] = deque()  # (день, строка)
        self._size = 0
        self._dropped = 0
        self._failing = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="log-ship", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if threading.current_thread() is self._thread:
            return
        try:
            line = self.format(record) + "\n"
        except Exception:  # noqa: BLE001 — сломанная запись не должна ронять лог
            self.handleError(record)
            return
        day = date.fromtimestamp(record.created).isoformat()
        with self._guard:
            self._lines.append((day, line))
            self._size += len(line)
            while self._size > MAX_BACKLOG and self._lines:
                _, old = self._lines.popleft()
                self._size -= len(old)
                self._dropped += 1

    def close(self) -> None:
        """Дослать, что накопилось, и остановить поток. Зовёт `logging.shutdown`."""
        self._stop.set()
        self._wake.set()
        self._thread.join(TIMEOUT_S * 2)
        super().close()

    # --- поток отправки ------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._every)
            self._flush()
        self._flush()

    def _take(self) -> tuple[str, str]:
        """Забрать из очереди строки одного дня, не больше `MAX_BATCH`."""
        with self._guard:
            if not self._lines:
                return "", ""
            day = self._lines[0][0]
            taken: list[str] = []
            size = 0
            while self._lines and self._lines[0][0] == day and size < MAX_BATCH:
                _, line = self._lines.popleft()
                taken.append(line)
                size += len(line)
            self._size -= size
            if self._dropped:
                taken.insert(0, f"… пока сервер был недоступен, выброшено строк: {self._dropped}\n")
                self._dropped = 0
            return day, "".join(taken)

    def _giveback(self, day: str, text: str) -> None:
        """Вернуть неотправленное в начало очереди."""
        with self._guard:
            self._lines.appendleft((day, text))
            self._size += len(text)

    def _flush(self) -> None:
        while True:
            day, text = self._take()
            if not text:
                return
            if not self._send(day, text):
                self._giveback(day, text)
                return

    def _send(self, day: str, text: str) -> bool:
        body = text.encode("utf-8", "replace")
        connection = http.client.HTTPConnection(self._host, self._port, timeout=TIMEOUT_S)
        try:
            connection.request(
                "POST",
                PATH,
                body=body,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "X-Log-Name": self._name,
                    "X-Log-Date": day,
                },
            )
            status = connection.getresponse().status
        except (OSError, http.client.HTTPException):
            status = 0
        finally:
            connection.close()
        ok = 200 <= status < 300
        if ok == self._failing:
            # Только смену состояния, и только в stderr: запись в лог отсюда
            # ушла бы в эту же очередь.
            self._failing = not ok
            note = "снова доходит" if ok else f"не доходит (ответ {status or 'нет'}), коплю"
            try:
                print(f"{time.strftime('%H:%M:%S')} Копия лога на сервер {note}", file=sys.stderr)
            except Exception:  # noqa: BLE001 — у pythonw stderr бывает None
                pass
        return ok
