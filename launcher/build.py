"""Собрать Jarvis.exe — запускатель без окна консоли.

    python launcher/build.py              # запрашивает права администратора
    python launcher/build.py --no-admin   # запускается от обычного пользователя

Компилятор C# входит в .NET Framework, который стоит в любой Windows, поэтому
ставить ничего не нужно. Готовый файл ложится в корень проекта — оттуда он
находит папку `jarvis` — и в репозиторий не едет: собирается за секунду.

**Права администратора по умолчанию**, потому что так Jarvis запускается у
владельца и сейчас. Запущенный с правами запускатель передаёт их `pythonw`, и
ассистент может управлять программами, запущенными от администратора. Цена —
окно UAC на каждом запуске.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = HERE / "Jarvis.cs"
ICON = ROOT / "jarvis" / "core" / "tray" / "jarvis.ico"
OUT = ROOT / "Jarvis.exe"

MANIFEST = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="1.0.0.0" name="Jarvis.Launcher"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v2">
    <security>
      <requestedPrivileges xmlns="urn:schemas-microsoft-com:asm.v3">
        <requestedExecutionLevel level="{level}" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>
"""


def compiler() -> Path:
    """Путь к csc.exe из .NET Framework 4."""
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for bits in ("Framework64", "Framework"):
        candidate = windows / "Microsoft.NET" / bits / "v4.0.30319" / "csc.exe"
        if candidate.exists():
            return candidate
    raise SystemExit("Не нашёл csc.exe: нужен .NET Framework 4, он есть в любой Windows 10 и 11")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-admin", action="store_true", help="не запрашивать права администратора")
    args = parser.parse_args()

    level = "asInvoker" if args.no_admin else "requireAdministrator"
    with tempfile.TemporaryDirectory() as scratch:
        manifest = Path(scratch) / "app.manifest"
        manifest.write_text(MANIFEST.format(level=level), encoding="utf-8")
        result = subprocess.run(
            [
                str(compiler()), "/nologo", "/target:winexe", "/optimize+",
                # Исходник в UTF-8: без этого кириллица в сообщениях станет мусором.
                "/codepage:65001",
                f"/out:{OUT}", f"/win32icon:{ICON}", f"/win32manifest:{manifest}",
                str(SOURCE),
            ],
            capture_output=True, text=True, encoding="cp866", errors="replace",
        )
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
        return result.returncode
    rights = "обычный пользователь" if args.no_admin else "администратор"
    print(f"Собрано: {OUT} ({OUT.stat().st_size // 1024} КБ, права: {rights})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
