"""Jarvis в трее: запуск без консоли, связь значка с приложением, перезапуск.

Запускает это `python -m jarvis --tray`, а на машине владельца — `Jarvis.exe`
(см. `launcher/`), который зовёт `pythonw` без окна консоли.

Без консоли пропадают сразу три вещи, и каждую пришлось вернуть:

* **куда писать** — у `pythonw` потоки вывода равны ``None``, и первая же
  библиотека, пишущая в них, падает. Вывод уходит в никуда, а хвост ошибок
  копится в памяти;
* **как узнать об ошибке** — упавший запуск показывает окно с этим хвостом,
  иначе значок просто молча исчез бы;
* **как выключить** — `Ctrl+C` нажимать некуда, поэтому «Выйти» в меню.

Двойной запуск не допускается: второй Jarvis делил бы микрофон с первым и
отвечал на каждую команду дважды.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.core.contracts import Event, SystemStarted, SystemStopping

from .menu import READY, STARTING, STOPPING, tip

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

logger = logging.getLogger(__name__)

#: Имя мьютекса одиночного запуска. `Local\` — в пределах сеанса пользователя.
MUTEX_NAME = "Local\\JarvisAssistant"
#: Процесс запущен перезапуском: ему надо дождаться, пока старый отпустит место.
RESTART_ENV = "JARVIS_RESTARTED"
#: Сколько новый процесс ждёт старый. Прощание и остановка сервисов — секунды,
#: запас на случай, когда облачный голос отвечает медленно.
RESTART_WAIT_S = 60.0

_ICONS_DIR = Path(__file__).resolve().parent
#: Вид значка на каждое состояние: пока грузится — янтарный, готов — голубой.
ICONS = {
    STARTING: _ICONS_DIR / "jarvis-busy.ico",
    READY: _ICONS_DIR / "jarvis.ico",
    STOPPING: _ICONS_DIR / "jarvis-busy.ico",
}


class Icon(Protocol):
    """Что сессии нужно от значка. Настоящий — `win32.TrayIcon`."""

    def start(self) -> None: ...

    def set_state(self, state: str, tip: str) -> None: ...

    def stop(self) -> None: ...


class Lock(Protocol):
    """Одиночный запуск. Настоящий — `win32.SingleInstance`."""

    def acquire(self, wait_s: float = 0.0) -> bool: ...

    def release(self) -> None: ...


class StderrTail(io.TextIOBase):
    """Поток ошибок без консоли: хранит последние строки, чтобы их показать."""

    def __init__(self, limit: int = 2000) -> None:
        super().__init__()
        self._limit = limit
        self._text = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._text = (self._text + text)[-self._limit :]
        return len(text)

    @property
    def text(self) -> str:
        """Накопленный хвост без пустых краёв."""
        return self._text.strip()


def quiet_streams() -> StderrTail | None:
    """Подставить потоки вывода, если их нет (`pythonw`).

    Запущенный из терминала `--tray` потоки имеет — их не трогаем, так удобнее
    отлаживать. Возвращает хвост ошибок, если он подставлен.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — живёт до конца процесса
    if sys.stderr is None:
        tail = StderrTail()
        sys.stderr = tail
        return tail
    return None


def current_log_file(root: logging.Logger | None = None) -> Path | None:
    """Файл, в который прямо сейчас пишется лог: у дневного он меняется в полночь."""
    for handler in (root or logging.getLogger()).handlers:
        name = getattr(handler, "baseFilename", None)
        # Обработчик в пустое устройство — тоже файловый: такой вешает pytest.
        if name and Path(name).name.lower() not in ("nul", "null"):
            return Path(name)
    return None


def restart_command(argv: list[str]) -> list[str]:
    """Команда перезапуска: тот же интерпретатор, те же флаги."""
    return [sys.executable, "-m", "jarvis", *argv]


