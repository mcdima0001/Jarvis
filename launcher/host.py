r"""«Джарвис» вместо «Python» в диспетчере задач — и подписью, и значком.

Просьба владельца 23.09.2026: «можем его Джарвисом назвать, чтобы не путать?».
В диспетчере процесс подписан «Python», потому что Windows берёт название из
**описания файла** внутри `pythonw.exe`, а не из имени процесса.

Поэтому рядом с проектом кладётся копия интерпретатора — `JarvisHost.exe`, — у
которой это описание переписано на «Jarvis». Приём держится на удаче: слова
«Python» и «Jarvis» одной длины, значит строку можно заменить прямо в ресурсе,
не пересобирая его структуру.

**Значок оттуда же** (24.09.2026). Подпись сменилась, а картинка осталась
питоновская, и владелец увидел в диспетчере «Jarvis» с чужим значком: и он, и
проводник берут значок из ресурсов того же файла. Теперь копии подкладывается
`jarvis.ico` — тот же, что у трея и у запускателя, один облик на весь Jarvis.
Старые значки при этом **удаляются, а не оставляются рядом**: показывается
группа с наименьшим номером, и оставленная чужая выиграла бы. Соседние ресурсы
не трогаются — в манифесте объявлены права и осведомлённость о масштабе, и
снести его заодно значило бы поменять поведение интерпретатора.

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
import struct
import sys
from contextlib import suppress
from itertools import count
from pathlib import Path

# Ресурсы правятся только на Windows, но разбор файла значков — обычный разбор
# байтов, и проверяют его тесты на сервере. Поэтому ввоз всего оконного стоит
# под условием: без него модуль не ввозится вовсе, а `ctypes.wintypes` на Linux
# отказывается загружаться.
if sys.platform == "win32":
    from ctypes import wintypes

#: Как процесс подписан сейчас и как он должен быть подписан.
WAS, NOW = "Python", "Jarvis"

#: Типы ресурсов и язык «нейтральный» — там у Python лежат описание и значки
#: (проверено на `pythonw.exe` 3.14.4).
RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16
NEUTRAL = 0

#: Читать чужой файл только ради ресурсов: код из него не исполняется.
LOAD_LIBRARY_AS_DATAFILE = 0x2

ROOT = Path(__file__).resolve().parent.parent
#: Имя копии. Не `Jarvis.exe`: так зовут запускатель, и два одинаковых имени
#: рядом путали бы сильнее, чем «Python».
HOST = ROOT / "JarvisHost.exe"
#: Значок — тот же, что у трея и у запускателя.
ICON = ROOT / "jarvis" / "core" / "tray" / "jarvis.ico"

#: Заголовок файла значков и одна запись в нём. На диске и в ресурсе записи
#: различаются последним полем: там смещение картинки, здесь её номер.
ICONDIR = struct.Struct("<HHH")
ICONDIRENTRY = struct.Struct("<BBBBHHII")
GRPICONDIRENTRY = struct.Struct("<BBBBHHIH")


def icon_group(raw: bytes) -> tuple[bytes, list[bytes]]:
    """Разобрать содержимое `.ico` на опись значков и сами картинки.

    Опись (`RT_GROUP_ICON`) — это тот же заголовок файла, но вместо смещений в
    нём номера ресурсов; по ней Windows и выбирает нужный размер. Функция
    чистая и от Windows не зависит, поэтому её проверяют тесты.
    """
    if len(raw) < ICONDIR.size:
        raise ValueError("Файл значков пуст.")
    _, kind, count = ICONDIR.unpack_from(raw)
    if kind != 1 or not count:
        raise ValueError("Это не файл значков.")
    group = bytearray(ICONDIR.pack(0, 1, count))
    images: list[bytes] = []
    for number in range(count):
        entry = ICONDIRENTRY.unpack_from(raw, ICONDIR.size + number * ICONDIRENTRY.size)
        *head, size, offset = entry
        image = raw[offset:offset + size]
        if len(image) != size:
            raise ValueError(f"Значок №{number + 1} обрезан.")
        images.append(image)
        group += GRPICONDIRENTRY.pack(*head, size, number + 1)
    return bytes(group), images


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


def _kernel32() -> ctypes.WinDLL:
    """Объявленный kernel32: без типов дескрипторы уезжают в 32 бита."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, NAMES, ctypes.c_void_p]
    kernel32.EnumResourceLanguagesW.argtypes = [
        wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, LANGUAGES, ctypes.c_void_p
    ]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    return kernel32


#: Обходчики ресурсов. Имя приходит либо номером, либо указателем на строку,
#: поэтому берём его нетипизированным: иначе ctypes полезет по номеру как по
#: адресу и уронит процесс.
if sys.platform == "win32":
    NAMES = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )
    LANGUAGES = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p,
    )


def _existing(path: Path, kind: int) -> list[tuple[int | str, int]]:
    """Какие ресурсы такого типа уже лежат в файле: имя и язык каждого."""
    kernel32 = _kernel32()
    module = kernel32.LoadLibraryExW(str(path), None, LOAD_LIBRARY_AS_DATAFILE)
    if not module:
        return []
    names: list[int | str] = []

    @NAMES
    def keep_name(_module: int, _kind: int, name: int, _param: int) -> bool:
        value = name or 0
        names.append(value if value >> 16 == 0 else ctypes.wstring_at(value))
        return True

    found: list[tuple[int | str, int]] = []
    try:
        kernel32.EnumResourceNamesW(module, ctypes.cast(kind, ctypes.c_void_p), keep_name, None)
        for name in names:
            languages = _languages(kernel32, module, kind, name)
            found.extend((name, language) for language in languages or [NEUTRAL])
    finally:
        kernel32.FreeLibrary(module)
    return found


