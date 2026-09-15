"""Автозапуск с Windows: текст задания Планировщика и переключение без настоящего schtasks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.tray import autostart
from jarvis.core.tray.autostart import (
    TASK_NAME,
    Autostart,
    AutostartError,
    create_command,
    delete_command,
    task_xml,
    wants_elevation,
)


def test_task_runs_exe_at_logon_with_rights_and_on_battery() -> None:
    xml = task_xml(Path(r"C:\Studio & Co\Jarvis\Jarvis.exe"), user=r"PC\dima", elevated=True)
    assert "<RunLevel>HighestAvailable</RunLevel>" in xml
    assert "<LogonTrigger>" in xml and r"<UserId>PC\dima</UserId>" in xml
    # Ноутбучные ловушки заданий по умолчанию сняты.
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    # Путь экранирован, рабочая папка — папка exe.
    assert r"<Command>C:\Studio &amp; Co\Jarvis\Jarvis.exe</Command>" in xml
    assert r"<WorkingDirectory>C:\Studio &amp; Co\Jarvis</WorkingDirectory>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in task_xml(Path("J.exe"), user="u", elevated=False)


def test_elevation_is_read_from_the_launcher_manifest() -> None:
    assert wants_elevation(b'...level="requireAdministrator" uiAccess...')
    assert not wants_elevation(b'...level="asInvoker"...')


class FakeSchtasks:
    """schtasks без системы: помнит, есть ли задание, и что ему прислали."""

    def __init__(self, *, exists: bool = False, refuse: bool = False) -> None:
        self.exists, self.refuse = exists, refuse
        self.commands: list[list[str]] = []
        self.xml = ""

    def __call__(self, command: list[str]) -> Any:
        self.commands.append(command)
        verb = command[1]
        code = 0
        if verb == "/Query":
            code = 0 if self.exists else 1
        elif self.refuse:
            code = 1
        elif verb == "/Create":
            self.xml = Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16")
            self.exists = True
        elif verb == "/Delete":
            self.exists = False
        return SimpleNamespace(returncode=code, stdout=b"", stderr="ОШИБКА: Отказано в доступе.".encode("cp866"))


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart.sys, "platform", "win32")


def _starter(root: Path, schtasks: FakeSchtasks, *, admin: bool = True) -> Autostart:
    return Autostart(root, run=schtasks, is_admin=lambda: admin, user=lambda: r"PC\dima")


def test_toggle_creates_and_deletes_the_task(tmp_path: Path, windows: None) -> None:
    (tmp_path / "Jarvis.exe").write_bytes(b'level="requireAdministrator"')
    schtasks = FakeSchtasks()
    starter = _starter(tmp_path, schtasks)
    assert not starter.enabled()
    assert starter.toggle() is True
    assert "HighestAvailable" in schtasks.xml and str(tmp_path / "Jarvis.exe") in schtasks.xml
    # Временный файл задания не остаётся.
    assert not Path(schtasks.commands[-1][schtasks.commands[-1].index("/XML") + 1]).exists()
    assert starter.enabled()
    assert starter.toggle() is False
    assert schtasks.commands[-1] == delete_command() and not schtasks.exists


def test_without_launcher_explains_how_to_build_it(tmp_path: Path, windows: None) -> None:
    with pytest.raises(AutostartError, match="launcher/build.py"):
        _starter(tmp_path, FakeSchtasks()).set(True)


def test_refusal_without_rights_is_explained(tmp_path: Path, windows: None) -> None:
    (tmp_path / "Jarvis.exe").write_bytes(b'level="requireAdministrator"')
    with pytest.raises(AutostartError, match="без них"):
        _starter(tmp_path, FakeSchtasks(refuse=True), admin=False).set(True)
    (tmp_path / "Jarvis.exe").write_bytes(b'level="asInvoker"')
    with pytest.raises(AutostartError, match="Отказано в доступе"):
        _starter(tmp_path, FakeSchtasks(refuse=True), admin=False).set(True)


def test_commands_name_the_task() -> None:
    assert create_command(Path("t.xml"))[:4] == ["schtasks", "/Create", "/TN", TASK_NAME]
    assert delete_command()[-1] == "/F"


def test_outside_windows_it_is_simply_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    schtasks = FakeSchtasks(exists=True)
    assert not _starter(tmp_path, schtasks).enabled() and schtasks.commands == []
