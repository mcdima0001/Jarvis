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
    live_log_command,
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
        config=SimpleNamespace(app=SimpleNamespace(name="Jarvis"), root=root),
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


def test_log_opens_live_window_on_current_file(tmp_path: Path) -> None:
    watched: list[Path] = []
    log = tmp_path / "jarvis-2026-09-14.log"
    session = TraySession(
        FakeIcon, opener=lambda path: None, live_log=watched.append, log_file=lambda: log
    )
    session.on_action("log")
    assert watched == [log]


def test_live_log_command_follows_the_file_and_survives_quotes() -> None:
    command = live_log_command(Path("D:/Джарвис's/logs/jarvis.log"))
    script = command[-1]
    assert command[0] == "powershell.exe"
    assert "-Wait" in script and "-Encoding UTF8" in script
    # Одинарная кавычка в пути удвоена, иначе строка PowerShell оборвётся.
    assert "Джарвис''s" in script


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