def _languages(kernel32: ctypes.WinDLL, module: int, kind: int, name: int | str) -> list[int]:
    """На каких языках лежит ресурс: удалять его надо ровно там, где он есть.

    Отдельной функцией, а не замыканием внутри цикла: такое замыкание держит
    переменную цикла, а не её значение, и линтер справедливо на это ругается.
    """
    languages: list[int] = []

    @LANGUAGES
    def keep(_m: int, _k: int, _n: int, language: int, _p: int) -> bool:
        languages.append(language)
        return True

    handle = ctypes.cast(name, ctypes.c_void_p) if isinstance(name, int) else \
        ctypes.cast(ctypes.create_unicode_buffer(name), ctypes.c_void_p)
    kernel32.EnumResourceLanguagesW(module, ctypes.cast(kind, ctypes.c_void_p), handle, keep, None)
    return languages


def _named(value: int | str) -> object:
    """Имя ресурса в том виде, в каком его ждёт `UpdateResourceW`."""
    return ctypes.cast(value, wintypes.LPCWSTR) if isinstance(value, int) else value


def _make_room(target: Path) -> None:
    """Освободить имя, даже если под ним прямо сейчас работает ассистент.

    Запущенный exe Windows перезаписать не даёт, а пересобирают копию как раз
    во время работы. Переименовать работающий файл она при этом позволяет:
    процесс держит содержимое, а не имя. Старый файл остаётся лежать до
    перезапуска и выносится следующей сборкой — своего имени он не занимает.
    """
    for stale in target.parent.glob(f"{target.name}.old*"):
        with suppress(OSError):
            stale.unlink()
    if not target.exists():
        return
    try:
        target.unlink()
    except PermissionError:
        aside = next(
            path for number in count(1)
            if not (path := target.with_name(f"{target.name}.old{number}")).exists()
        )
        target.rename(aside)
        print(f"{target.name} занят работающим Jarvis — старый отложен в {aside.name}")


def rename(source: Path, target: Path, *, icon: Path | None = None) -> None:
    """Сделать копию интерпретатора, подписанную «Jarvis» и со своим значком."""
    _make_room(target)
    shutil.copy2(source, target)
    raw = _resource(source)
    # Строки в ресурсе лежат в UTF-16; длина слов совпадает, поэтому замена
    # ничего не сдвигает и структуру пересобирать не нужно.
    patched = raw.replace(WAS.encode("utf-16-le"), NOW.encode("utf-16-le"))
    if patched == raw:
        raise SystemExit(f"В ресурсе {source.name} не нашлось слова «{WAS}».")

    group, images = (b"", [])
    old: list[tuple[int, int | str, int]] = []
    if icon is not None:
        try:
            group, images = icon_group(icon.read_bytes())
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Не прочитать {icon}: {exc}") from exc
        for kind in (RT_ICON, RT_GROUP_ICON):
            old.extend((kind, name, language) for name, language in _existing(source, kind))
    # Язык у нового значка тот же, что у старого: так Windows найдёт его там
    # же, где искала питоновский.
    language = next((lang for kind, _, lang in old if kind == RT_GROUP_ICON), NEUTRAL)

    kernel32 = _kernel32()
    handle = kernel32.BeginUpdateResourceW(str(target), False)
    if not handle:
        raise SystemExit(f"Не открыть ресурсы {target.name}: ошибка {ctypes.get_last_error()}")

    def write(kind: int, name: int | str, lang: int, data: bytes | None) -> None:
        buffer = ctypes.create_string_buffer(data, len(data)) if data else None
        ok = kernel32.UpdateResourceW(
            handle,
            ctypes.cast(kind, wintypes.LPCWSTR),
            _named(name),
            lang,
            ctypes.cast(buffer, wintypes.LPVOID) if buffer else None,
            len(data) if data else 0,
        )
        if not ok:
            raise SystemExit(f"Не записать ресурс {kind}/{name}: ошибка {ctypes.get_last_error()}")

    write(RT_VERSION, 1, NEUTRAL, patched)
    for kind, name, lang in old:
        write(kind, name, lang, None)
    for number, image in enumerate(images, start=1):
        write(RT_ICON, number, language, image)
    if group:
        write(RT_GROUP_ICON, 1, language, group)

    if not kernel32.EndUpdateResourceW(handle, False):
        raise SystemExit(f"Не записать ресурс: ошибка {ctypes.get_last_error()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Копия интерпретатора с именем Jarvis")
    parser.add_argument("--check", action="store_true", help="только показать подписи")
    parser.add_argument("--python", default="", help="какой интерпретатор копировать")
    parser.add_argument("--no-icon", action="store_true", help="оставить значок интерпретатора")
    args = parser.parse_args()

    source = Path(args.python) if args.python else Path(sys.executable).with_name("pythonw.exe")
    if args.check:
        for path in (source, HOST):
            if not path.exists():
                print(f"{path}: нет файла")
                continue
            icons = len(_existing(path, RT_GROUP_ICON))
            print(f"{path}: {description(path) or '(без описания)'}, групп значков {icons}")
        return 0

    if not source.exists():
        print(f"Не нашёл {source} — укажи интерпретатор через --python", file=sys.stderr)
        return 1
    icon = None if args.no_icon else ICON
    if icon is not None and not icon.exists():
        print(f"Не нашёл значок {icon} — собери его: python tools/make_icon.py", file=sys.stderr)
        return 1
    rename(source, HOST, icon=icon)
    print(f"{HOST.name}: подписан «{description(HOST)}» (был «{description(source)}»)")
    if icon is not None:
        print(f"Значок: {icon.name}")
    print("Пересобери запускатель: python launcher/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
