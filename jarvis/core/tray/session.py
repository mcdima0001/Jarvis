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
from jarvis.core.logging.visible import visible_levels

from .autostart import Autostart, AutostartError
from .menu import AUTOSTART, HUSH, READY, STARTING, STOPPING, tip

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


def live_log_command(path: Path, level: str = "INFO", *, tail: int = 1500) -> list[str]:
    """Команда окна, где лог дописывается на глазах: `Get-Content -Wait`.

    Блокнот показывает снимок файла, а смотреть в лог нужно как раз по ходу
    дела — сказал команду и видишь, что с ней стало. Кодировка консоли ставится
    явно: иначе кириллица в Windows PowerShell приходит мусором.

    Показывается то же, что в консоли (`jarvis/core/logging/visible.py`):
    отладочные строки есть в файле, но в окне они топят нужное. Хвост берётся
    длиннее, потому что большая его часть отсеется.
    """
    literal = str(path).replace("'", "''")
    levels = ",".join(f"'{name}'" for name in visible_levels(level))
    script = (
        _LIVE_LOG.replace("__LEVELS__", levels)
        .replace("__PATH__", literal)
        .replace("__TAIL__", str(tail))
    )
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script]


#: Скрипт окна лога. Цвета те же, что у консоли (`logging/colors.py`): время и
#: имя логгера приглушены, предупреждение жёлтое, ошибка красная, услышанное
#: голубое, сказанное зелёное. В файле пометки «услышал/сказал» нет, поэтому она
#: узнаётся по началу сообщения конвейера — «Распознано» и «Отвечаю:».
#: Строки продолжения (стек ошибки) наследуют цвет и видимость своей записи.
_LIVE_LOG = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$Host.UI.RawUI.WindowTitle = 'Jarvis — лог'; "
    "$levels = @(__LEVELS__); $show = $true; $color = 'Gray'; "
    "Get-Content -LiteralPath '__PATH__' -Encoding UTF8 -Tail __TAIL__ -Wait | ForEach-Object { "
    "$line = $_; "
    "if ($line -match '^(\\d[\\d.:, /-]*) (DEBUG|INFO|WARNING|ERROR|CRITICAL)(\\s+)(\\S+\\s+)(.*)$') { "
    "$time = $Matches[1]; $level = $Matches[2]; $pad = $Matches[3]; $name = $Matches[4]; $text = $Matches[5]; "
    "$show = $levels -contains $level; "
    "if ($show) { "
    "$color = 'Gray'; "
    "if ($level -eq 'WARNING') { $color = 'Yellow' } "
    "elseif ($level -eq 'ERROR') { $color = 'Red' } "
    "elseif ($level -eq 'CRITICAL') { $color = 'Magenta' } "
    "elseif ($text.StartsWith('Распознано')) { $color = 'Cyan' } "
    "elseif ($text.StartsWith('Отвечаю:')) { $color = 'Green' }; "
    "$levelColor = 'Gray'; if ($level -ne 'INFO') { $levelColor = $color }; "
    "Write-Host ($time + ' ') -ForegroundColor DarkGray -NoNewline; "
    "Write-Host ($level + $pad) -ForegroundColor $levelColor -NoNewline; "
    "Write-Host $name -ForegroundColor DarkGray -NoNewline; "
    "Write-Host $text -ForegroundColor $color "
    "} "
    "} elseif ($show) { Write-Host $line -ForegroundColor $color } "
    "}"
)


#: Где обычно стоит Edge. Окно `--app` — без адресной строки и вкладок, как
#: отдельная программа; Edge есть в любой Windows 10 и 11.
EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


#: Какую долю рабочего стола занимает окно панели. Подобрано по снимку
#: владельца (14.09.2026): при 1180×760 лог переносился на каждой строке.
PANEL_WIDTH, PANEL_HEIGHT, PANEL_MARGIN = 0.9, 0.83, 0.012


