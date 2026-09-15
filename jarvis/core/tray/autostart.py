"""Запуск Jarvis вместе с Windows: задание Планировщика при входе в систему.

**Планировщик, а не ключ `Run` в реестре**, и это не вкус. `Jarvis.exe` по
умолчанию просит права администратора, а программы с таким манифестом Windows
из `Run` молча не запускает — автозапуск выглядел бы включённым и не работал.
Задание с «наивысшими правами» поднимает exe с правами и **без окна UAC**.
Заодно у задания снимаются ноутбучные ловушки по умолчанию: «не запускать от
батареи» и «остановить через трое суток».

Создать задание с наивысшими правами может только процесс с правами — то есть
Jarvis, запущенный через такой же `Jarvis.exe`. Иначе `schtasks` отказывает, и
отказ говорится человеческими словами.

Здесь чистые функции (текст задания, команды) и тонкий `Autostart`, который их
выполняет: проверяются тесты на любой машине.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

#: Имя задания в Планировщике.
TASK_NAME = "Jarvis"
#: Пауза после входа: пусть звук и сеть поднимутся раньше ассистента.
LOGON_DELAY = "PT15S"
#: Приоритет процесса 4 — обычный. По умолчанию у заданий 7, ниже обычного:
#: голосовому ассистенту это вредно.
PRIORITY = 4

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Запуск голосового ассистента Jarvis при входе в Windows.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
      <Delay>{delay}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>{priority}</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <WorkingDirectory>{folder}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class AutostartError(Exception):
    """Автозапуск не включился или не выключился — текст для человека."""


def current_user() -> str:
    """Пользователь в виде «ДОМЕН\\имя»: так его ждёт Планировщик."""
    name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{name}" if domain else name


def task_xml(exe: Path, *, user: str, elevated: bool) -> str:
    """Текст задания: при входе этого пользователя запустить exe из его папки."""
    return _TASK_XML.format(
        user=escape(user),
        delay=LOGON_DELAY,
        level="HighestAvailable" if elevated else "LeastPrivilege",
        priority=PRIORITY,
        command=escape(str(exe)),
        folder=escape(str(exe.parent)),
    )


def query_command() -> list[str]:
    """Команда проверки: код возврата 0 — задание есть."""
    return ["schtasks", "/Query", "/TN", TASK_NAME]


def create_command(xml_file: Path) -> list[str]:
    """Команда создания задания из файла; `/F` — заменить прежнее."""
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_file), "/F"]


def delete_command() -> list[str]:
    """Команда удаления задания."""
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def wants_elevation(exe_data: bytes) -> bool:
    """Просит ли собранный exe права администратора: манифест лежит в нём текстом."""
    return b"requireAdministrator" in exe_data


def explain_failure(output: str, *, elevated: bool, admin: bool) -> str:
    """Отказ `schtasks` человеческими словами."""
    if elevated and not admin:
        return (
            "Jarvis.exe просит права администратора, а этот Jarvis запущен без них — "
            "задание с правами создать нельзя. Запусти Jarvis через Jarvis.exe и включи ещё раз."
        )
    text = " ".join(output.split())
    return f"Планировщик отказал: {text[-300:]}" if text else "Планировщик отказал без объяснений."


def _run_hidden(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Выполнить консольную команду без мелькающего окна (`pythonw`)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, capture_output=True, timeout=30, creationflags=flags, check=False)


def _decode(data: bytes | None) -> str:
    """Вывод `schtasks` — в кодировке консоли, а не в UTF-8."""
    return (data or b"").decode("cp866" if sys.platform == "win32" else "utf-8", errors="replace")


class Autostart:
    """Включить, выключить и узнать, стоит ли автозапуск.

    Состояние помнится `CHECK_TTL_S`: меню трея спрашивает его на каждом
    открытии, а `schtasks` — это процесс и десятая доля секунды. Надолго
    запоминать нельзя: автозапуск переключают и из панели, другим экземпляром.
    """

    #: Сколько секунд верить прошлой проверке.
    CHECK_TTL_S = 10.0

    def __init__(
        self,
        root: Path,
        *,
        run: Callable[[list[str]], Any] = _run_hidden,
        is_admin: Callable[[], bool] | None = None,
        user: Callable[[], str] = current_user,
    ) -> None:
        self.exe = root / "Jarvis.exe"
        self._run = run
        self._is_admin = is_admin
        self._user = user
        self._enabled: bool | None = None
        self._checked_at = 0.0

    @property
    def supported(self) -> bool:
        """Автозапуск есть только у Windows."""
        return sys.platform == "win32"

    def enabled(self) -> bool:
        """Стоит ли задание в Планировщике."""
        if not self.supported:
            return False
        if self._enabled is None or time.monotonic() - self._checked_at > self.CHECK_TTL_S:
            try:
                self._remember(self._run(query_command()).returncode == 0)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("Автозапуск: не смог спросить Планировщик (%s)", exc)
                return False
        return bool(self._enabled)

    def _remember(self, enabled: bool) -> None:
        self._enabled, self._checked_at = enabled, time.monotonic()

    def set(self, enabled: bool) -> None:
        """Включить или выключить. Отказ — `AutostartError` с объяснением."""
        if not self.supported:
            raise AutostartError("Автозапуск умею ставить только в Windows.")
        if enabled:
            self._create()
        else:
            self._delete()
        self._remember(enabled)
        logger.info("Автозапуск с Windows %s", "включён" if enabled else "выключен")

    def toggle(self) -> bool:
        """Переключить; возвращает новое состояние."""
        target = not self.enabled()
        self.set(target)
        return target

    def _create(self) -> None:
        if not self.exe.exists():
            raise AutostartError(
                "Нет Jarvis.exe: автозапуск запускает его. Собери: python launcher/build.py."
            )
        elevated = wants_elevation(self.exe.read_bytes())
        xml = task_xml(self.exe, user=self._user(), elevated=elevated)
        folder = Path(tempfile.mkdtemp(prefix="jarvis-task-"))
        path = folder / "task.xml"
        try:
            # Планировщик читает файл задания только в UTF-16, как объявлено в заголовке.
            path.write_text(xml, encoding="utf-16")
            result = self._run(create_command(path))
        finally:
            path.unlink(missing_ok=True)
            folder.rmdir()
        if result.returncode != 0:
            admin = self._is_admin() if self._is_admin is not None else _process_is_admin()
            output = _decode(result.stderr) or _decode(result.stdout)
            raise AutostartError(explain_failure(output, elevated=elevated, admin=admin))

    def _delete(self) -> None:
        if not self.enabled():
            return
        result = self._run(delete_command())
        if result.returncode != 0:
            output = _decode(result.stderr) or _decode(result.stdout)
            raise AutostartError(explain_failure(output, elevated=False, admin=True))


def _process_is_admin() -> bool:
    from jarvis.core.gui.settings import is_admin

    return is_admin()