def open_path(path: Path) -> None:
    """Открыть файл или папку тем, чем их открывает проводник."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — путь наш, а не услышанный
    else:
        logger.info("Открыть %s: вне Windows нечем", path)


def live_log_command(path: Path, *, tail: int = 200) -> list[str]:
    """Команда окна, где лог дописывается на глазах: `Get-Content -Wait`.

    Блокнот показывает снимок файла, а смотреть в лог нужно как раз по ходу
    дела — сказал команду и видишь, что с ней стало. Кодировка консоли ставится
    явно: иначе кириллица в Windows PowerShell приходит мусором.
    """
    literal = str(path).replace("'", "''")
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$Host.UI.RawUI.WindowTitle = 'Jarvis — лог'; "
        f"Get-Content -LiteralPath '{literal}' -Encoding UTF8 -Tail {tail} -Wait"
    )
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script]


def open_live_log(path: Path) -> None:
    """Открыть отдельное окно с логом в реальном времени."""
    if sys.platform == "win32":
        subprocess.Popen(live_log_command(path), creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        logger.info("Лог в реальном времени: tail -f %s", path)


class TraySession:
    """Связка значка с живым приложением.

    Меню работает в потоке значка, приложение — в петле asyncio. Сессия
    переправляет одно в другое: остановка ставится через
    `call_soon_threadsafe`, а не вызовом напрямую.
    """

    def __init__(
        self,
        icon_factory: Callable[[Callable[[str], None]], Icon],
        *,
        name: str = "Jarvis",
        root: Path | None = None,
        opener: Callable[[Path], None] = open_path,
        live_log: Callable[[Path], None] = open_live_log,
        log_file: Callable[[], Path | None] = current_log_file,
    ) -> None:
        self._log_file = log_file
        self.name = name
        self.root = root or Path.cwd()
        #: Выбран ли перезапуск: решает, что делать после остановки.
        self.restart = False
        self._opener = opener
        self._live_log = live_log
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._quit_early = False
        self.icon = icon_factory(self.on_action)

    def show(self) -> None:
        """Показать значок в состоянии «загружается»."""
        self.icon.start()
        self._set(STARTING)

    def close(self) -> None:
        """Убрать значок."""
        self.icon.stop()

    def attach(self, app: JarvisApp) -> None:
        """Подключиться к приложению. Зовётся из петли asyncio до `app.run()`."""
        self._loop = asyncio.get_running_loop()
        self._stopping = app.stopping
        self.name = app.config.app.name
        self.root = app.config.root
        app.events.subscribe(SystemStarted.NAME, self._on_started)
        app.events.subscribe(SystemStopping.NAME, self._on_stopping)
        self._set(STARTING)
        if self._quit_early:
            app.stopping.set()

    def on_action(self, action: str) -> None:
        """Пункт меню выбран. Зовётся из потока значка."""
        if action == "log":
            path = self._log_file()
            if path is None:
                self._opener(self.root / "logs")
            else:
                self._live_log(path)
        elif action == "folder":
            self._opener(self.root)
        elif action in ("restart", "quit"):
            self.restart = action == "restart"
            self._set(STOPPING)
            self._request_stop()
        else:
            logger.warning("Значок в трее: неизвестное действие %s", action)

    def _request_stop(self) -> None:
        if self._loop is None or self._stopping is None:
            # Приложение ещё собирается: остановится, как только подключится.
            self._quit_early = True
            return
        self._loop.call_soon_threadsafe(self._stopping.set)

    async def _on_started(self, event: Event) -> None:
        self._set(READY)

    async def _on_stopping(self, event: Event) -> None:
        self._set(STOPPING)

    def _set(self, state: str) -> None:
        self.icon.set_state(state, tip(self.name, state))


def _default_icon(on_action: Callable[[str], None]) -> Icon:
    from .win32 import TrayIcon

    return TrayIcon(on_action, icons=ICONS)


def run_in_tray(
    body: Callable[[TraySession], int],
    *,
    argv: list[str],
    icon_factory: Callable[[Callable[[str], None]], Icon] | None = None,
    lock: Lock | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
    message: Callable[..., None] | None = None,
) -> int:
    """Выполнить запуск Jarvis со значком в трее.

    :param body: сам запуск; получает сессию и возвращает код выхода.
    :param argv: флаги запуска — с ними же процесс поднимется при перезапуске.
    """
    from .win32 import SingleInstance, message_box

    tail = quiet_streams()
    lock = lock or SingleInstance(MUTEX_NAME)
    message = message or message_box

    restarted = bool(os.environ.pop(RESTART_ENV, None))
    if not lock.acquire(RESTART_WAIT_S if restarted else 0.0):
        message("Jarvis уже запущен: его значок в трее, рядом с часами.")
        return 0

    session = TraySession(icon_factory or _default_icon)
    session.show()
    try:
        code = body(session)
    except Exception:  # noqa: BLE001 — без консоли непойманное исключение не увидит никто
        logger.exception("Jarvis упал")
        traceback.print_exc()
        code = 1
    finally:
        session.close()

    lock.release()
    if session.restart:
        spawn(restart_command(argv), cwd=os.getcwd(), env={**os.environ, RESTART_ENV: "1"})
        return 0
    if code not in (0, 130):
        details = tail.text if tail is not None else ""
        message(
            "Jarvis остановился с ошибкой.\n\n"
            + (details[-1200:] + "\n\n" if details else "")
            + "Подробности в логе.",
            error=True,
        )
    return code