def panel_geometry(work_area: tuple[int, int, int, int], dpi: int = 96) -> tuple[int, int, int, int]:
    """Положение и размер окна панели в независимых точках: x, y, ширина, высота.

    Edge ждёт размеры в точках без учёта масштаба Windows, а рабочий стол
    процесс, объявивший осведомлённость о масштабе, получает в физических
    пикселях. Отсюда деление на `dpi / 96`.
    """
    left, top, right, bottom = work_area
    scale = dpi / 96 if dpi else 1.0
    width, height = (right - left) / scale, (bottom - top) / scale
    return (
        round(left / scale + width * PANEL_MARGIN),
        round(top / scale + height * PANEL_MARGIN),
        round(width * PANEL_WIDTH),
        round(height * PANEL_HEIGHT),
    )


def fits_screen(
    geometry: tuple[int, int, int, int], screen: tuple[int, int, int, int]
) -> bool:
    """Видно ли окно на экранах: хотя бы угол заголовка должен попасть на них.

    Сохранённое положение бывает с отключённого монитора — открыть окно там
    значит открыть его невидимым.

    :param screen: весь виртуальный экран в независимых точках: x, y, ширина, высота.
    """
    x, y, width, _ = geometry
    left, top, screen_width, screen_height = screen
    grab_x, grab_y = x + min(width, 200) // 2, y + 10
    return left <= grab_x < left + screen_width and top <= grab_y < top + screen_height


