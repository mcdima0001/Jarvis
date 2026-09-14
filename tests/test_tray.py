"""Значок в трее: меню, связь с приложением, одиночный запуск и перезапуск.

Сам значок — тонкая обёртка над WinAPI и проверяется живьём. Здесь проверяется
то, на чём держится обещание «без консоли ничего не теряется»: выход из меню
действительно гасит приложение, перезапуск поднимает новый процесс с теми же
флагами, второй запуск не стартует, а упавший — показывает, почему.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jarvis.__main__ import _parse_args
from jarvis.core.bus import LocalEventBus
from jarvis.core.contracts import SystemStarted
from jarvis.core.tray import menu
from jarvis.core.tray.session import (
    RESTART_ENV,
    StderrTail,
    TraySession,
    current_log_file,
    fits_screen,
    live_log_command,
    panel_command,
    panel_geometry,
    restart_command,
    run_in_tray,
)


class FakeIcon:
    def __init__(self, on_action: Any) -> None:
        self.on_action = on_action
        self.states: list[str] = []
        self.tips: list[str] = []
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def set_state(self, state: str, tip: str) -> None:
        self.states.append(state)
        self.tips.append(tip)

    def stop(self) -> None:
        self.stopped = True


class FakeLock:
    def __init__(self, free: bool = True) -> None:
        self.free = free
        self.waits: list[float] = []
        self.released = False

    def acquire(self, wait_s: float = 0.0) -> bool:
        self.waits.append(wait_s)
        return self.free

    def release(self) -> None:
        self.released = True


def _app(root: Path) -> Any:
    return SimpleNamespace(
        stopping=asyncio.Event(),
        events=LocalEventBus(),
        config=SimpleNamespace(
            app=SimpleNamespace(name="Jarvis"), root=root, logging=SimpleNamespace(level="INFO")
        ),
        panel=SimpleNamespace(url="http://127.0.0.1:8766/?token=t", saved_window=lambda: (100, 50, 1400, 900)),
    )


def _session(opened: list[Path] | None = None) -> TraySession:
    return TraySession(FakeIcon, opener=(opened.append if opened is not None else lambda path: None))


# --- меню -------------------------------------------------------------------


def test_menu_has_quit_and_restart_with_unique_commands() -> None:
    commands = menu.menu_commands()
    actions = {item.action for _, item in commands if item is not None}
    assert {"quit", "restart", "log"} <= actions
    numbers = [number for number, item in commands if item is not None]
    assert len(numbers) == len(set(numbers))
    # Ноль ОС возвращает, когда меню закрыли без выбора: пункту его давать нельзя.
    assert 0 not in numbers
    assert all(number == 0 for number, item in commands if item is None)


def test_tip_names_state_and_fits_the_os_limit() -> None:
    assert menu.tip("Jarvis", menu.READY) == "Jarvis — слушает"
    assert len(menu.tip("J" * 300, menu.STARTING)) == menu.TIP_LIMIT


# --- без консоли ------------------------------------------------------------


def test_stderr_tail_keeps_only_the_end() -> None:
    tail = StderrTail(limit=10)
    tail.write("начало, которое не нужно ")
    tail.write("конец\n")
    assert tail.text.endswith("конец")
    assert len(tail.text) <= 10
    assert "начало" not in tail.text


def test_current_log_file_is_taken_from_the_file_handler(tmp_path: Path) -> None:
    logger = logging.Logger("tray-test")
    handler = logging.FileHandler(tmp_path / "jarvis-2026-09-14.log", encoding="utf-8")
    logger.addHandler(logging.StreamHandler())
    logger.addHandler(handler)
    try:
        assert current_log_file(logger) == tmp_path / "jarvis-2026-09-14.log"
    finally:
        handler.close()


def test_restart_uses_same_interpreter_and_flags() -> None:
    assert restart_command(["--tray", "--log-level", "DEBUG"]) == [
        sys.executable, "-m", "jarvis", "--tray", "--log-level", "DEBUG",
    ]


def test_tray_flag_is_parsed() -> None:
    assert _parse_args(["--tray"]).tray is True
    assert _parse_args([]).tray is False


# --- связь с приложением ----------------------------------------------------


async def test_quit_from_menu_stops_the_app(tmp_path: Path) -> None:
    session = _session()
    app = _app(tmp_path)
    session.attach(app)
    # Меню работает в чужом потоке: остановка обязана пройти через петлю.
    await asyncio.to_thread(session.on_action, "quit")
    await asyncio.wait_for(app.stopping.wait(), timeout=1.0)
    assert session.restart is False
    assert session.icon.states[-1] == menu.STOPPING  # type: ignore[attr-defined]


async def test_restart_from_menu_stops_and_remembers(tmp_path: Path) -> None:
    session = _session()
    app = _app(tmp_path)
    session.attach(app)
    await asyncio.to_thread(session.on_action, "restart")
    await asyncio.wait_for(app.stopping.wait(), timeout=1.0)
    assert session.restart is True


async def test_quit_before_the_app_is_built_is_not_lost(tmp_path: Path) -> None:
    session = _session()
    session.on_action("quit")
    app = _app(tmp_path)
    session.attach(app)
    assert app.stopping.is_set()


async def test_icon_turns_ready_when_system_started(tmp_path: Path) -> None:
    session = _session()
    app = _app(tmp_path)
    session.attach(app)
    await app.events.publish(SystemStarted(source="app"))
    assert session.icon.states[-1] == menu.READY  # type: ignore[attr-defined]


async def test_log_opens_live_window_on_current_file(tmp_path: Path) -> None:
    watched: list[tuple[Path, str]] = []
    log = tmp_path / "jarvis-2026-09-14.log"
    session = TraySession(
        FakeIcon, opener=lambda path: None,
        live_log=lambda path, level: watched.append((path, level)), log_file=lambda: log,
    )
    session.attach(_app(tmp_path))
    session.on_action("log")
    # Уровень консоли из конфига: окно показывает то же, что консоль.
    assert watched == [(log, "INFO")]


def test_live_log_command_follows_the_file_and_survives_quotes() -> None:
    command = live_log_command(Path("D:/Джарвис's/logs/jarvis.log"), "INFO")
    script = command[-1]
    assert command[0] == "powershell.exe"
    assert "-Wait" in script and "-Encoding UTF8" in script
    # Одинарная кавычка в пути удвоена, иначе строка PowerShell оборвётся.
    assert "Джарвис''s" in script
    # Отладочные строки отсеиваются: DEBUG в список показываемых не входит.
    assert "$levels = @('INFO','WARNING','ERROR','CRITICAL')" in script
    # Цвета как у консоли: предупреждение жёлтое, сказанное зелёное.
    assert "Write-Host" in script and "'Yellow'" in script and "'Отвечаю:'" in script
    assert "__" not in script, "в скрипте осталась неподставленная метка"


def test_panel_window_takes_most_of_the_screen() -> None:
    # Рабочий стол 1920×1140 без масштаба — окно как на снимке владельца.
    x, y, width, height = panel_geometry((0, 0, 1920, 1140), 96)
    assert (width, height) == (1728, 946)
    assert (x, y) == (23, 14)
    # Масштаб 150%: те же доли, но в независимых точках.
    assert panel_geometry((0, 0, 2880, 1710), 144) == (x, y, width, height)


async def test_panel_opens_with_token_from_the_app(tmp_path: Path) -> None:
    opened: list[tuple[str, Any]] = []
    session = TraySession(FakeIcon, opener=lambda path: None, panel=lambda url, saved: opened.append((url, saved)))
    session.attach(_app(tmp_path))
    session.on_action("panel")
    assert opened == [("http://127.0.0.1:8766/?token=t", (100, 50, 1400, 900))]


def test_saved_window_is_used_only_if_it_is_on_screen() -> None:
    screen = (0, 0, 1920, 1200)
    assert fits_screen((100, 50, 1400, 900), screen)
    # Монитор справа отключили: окно осталось бы за краем.
    assert not fits_screen((2100, 50, 1400, 900), screen)
    # Второй монитор слева — отрицательные координаты законны.
    assert fits_screen((-1500, 40, 1200, 800), (-1920, 0, 3840, 1200))


def test_panel_without_app_falls_back_to_log(tmp_path: Path) -> None:
    watched: list[Path] = []
    log = tmp_path / "jarvis.log"
    session = TraySession(
        FakeIcon, opener=lambda path: None, live_log=lambda path, level: watched.append(path),
        log_file=lambda: log, panel=lambda url, saved: None,
    )
    session.on_action("panel")
    assert watched == [log]


def test_panel_window_is_edge_app_mode() -> None:
    command = panel_command("http://127.0.0.1:8766/?token=t", Path("C:/Edge/msedge.exe"))
    assert command is not None and command[1] == "--app=http://127.0.0.1:8766/?token=t"
    assert panel_command("http://x", None) is None


async def test_folder_opens_project_root(tmp_path: Path) -> None:
    opened: list[Path] = []
    session = _session(opened)
    session.attach(_app(tmp_path))
    session.on_action("folder")
    assert opened == [tmp_path]


# --- запуск целиком ---------------------------------------------------------


def test_second_launch_does_not_start(monkeypatch: Any) -> None:
    monkeypatch.delenv(RESTART_ENV, raising=False)
    said: list[str] = []
    ran: list[bool] = []
    code = run_in_tray(
        lambda session: ran.append(True) or 0,
        argv=["--tray"], icon_factory=FakeIcon, lock=FakeLock(free=False),
        message=lambda text, **_: said.append(text),
    )
    assert code == 0 and not ran
    assert "уже запущен" in said[0]


def test_crash_is_shown_and_icon_removed(monkeypatch: Any) -> None:
    monkeypatch.delenv(RESTART_ENV, raising=False)
    said: list[tuple[str, dict[str, Any]]] = []
    sessions: list[TraySession] = []

    def body(session: TraySession) -> int:
        sessions.append(session)
        raise RuntimeError("микрофон пропал")

    code = run_in_tray(
        body, argv=["--tray"], icon_factory=FakeIcon, lock=FakeLock(),
        message=lambda text, **kw: said.append((text, kw)),
    )
    assert code == 1
    assert said and said[0][1].get("error") is True
    assert sessions[0].icon.stopped  # type: ignore[attr-defined]


def test_restart_spawns_new_process_that_waits_for_the_old(monkeypatch: Any) -> None:
    monkeypatch.delenv(RESTART_ENV, raising=False)
    spawned: list[tuple[list[str], dict[str, Any]]] = []
    lock = FakeLock()

    def body(session: TraySession) -> int:
        session.on_action("restart")
        return 0

    code = run_in_tray(
        body, argv=["--tray"], icon_factory=FakeIcon, lock=lock,
        spawn=lambda command, **kw: spawned.append((command, kw)),
        message=lambda *a, **kw: None,
    )
    assert code == 0
    assert lock.released, "новый процесс не должен ждать, пока старый держит место"
    command, options = spawned[0]
    assert command[-1] == "--tray"
    assert options["env"][RESTART_ENV] == "1"


def test_restarted_process_waits_for_the_old_one(monkeypatch: Any) -> None:
    monkeypatch.setitem(os.environ, RESTART_ENV, "1")
    lock = FakeLock()
    run_in_tray(lambda session: 0, argv=["--tray"], icon_factory=FakeIcon, lock=lock,
                message=lambda *a, **kw: None)
    assert lock.waits[0] > 0
    # Флаг одноразовый: следующий перезапуск ставит его заново.
    assert RESTART_ENV not in os.environ


# --- где встаёт меню значка ---------------------------------------------------

MONITOR = (0, 0, 1920, 1200)


def test_menu_stands_above_a_bottom_taskbar() -> None:
    """Живой случай: меню у курсора уходило под панель задач внизу."""
    from jarvis.core.tray.win32 import TPM_BOTTOMALIGN, menu_placement

    x, y, align, exclude = menu_placement((1700, 1180), MONITOR, (0, 0, 1920, 1152))
    assert (x, y) == (1700, 1152)
    assert align & TPM_BOTTOMALIGN
    assert exclude == (0, 1152, 1920, 1200)


def test_menu_hangs_below_a_top_taskbar() -> None:
    from jarvis.core.tray.win32 import TPM_BOTTOMALIGN, menu_placement

    x, y, align, exclude = menu_placement((1700, 20), MONITOR, (0, 48, 1920, 1200))
    assert (x, y) == (1700, 48) and not align & TPM_BOTTOMALIGN
    assert exclude == (0, 0, 1920, 48)


def test_menu_leaves_a_side_taskbar_alone() -> None:
    from jarvis.core.tray.win32 import TPM_RIGHTALIGN, menu_placement

    x, y, align, exclude = menu_placement((1900, 1100), MONITOR, (0, 0, 1860, 1200))
    assert (x, y) == (1860, 1100) and align & TPM_RIGHTALIGN
    assert exclude == (1860, 0, 1920, 1200)
    left = menu_placement((10, 1100), MONITOR, (60, 0, 1920, 1200))
    assert left[:2] == (60, 1100) and left[3] == (0, 0, 60, 1200)


def test_menu_from_the_hidden_icons_flyout_stays_at_the_cursor() -> None:
    from jarvis.core.tray.win32 import menu_placement

    assert menu_placement((1500, 1000), MONITOR, (0, 0, 1920, 1152))[1::2] == (1000, None)


# --- своё меню: раскладка и место --------------------------------------------


def test_popup_layout_scales_and_skips_separators() -> None:
    layout = menu.popup_layout(menu.MENU, 1.5)
    assert layout.width == 372 and layout.header == 84
    items = [row for row in layout.rows if row.item is not None]
    assert len(items) == 5 and all(row.height == 51 for row in items)
    assert layout.height == layout.rows[-1].top + layout.rows[-1].height + 9
    separator = next(index for index, row in enumerate(layout.rows) if row.item is None)
    assert menu.row_at(layout, layout.rows[separator].top + 1) is None
    assert menu.row_at(layout, 10) is None  # шапка
    assert menu.row_at(layout, layout.rows[0].top) == 0


def test_arrows_walk_items_around_the_separator() -> None:
    layout = menu.popup_layout()
    assert menu.step_row(layout, None, 1) == 0
    assert menu.step_row(layout, None, -1) == 5
    assert menu.step_row(layout, 2, 1) == 4  # через разделитель
    assert menu.step_row(layout, 5, 1) == 0  # по кругу


def test_popup_hovers_above_the_taskbar_with_a_gap() -> None:
    """Просьба владельца: зазор между панелью задач и меню."""
    work = (0, 0, 1920, 1152)
    x, y = menu.popup_position((1700, 1180), MONITOR, work, (248, 300), 8)
    assert y + 300 == 1152 - 8
    assert x == 1700 - 124
    # У правого края меню не вылезает за экран.
    x, _ = menu.popup_position((1915, 1180), MONITOR, work, (248, 300), 8)
    assert x + 248 == 1920 - 8


def test_popup_for_other_taskbar_edges() -> None:
    assert menu.popup_position((900, 10), MONITOR, (0, 48, 1920, 1200), (248, 300), 8)[1] == 56
    assert menu.popup_position((1900, 600), MONITOR, (0, 0, 1860, 1200), (248, 300), 8)[0] == 1860 - 8 - 248
    assert menu.popup_position((10, 600), MONITOR, (60, 0, 1920, 1200), (248, 300), 8)[0] == 68


def test_native_menu_keeps_the_same_gap() -> None:
    from jarvis.core.tray.win32 import menu_placement

    _, y, _, exclude = menu_placement((1700, 1180), MONITOR, (0, 0, 1920, 1152), gap=8)
    assert y == 1144 and exclude == (0, 1144, 1920, 1200)


def test_quit_is_the_only_danger_item() -> None:
    assert [item.action for item in menu.MENU if item is not None and item.danger] == ["quit"]
