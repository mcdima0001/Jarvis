r"""«Джарвис» вместо «Python» в диспетчере задач.

Просьба владельца 23.09.2026: «можем его Джарвисом назвать, чтобы не путать?».
В диспетчере процесс подписан «Python», потому что Windows берёт название из
**описания файла** внутри `pythonw.exe`, а не из имени процесса.

Поэтому рядом с проектом кладётся копия интерпретатора — `JarvisHost.exe`, — у
которой это описание переписано на «Jarvis». Приём держится на удаче: слова
«Python» и «Jarvis» одной длины, значит строку можно заменить прямо в ресурсе,
не пересобирая его структуру.

    C:\Python314\python.exe launcher/host.py          # собрать JarvisHost.exe
    C:\Python314\python.exe launcher/host.py --check  # посмотреть, что вышло

Копии нужен `PYTHONHOME`: стандартную библиотеку интерпретатор ищет рядом с
собой, а рядом с ней её нет. Переменную ставит запускатель `Jarvis.exe` — он и
так находит настоящий `pythonw.exe`, чтобы узнать эту папку. Из venv так
запускать нельзя: `PYTHONHOME` его перебьёт.

Сам бинарник в `.gitignore`: это копия чужого файла, ей в репозитории не место.
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

#: Как процесс подписан сейчас и как он должен быть подписан.
WAS, NOW = "Python", "Jarvis"

#: Тип ресурса с версией файла и язык «нейтральный» — именно там у Python
#: лежит описание (проверено на `pythonw.exe` 3.14.4).
RT_VERSION = 16
NEUTRAL = 0

ROOT = Path(__file__).resolve().parent.parent
#: Имя копии. Не `Jarvis.exe`: так зовут запускатель, и два одинаковых имени
#: рядом путали бы сильнее, чем «Python».
HOST = ROOT / "JarvisHost.exe"


def description(path: Path) -> str:
    """Как файл подписан — то самое, что показывает диспетчер задач."""
    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW(str(path), 0, size, data)
    block = ctypes.c_void_p()
    length = wintypes.UINT()
    found = version.VerQueryValueW(
        data, r"\StringFileInfo\000004b0\FileDescription", ctypes.byref(block), ctypes.byref(length)
    )
    return ctypes.wstring_at(block.value, length.value - 1) if found else ""


def _resource(path: Path) -> bytes:
    """Ресурс версии как есть — его и правим."""
    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise SystemExit(f"У {path.name} нет ресурса версии — переписывать нечего.")
    data = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW(str(path), 0, size, data)
    return data.raw[:size]


def rename(source: Path, target: Path) -> None:
    """Сделать копию интерпретатора, подписанную «Jarvis»."""
    shutil.copy2(source, target)
    raw = _resource(source)
    # Строки в ресурсе лежат в UTF-16; длина слов совпадает, поэтому замена
    # ничего не сдвигает и структуру пересобирать не нужно.
    patched = raw.replace(WAS.encode("utf-16-le"), NOW.encode("utf-16-le"))
    if patched == raw:
        raise SystemExit(f"В ресурсе {source.name} не нашлось слова «{WAS}».")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

    handle = kernel32.BeginUpdateResourceW(str(target), False)
    if not handle:
        raise SystemExit(f"Не открыть ресурсы {target.name}: ошибка {ctypes.get_last_error()}")
    buffer = ctypes.create_string_buffer(patched, len(patched))
    ok = kernel32.UpdateResourceW(
        handle,
        ctypes.cast(RT_VERSION, wintypes.LPCWSTR),
        ctypes.cast(1, wintypes.LPCWSTR),
        NEUTRAL,
        ctypes.cast(buffer, wintypes.LPVOID),
        len(patched),
    )
    if not ok or not kernel32.EndUpdateResourceW(handle, False):
        raise SystemExit(f"Не записать ресурс: ошибка {ctypes.get_last_error()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Копия интерпретатора с именем Jarvis")
    parser.add_argument("--check", action="store_true", help="только показать подписи")
    parser.add_argument("--python", default="", help="какой интерпретатор копировать")
    args = parser.parse_args()

    source = Path(args.python) if args.python else Path(sys.executable).with_name("pythonw.exe")
    if args.check:
        for path in (source, HOST):
            print(f"{path}: {description(path) or '(без описания)'}" if path.exists()
                  else f"{path}: нет файла")
        return 0

    if not source.exists():
        print(f"Не нашёл {source} — укажи интерпретатор через --python", file=sys.stderr)
        return 1
    rename(source, HOST)
    print(f"{HOST.name}: подписан «{description(HOST)}» (был «{description(source)}»)")
    print("Пересобери запускатель: python launcher/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