def _work_area() -> tuple[tuple[int, int, int, int], int] | None:
    """Рабочий стол без панели задач и масштаб системы."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return None
    try:
        dpi = int(user32.GetDpiForSystem())
    except AttributeError:
        dpi = 96
    return (rect.left, rect.top, rect.right, rect.bottom), dpi


#: Свой профиль Edge для окна панели. В общем профиле Edge запоминал, где стояло
#: его последнее окно, — а это окно панели, и **любое** новое окно браузера
#: открывалось на месте панели (жалоба владельца 19.09.2026). Лежит в `memory/`:
#: это состояние машины, а не код, и в репозиторий не едет.
PANEL_PROFILE = Path(__file__).resolve().parents[3] / "memory" / "panel-browser"


def panel_command(
    url: str,
    edge: Path | None,
    geometry: tuple[int, int, int, int] | None = None,
    profile: Path = PANEL_PROFILE,
) -> list[str] | None:
    """Команда окна панели; ``None`` — Edge нет, откроется обычный браузер."""
    if edge is None:
        return None
    x, y, width, height = geometry or (40, 40, 1180, 760)
    return [
        str(edge),
        f"--app={url}",
        f"--window-size={width},{height}",
        f"--window-position={x},{y}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def open_panel(url: str, saved: tuple[int, int, int, int] | None = None) -> None:
    """Открыть панель отдельным окном: там, где она была, либо на 90% рабочего стола.

    Флаги размера Edge соблюдает только при своём старте, а если он уже открыт
    как обычный браузер — молча игнорирует. Поэтому сохранённое место окно
    получает не флагами, а после появления: его ставит `window.place_new_window`.
    """
    edge = next((path for path in EDGE_PATHS if path.exists()), None)
    area = _work_area()
    command = panel_command(url, edge, panel_geometry(*area) if area else None)
    if saved is not None and command is not None and sys.platform == "win32":
        import threading

        from jarvis.core.gui import window

        before = set(window.panel_windows())
        subprocess.Popen(command)
        threading.Thread(
            target=window.place_new_window, args=(saved, before), name="panel-place", daemon=True
        ).start()
        return
    if command is not None:
        subprocess.Popen(command)
    elif sys.platform == "win32":
        os.startfile(url)  # noqa: S606 — адрес наш, локальный
    else:
        logger.info("Панель: %s", url)


def open_live_log(path: Path, level: str = "INFO") -> None:
    """Открыть отдельное окно с логом в реальном времени."""
    if sys.platform == "win32":
        subprocess.Popen(live_log_command(path, level), creationflags=subprocess.CREATE_NEW_CONSOLE)
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
        live_log: Callable[[Path, str], None] = open_live_log,
        log_file: Callable[[], Path | None] = current_log_file,
        panel: Callable[[str, tuple[int, int, int, int] | None], None] = open_panel,
        autostart: Autostart | None = None,
        notify: Callable[..., None] | None = None,
    ) -> None:
        self._log_file = log_file
        self._autostart = autostart
        self._notify = notify
        self._open_panel = panel
        #: Где окно панели было в прошлый раз — спрашивается у панели в момент открытия.
        self._saved_window: Callable[[], tuple[int, int, int, int] | None] = lambda: None
        #: Адрес панели с токеном. Появляется, когда приложение подключено.
        self.panel_url: str | None = None
        #: Уровень консоли: окно лога показывает то же, что показала бы она.
        self.log_level = "INFO"
        self.name = name
        self.root = root or Path.cwd()
        #: Выбран ли перезапуск: решает, что делать после остановки.
        self.restart = False
        self._opener = opener
        self._live_log = live_log
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._quit_early = False
        #: Как замолчать: ставится при подключении приложения.
        self._hush: Callable[[], object] | None = None
        self.icon = icon_factory(self.on_action)
        # Галочки меню — факт системы, а не значка: значок только спрашивает.
        if hasattr(self.icon, "checked"):
            self.icon.checked = self.checked_actions

    @property
    def autostart(self) -> Autostart:
        """Автозапуск с Windows для этой папки проекта."""
        if self._autostart is None:
            self._autostart = Autostart(self.root)
        return self._autostart

    def checked_actions(self) -> frozenset[str]:
        """Какие переключатели меню сейчас включены."""
        return frozenset({AUTOSTART}) if self.autostart.enabled() else frozenset()

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
        self.panel_url = app.panel.url if app.panel is not None else None
        if app.panel is not None:
            self._saved_window = app.panel.saved_window
        self.log_level = app.config.logging.level
        pipeline = getattr(app, "pipeline", None)
        self._hush = pipeline.interrupt if pipeline is not None else None
        app.events.subscribe(SystemStarted.NAME, self._on_started)
        app.events.subscribe(SystemStopping.NAME, self._on_stopping)
        self._set(STARTING)
        if self._quit_early:
            app.stopping.set()

    def on_action(self, action: str) -> None:
        """Пункт меню выбран. Зовётся из потока значка."""
        if action == "panel":
            if self.panel_url:
                self._open_panel(self.panel_url, self._saved_window())
            else:
                # Панели нет (выключена или ещё не поднялась) — хотя бы лог.
                self.on_action("log")
        elif action == "log":
            path = self._log_file()
            if path is None:
                self._opener(self.root / "logs")
            else:
                self._live_log(path, self.log_level)
        elif action == "folder":
            self._opener(self.root)
        elif action == HUSH:
            if self._loop is not None and self._hush is not None:
                self._loop.call_soon_threadsafe(self._hush)
        elif action == AUTOSTART:
            self._toggle_autostart()
        elif action in ("restart", "quit"):
            self.restart = action == "restart"
            self._set(STOPPING)
            self._request_stop()
        else:
            logger.warning("Значок в трее: неизвестное действие %s", action)

    def _toggle_autostart(self) -> None:
        """Переключить автозапуск и сказать, что вышло: галочку в меню видно не сразу."""
        notify = self._notify
        if notify is None:
            from .win32 import message_box

            notify = message_box
        try:
            enabled = self.autostart.toggle()
        except AutostartError as exc:
            logger.warning("Автозапуск не переключился: %s", exc)
            notify(str(exc), error=True)
            return
        notify(
            "Jarvis будет запускаться при входе в Windows, без окна UAC."
            if enabled
            else "Автозапуск выключен: с Windows Jarvis больше не стартует."
        )

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
