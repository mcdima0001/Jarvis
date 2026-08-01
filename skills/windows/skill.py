"""Управление компьютером студии: запуск программ, блокировка, громкость.

Скилл объявлен только для Windows. На других системах менеджер его пропустит —
это штатное поведение, а не ошибка сборки, поэтому импорты Windows-only лежат
внутри методов.

**Про безопасность.** Название программы приезжает сюда длинной дорогой:
микрофон → Whisper → языковая модель → аргумент инструмента. На каждом шаге оно
может превратиться во что угодно, а Jarvis запускается с правами
администратора. Поэтому здесь нет ни одного вызова оболочки со строкой:
услышанное сначала **сопоставляется с известным списком** (программы из
конфига, ярлыки меню «Пуск», встроенные средства Windows), и запускается только
то, что в списке нашлось. Не нашлось — отказ с подсказкой, а не попытка
выполнить услышанное.

Список установленного собирается сам из меню «Пуск», поэтому Steam, OBS и
всё остальное доступны голосом без единой строчки в конфиге.
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import io
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Container, Mapping, Sequence

from jarvis.core.contracts import (
    AssistantReplied,
    Event,
    ToolResult,
    VoiceCommandRecognized,
    WakeWordDetected,
)
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import romanize, skeleton, squash
from jarvis.core.tools import tool

#: Встроенные средства Windows: в меню «Пуск» лежат не все.
BUILT_IN: dict[str, str] = {
    "проводник": "explorer.exe",
    "explorer": "explorer.exe",
    "блокнот": "notepad.exe",
    "notepad": "notepad.exe",
    "калькулятор": "calc.exe",
    "calculator": "calc.exe",
    "диспетчер задач": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "панель управления": "control.exe",
    "control panel": "control.exe",
    "параметры": "ms-settings:",
    "настройки": "ms-settings:",
    "settings": "ms-settings:",
    "командная строка": "cmd.exe",
    "терминал": "wt.exe",
    "terminal": "wt.exe",
    "paint": "mspaint.exe",
    "микшер": "sndvol.exe",
    "volume mixer": "sndvol.exe",
}

#: Ярлыки, которые в меню «Пуск» есть, а запускать их никто не просит.
_SKIP_SHORTCUT = re.compile(
    r"uninstall|удалить|remove|readme|прочти|документация|documentation|"
    r"справка|help|website|сайт|manual|руководство",
    re.IGNORECASE,
)

#: Имя процесса для taskkill: только то, что не может оказаться чем-то иным.
_PROCESS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\.exe$")

#: Насколько похожими должны быть названия, чтобы счесть их одним и тем же.
#: Порог низкий: транслитерация огрубляет слова, «влс» против «vlc» даёт всего
#: 0.67. Запас до ближайшего известного ложного срабатывания («трамп» против
#: «telegram», 0.62) невелик — понижать дальше нельзя.
_SIMILARITY = 0.66


#: Слова, которые в названиях есть у всех и ничего не различают.
_GENERIC = frozenset({
    "studio", "desktop", "launcher", "edition", "player", "manager", "suite",
    "app", "application", "browser", "client", "tools", "media", "file",
    "files", "games", "game", "experience", "adobe", "microsoft", "mozilla",
    "google", "nvidia", "the", "for", "and", "x64", "x86", "bit", "beta",
})


def _significant_words(text: str) -> list[str]:
    """Слова названия, по которым его реально узнают вслух.

    В меню «Пуск» программы подписаны полностью — «Mozilla Firefox», «Adobe
    Photoshop 2024», — а произносят из этого одно слово, и не всегда первое.
    """
    words = re.split(r"[\s(\[\]/_,.-]+", text.strip().lower())
    return [
        word
        for word in words
        if len(word) >= 3 and word not in _GENERIC and not word.isdigit()
    ]


def _keys(text: str, *, split: bool = True) -> tuple[str, ...]:
    """Написания, по которым название можно узнать.

    :param split: разбирать ли текст на отдельные слова. Для названий из меню
        «Пуск» это нужно («Mozilla Firefox» зовут «файрфокс»), а для услышанного
        запроса — вредно: служебные слова начинают походить на программы.
        «Что такое питон» открывало Task Manager, потому что «такое» похоже
        на «task».
    """
    lowered = text.strip().lower()
    variants = [lowered, romanize(lowered)]
    if split:
        for word in _significant_words(text):
            variants += [word, romanize(word)]

    keys = (squash(variant) for variant in variants)
    return tuple(dict.fromkeys(key for key in keys if key))


def _skeletons(text: str) -> set[str]:
    """Костяки названия целиком и каждого значащего слова."""
    found = {skeleton(text)} | {skeleton(word) for word in _significant_words(text)}
    # Костяк из одной буквы совпадёт с чем угодно.
    return {item for item in found if len(item) >= 2}


def _touches(part: str, key: str) -> bool:
    """Совпадают ли слова краем — началом или концом."""
    if len(part) < 3 or len(key) < 3:
        return False
    short, long = sorted((part, key), key=len)
    return long.startswith(short) or long.endswith(short)


#: Сколько слов может быть в названии программы. Длинная фраза программой не
#: бывает: «открой видео, как я обманывал всех десять лет на сайте» находило
#: «4K Video Downloader+» — по слову «видео» — и вызывало запрос прав
#: администратора. Запускать что-то от администратора по такому основанию
#: нельзя, а отказ отправит фразу дальше, в браузер.
MAX_PROGRAM_WORDS = 5

#: До какой доли громкости убавлять чужой звук, пока Jarvis слушает команду.
#: Не в ноль намеренно: полная тишина посреди трека пугает сильнее, чем
#: приглушение, а для микрофона разница между 20% и нулём уже невелика.
DUCK_LEVEL = 0.2

#: Сколько подождать после ответа, прежде чем вернуть громкость. Колонки
#: договаривают последний слог, и в комнате остаётся реверберация.
RESTORE_DELAY_S = 0.5

#: За сколько убавлять и за сколько возвращать. Числа разные намеренно:
#: убавляем перед командой, и медленное затухание означало бы, что начало фразы
#: всё равно записано с музыкой, — то есть смысл приглушения теряется. А вот
#: возвращать резко незачем: по ушам бьёт именно мгновенный скачок вверх.
FADE_OUT_S = 0.25
FADE_IN_S = 1.2

#: Через сколько секунд вернуть громкость, если ответа так и не было.
#: Больше окна ответа (`audio.wake_word.follow_up_s`) плюс запас на
#: распознавание и саму команду.
DUCK_TIMEOUT_S = 20.0


def match_program(query: str, catalog: Mapping[str, str]) -> tuple[str, str] | None:
    """Найти программу в каталоге по услышанному названию.

    Сравнение идёт в три захода: точное совпадение, вхождение (чтобы «обс»
    находило «OBS Studio»), и только потом нечёткое. Порядок важен: при
    обратном коротний запрос цепляет случайного соседа по алфавиту.

    :param query: название, как его произнесли.
    :param catalog: известные программы, имя → чем запускать.
    :return: пара «найденное имя» и «чем запускать», либо ``None``.
    """
    if len(str(query).split()) > MAX_PROGRAM_WORDS:
        return None

    wanted = _keys(query, split=False)
    if not wanted:
        return None

    # Отдельно название целиком и отдельно его слова: по словам можно искать
    # точно и краем, но не нечётко — иначе «гитхап» находит «guitar» внутри
    # «Ample Guitar».
    prepared = [
        (name, target, _keys(name), _keys(name, split=False))
        for name, target in catalog.items()
    ]

    for name, target, keys, _ in prepared:
        if any(key in wanted for key in keys):
            return name, target

    # Совпадение краем слова: «обс» находит «OBS Studio», «торрент» —
    # «qBittorrent». Именно краем, а не любым куском: «telegramdesktop»
    # содержит «кто», и вопрос «кто такой трамп» открывал Telegram, а «блокнот»
    # содержит «окно». Побеждает самое короткое название, иначе «обс» уезжает
    # в «OBS Studio Portable Edition».
    contained = [
        (name, target)
        for name, target, keys, _ in prepared
        if any(_touches(part, key) for key in keys for part in wanted)
    ]
    if contained:
        return min(contained, key=lambda item: len(item[0]))

    # Согласный костяк: «фотошоп» и «photoshop» пишутся по-разному, а звучат
    # одинаково. Совпадение требуется точное — костяк и так огрубляет слово.
    skeletons = {skeleton(query)} - {""}
    by_skeleton = [
        (name, target)
        for name, target, _, _ in prepared
        if _skeletons(name) & skeletons
    ]
    if by_skeleton:
        return min(by_skeleton, key=lambda item: len(item[0]))

    # Нечёткое сравнение — последняя попытка. Транслитерация огрубляет слова
    # («стим» → «stim» против «steam»), поэтому порог невысокий, зато берётся
    # лучшее совпадение из всех, а не первое подошедшее.
    #
    # Сравнивается только название целиком. Отдельное слово внутри длинного
    # названия — слишком слабое основание: «открой гитхап» запускало «Ample
    # Guitar», потому что «githap» похоже на «guitar» на 0.73. Точное
    # совпадение и совпадение краем по словам работают выше и там уместны.
    best: tuple[float, str, str] | None = None
    for name, target, _, whole in prepared:
        for key in whole:
            for part in wanted:
                # Сравнивать имеет смысл слова сопоставимой длины: короткое
                # «окно» иначе находит «блокнот» с похожестью 0.73.
                if min(len(part), len(key)) / max(len(part), len(key)) < 0.7:
                    continue
                ratio = difflib.SequenceMatcher(None, part, key).ratio()
                if ratio >= _SIMILARITY and (best is None or ratio > best[0]):
                    best = (ratio, name, target)
    return (best[1], best[2]) if best else None


def scan_start_menu(directories: list[Path], *, limit: int = 400) -> dict[str, str]:
    """Собрать ярлыки меню «Пуск»: название программы → путь к ярлыку.

    Так список установленного получается сам и остаётся актуальным: поставил
    программу — она сразу доступна голосом, конфиг править не нужно.

    :param directories: каталоги меню «Пуск».
    :param limit: предохранитель от разросшегося меню.
    """
    found: dict[str, str] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            shortcuts = sorted(
                item for item in directory.rglob("*") if item.suffix.lower() in (".lnk", ".url")
            )
        except OSError:
            continue
        for shortcut in shortcuts:
            name = shortcut.stem
            if _SKIP_SHORTCUT.search(name):
                continue
            # Первый найденный побеждает: в общем меню ярлыки аккуратнее,
            # чем в пользовательском.
            found.setdefault(name, str(shortcut))
            if len(found) >= limit:
                return found
    return found


def start_menu_dirs() -> list[Path]:
    """Каталоги, где Windows держит ярлыки: меню «Пуск» и рабочий стол.

    Рабочий стол добавлен не для красоты: Steam и часть установщиков кладут
    ярлык только туда, и без этого программа остаётся невидимой.
    """
    parts = [
        (os.environ.get("ProgramData"), "Microsoft/Windows/Start Menu/Programs"),
        (os.environ.get("APPDATA"), "Microsoft/Windows/Start Menu/Programs"),
        (os.environ.get("PUBLIC"), "Desktop"),
        (os.environ.get("USERPROFILE"), "Desktop"),
        (os.environ.get("USERPROFILE"), "OneDrive/Desktop"),
    ]
    return [Path(root) / tail for root, tail in parts if root]


def scan_program_files(roots: list[Path], *, limit: int = 300) -> dict[str, str]:
    """Найти программы, не оставившие ярлыка: ``Program Files/Имя/Имя.exe``.

    Так находится то, что ставится без ярлыков или ставится через Steam.
    Смотрим только на один уровень вглубь и только на файлы, чьё имя похоже на
    имя папки, — иначе в каталог попадут все установщики и обновлялки подряд.
    """
    found: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            folders = sorted(item for item in root.iterdir() if item.is_dir())
        except OSError:
            continue
        for folder in folders:
            if _SKIP_SHORTCUT.search(folder.name):
                continue
            try:
                executables = [item for item in folder.glob("*.exe") if item.is_file()]
            except OSError:
                continue
            for executable in executables:
                if skeleton(executable.stem) == skeleton(folder.name):
                    found.setdefault(folder.name, str(executable))
                    break
            if len(found) >= limit:
                return found
    return found


def program_files_dirs() -> list[Path]:
    """Куда Windows и Steam ставят программы."""
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    directories = [Path(root) for root in roots if root]
    directories += steam_library_dirs()
    # Один и тот же путь может прийти из разных переменных окружения.
    return list(dict.fromkeys(directories))


def steam_library_dirs() -> list[Path]:
    """Папки, куда Steam ставит игры и приложения.

    Библиотек бывает несколько и на разных дисках; их список Steam держит в
    ``libraryfolders.vdf``. Формат простой, разбираем регулярным выражением —
    тащить ради этого зависимость незачем.
    """
    bases = [
        Path(root) / "Steam"
        for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"))
        if root
    ]
    libraries: list[Path] = []
    for base in bases:
        manifest = base / "steamapps" / "libraryfolders.vdf"
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            libraries.append(Path(match.group(1).replace("\\\\", "\\")) / "steamapps" / "common")
    return [path for path in libraries if path.is_dir()]


@dataclass(frozen=True, slots=True)
class Process:
    """Запущенная программа: чем является, под каким номером и как подписана."""

    image: str
    pid: int
    title: str = ""


def parse_tasklist(output: str) -> list[Process]:
    """Разобрать вывод ``tasklist /fo csv /nh /v``."""
    processes: list[Process] = []
    for row in csv.reader(io.StringIO(output)):
        if not row or not row[0].lower().endswith(".exe"):
            continue
        try:
            pid = int(row[1])
        except (IndexError, ValueError):
            continue
        title = row[-1].strip() if len(row) >= 9 else ""
        processes.append(
            Process(image=row[0], pid=pid, title="" if title == "N/A" else title)
        )
    return processes


def process_catalog(processes: list[Process]) -> dict[str, str]:
    """Как программу называют → имя её процесса.

    Имя процесса и название программы совпадают далеко не всегда: FL Studio
    работает как ``FL64.exe``, и «закрой фл студио» по именам процессов не
    находилось ничего. Поэтому в каталог идут и заголовки окон — там программа
    подписана так, как её называет человек.
    """
    catalog: dict[str, str] = {}
    for process in processes:
        catalog.setdefault(process.image.removesuffix(".exe"), process.image)
        if process.title:
            catalog.setdefault(process.title, process.image)
    return catalog


def helper_pids(processes: list[Process], image: str) -> set[int]:
    """Номера процессов-помощников — тех, чьё имя начинается с имени главного.

    Окно принадлежит не тому процессу, который запускали. У Steam с переездом
    интерфейса на Chromium окно рисует ``steamwebhelper.exe``, а у самого
    ``steam.exe`` видимых окон нет вовсе. Из-за этого «закрой стим» находил
    процесс, не находил у него ни одного окна и докладывал, что окно и так
    убрано, — при открытом на весь экран Steam.

    Отбор по имени, а не по заголовку окна: заголовок «Steam» бывает и у папки
    в проводнике, и у вкладки браузера, и закрывать их точно не нужно.
    """
    base = image.lower().removesuffix(".exe")
    if len(base) < 4:
        # Короткая основа цепляет посторонних: «fl» нашлось бы во «flux».
        return set()
    return {
        process.pid
        for process in processes
        if process.image.lower() != image.lower()
        and process.image.lower().removesuffix(".exe").startswith(base)
    }


#: Программы, которые живут в трее: «закрой» для них означает «убери окно».
#:
#: Steam — главный пример. Крестик у него сворачивает окно, а не выходит, и это
#: задумано: клиент должен оставаться в трее, иначе перестанут работать
#: загрузки и оверлей в играх. Поэтому убирать окно здесь — не половина дела,
#: а всё дело: процесс остаётся жить намеренно, и добивать его не нужно.
TRAY_APPS: frozenset[str] = frozenset({"steam.exe"})

#: Свои команды выхода — для программ, которые не реагируют ни на окно, ни на
#: taskkill. По умолчанию пусто: полный выход это не то, что обычно имеют в
#: виду, говоря «закрой». Если он всё же нужен, в конфиге пишется
#: ``quit_commands: {steam.exe: "steam://exit"}``.
QUIT_URIS: dict[str, str] = {}


def close_windows(pids: set[int], *, title: str | None = None) -> int:
    """Послать окнам процессов запрос на закрытие — то же, что Alt+F4.

    Именно сообщение окну, а не нажатие клавиш: клавиши ушли бы в то окно,
    которое сейчас в фокусе, а это может оказаться что угодно. Программа при
    этом успевает спросить про несохранённое — в отличие от ``taskkill /f``.

    :param title: закрывать только окно с таким заголовком. У браузера все
        окна принадлежат одному процессу, поэтому без этого «закрой YouTube»
        закрывало заодно и все остальные вкладки.
    :return: скольким окнам отправлен запрос.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    sent = 0

    def window_title(handle: int) -> str:
        """Заголовок окна — по нему отличаем одно окно процесса от другого."""
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        """Проверить одно окно и, если оно наше, попросить его закрыться."""
        nonlocal sent
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value not in pids or not user32.IsWindowVisible(handle):
            return True
        # Заголовок берётся из tasklist, а сверяется с живым окном: длинные
        # названия там могут оказаться обрезанными, поэтому годится и начало.
        if title is not None and not window_title(handle).startswith(title):
            return True
        user32.PostMessageW(handle, _WM_CLOSE, 0, 0)
        sent += 1
        return True

    user32.EnumWindows(visit, 0)
    return sent


#: Сообщение «закройся», которое Windows шлёт окну по Alt+F4.
_WM_CLOSE = 0x0010

#: ShowWindow: развернуть свёрнутое окно, не трогая уже развёрнутое.
_SW_RESTORE = 9


def enum_windows() -> list[tuple[int, str]]:
    """Видимые окна системы: номер процесса и заголовок.

    Нужно там, где `tasklist` бессилен: он показывает по одному заголовку на
    процесс, а у браузера все окна — один процесс. Чтобы понять, открыт ли
    где-то YouTube, окна надо перебирать поштучно.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        """Запомнить одно окно, если оно видимое и подписанное."""
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        found.append((owner.value, buffer.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def raise_window(title: str) -> bool:
    """Поднять окно с таким заголовком на передний план.

    Windows не даёт программе перехватывать фокус просто так — иначе окна
    дрались бы за него. Обходной приём стандартный: на время вызова свой поток
    ввода привязывается к потоку окна, которое сейчас впереди, и запрет
    снимается. Прав администратора это не требует и не заменяет.

    :return: удалось ли поднять окно.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    target: int | None = None

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        """Найти первое видимое окно с нужным заголовком."""
        nonlocal target
        if target is not None or not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value.startswith(title):
            target = handle
        return True

    user32.EnumWindows(visit, 0)
    if target is None:
        return False

    user32.ShowWindow(target, _SW_RESTORE)
    if user32.SetForegroundWindow(target):
        return True

    foreground = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(foreground, None)
    ours = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(ours, theirs, True)
    try:
        user32.BringWindowToTop(target)
        return bool(user32.SetForegroundWindow(target))
    finally:
        user32.AttachThreadInput(ours, theirs, False)


def endpoint_volume():  # type: ignore[no-untyped-def]  # тип живёт только в pycaw
    """Получить регулятор громкости системы через pycaw.

    Пакет за годы поменял API: раньше ``GetSpeakers()`` отдавал сырой
    COM-объект, у которого надо было запрашивать интерфейс через ``Activate``,
    теперь — обёртку ``AudioDevice`` со свойством ``EndpointVolume``. Старый
    вызов на новой версии падает с ``AttributeError``, поэтому поддерживаем оба.
    """
    from pycaw.utils import AudioUtilities

    speakers = AudioUtilities.GetSpeakers()
    volume = getattr(speakers, "EndpointVolume", None)
    if volume is not None:
        return volume

    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume

    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


# --- приглушение чужого звука ------------------------------------------------
#
# Микрофон у владельца встроенный, а музыка играет через колонку с сабом и
# погромче. Никакой шумодав столько не вытянет: алгоритмы борются за десяток
# децибел, а просто убавить громкость на время команды — это сразу двадцать,
# мгновенно и без нагрузки на процессор. Источник шума тут наш собственный, и
# грех этим не воспользоваться.


@dataclass(frozen=True, slots=True, kw_only=True)
class SoundSession:
    """Звуковая сессия приложения: кто звучит и с какой громкостью."""

    pid: int
    name: str
    volume: float


def plan_ducking(
    sessions: Sequence[SoundSession], *, own_pids: Container[int], level: float
) -> dict[int, float]:
    """Кого приглушить и какая у него сейчас громкость.

    Своя сессия не трогается принципиально: Jarvis отвечает голосом, и
    приглушённый ответ утонул бы вместе с музыкой. Уже тихие тоже пропускаем —
    «восстановление» сделало бы их громче, чем было.

    Ключ — номер процесса: у одного приложения бывает несколько сессий, и
    возвращать их по отдельности незачем — громкость у них общая по смыслу.
    """
    plan: dict[int, float] = {}
    for session in sessions:
        if session.pid <= 0 or session.pid in own_pids:
            continue
        if session.volume <= level:
            continue
        plan[session.pid] = max(plan.get(session.pid, 0.0), session.volume)
    return plan


#: Кто прислал реплику, которую действительно произнесли вслух. Диспетчер шлёт
#: своё событие сразу, как только инструмент вернул ответ, — для Telegram и
#: веб-панели это правильно, а для колонок рано: текст ещё не прозвучал.
VOICE_SOURCE = "voice"


def restores_volume(source: str, *, awaiting_command: bool) -> bool:
    """Возвращать ли громкость на этой реплике ассистента.

    Условия два, и каждое поймано на живом запуске:

    * **реплика должна быть произнесена, а не составлена.** На одну команду
      событие «ответил» приходит дважды: сперва от диспетчера (текст готов),
      потом от голосового конвейера (текст отзвучал). По первому музыка
      возвращалась в ту же секунду, в которую Jarvis только начинал говорить, —
      и ответа было не слышно;
    * **это не должен быть отклик на имя.** Сразу после «Джарвис» ассистент
      говорит «Слушаю, сэр», и это тоже произнесённая реплика. Команда ещё
      впереди, музыку возвращать рано.
    """
    return source == VOICE_SOURCE and not awaiting_command


#: Из скольких шагов складывается плавный переход. Сорок миллисекунд — предел,
#: за которым ступеньки перестают быть слышны, а мельче дробить незачем: каждый
#: шаг это вызов COM на каждое приложение.
FADE_STEP_S = 0.04


def fade_steps(start: float, end: float, seconds: float) -> list[float]:
    """Промежуточные громкости для плавного перехода.

    Шаги **равные в децибелах, а не в долях**, то есть громкость умножается на
    одно и то же число, а не увеличивается на одно и то же. Слух устроен именно
    так: путь от 0.2 к 0.4 воспринимается как такой же скачок, что от 0.4 к 0.8,
    хотя во втором случае прибавка вдвое больше. Ровная по долям кривая на слух
    рвётся в начале и еле ползёт в конце.

    Последним значением всегда стоит ровно ``end``: накопленная погрешность
    умножений не должна оставлять музыку на 0.98 навсегда.
    """
    if seconds <= 0 or start <= 0 or end <= 0 or start == end:
        return [end]
    count = max(1, round(seconds / FADE_STEP_S))
    ratio = (end / start) ** (1 / count)
    levels = [start * ratio ** step for step in range(1, count)]
    return [max(0.0, min(1.0, level)) for level in levels] + [end]


def sound_sessions() -> list[tuple[Any, SoundSession]]:
    """Звуковые сессии Windows: COM-объект и его описание.

    Возвращаются парами, потому что менять громкость всё равно придётся через
    COM-объект, а решение принимается по описанию — и его можно проверить
    тестами на любой машине.
    """
    from pycaw.utils import AudioUtilities

    found: list[tuple[Any, SoundSession]] = []
    for session in AudioUtilities.GetAllSessions():
        volume = getattr(session, "SimpleAudioVolume", None)
        if volume is None:
            # Системные звуки идут сессией без своего регулятора.
            continue
        process = getattr(session, "Process", None)
        pid = int(getattr(session, "ProcessId", 0) or 0)
        if not pid and process is not None:
            pid = int(getattr(process, "pid", 0) or 0)
        name = ""
        if process is not None:
            try:
                name = str(process.name())
            except Exception:  # noqa: BLE001 — процесс мог умереть между вызовами
                name = ""
        try:
            level = float(volume.GetMasterVolume())
        except Exception:  # noqa: BLE001 — COM бросает что угодно
            continue
        found.append((session, SoundSession(pid=pid, name=name, volume=level)))
    return found


class WindowsSkill(Skill):
    """Запуск программ, блокировка компьютера и громкость."""

    meta = SkillMeta(
        name="windows",
        description="Управление компьютером студии",
        version="0.2.0",
        platforms=("windows",),
    )

    async def on_setup(self) -> None:
        """Собрать каталог программ: встроенные, меню «Пуск», конфиг."""
        self._configured: dict[str, str] = {
            str(key): str(value)
            for key, value in dict(self.context.setting("programs", {})).items()
        }
        self._force_close = bool(self.context.setting("force_close", False))
        # Программы из трея: «закрой» для них означает «убери окно».
        self._tray_apps = TRAY_APPS | {
            str(name).lower() for name in self.context.setting("tray_apps", [])
        }
        # Свои команды выхода поверх встроенных: ключ — имя процесса.
        self._quit_commands = {
            **QUIT_URIS,
            **{
                str(key).lower(): str(value)
                for key, value in dict(self.context.setting("quit_commands", {})).items()
            },
        }
        self._catalog: dict[str, str] = {}
        self._rebuild()
        self._setup_ducking()

    def _setup_ducking(self) -> None:
        """Подписаться на голосовые события, чтобы приглушать чужой звук.

        Политика простая: позвали по имени — музыку убавить, ответили на
        команду — вернуть. А вот «ответили» оказалось хитрее, чем выглядит, —
        см. `restores_volume`.
        """
        ducking = dict(self.context.setting("ducking", {}))
        self._duck_level = float(ducking.get("level", DUCK_LEVEL))
        self._duck_timeout = float(ducking.get("restore_after_s", DUCK_TIMEOUT_S))
        self._restore_delay = float(ducking.get("restore_delay_s", RESTORE_DELAY_S))
        self._fade_out = float(ducking.get("fade_out_s", FADE_OUT_S))
        self._fade_in = float(ducking.get("fade_in_s", FADE_IN_S))
        #: Номер текущего перехода громкости. Начатый переход отменяет
        #: предыдущий: позвали второй раз посреди возврата — возврат бросаем и
        #: уводим вниз, иначе две плавные кривые тянули бы ползунок в разные
        #: стороны, ступенька через ступеньку.
        self._move = 0
        #: Что приглушили: номер процесса -> прежняя громкость.
        self._ducked: dict[int, float] = {}
        #: Ждём команду после имени — значит «Слушаю» громкость не возвращает.
        self._awaiting_command = False
        self._duck_timer: asyncio.Task[None] | None = None

        if not bool(ducking.get("enabled", True)):
            self.log.debug("Приглушение звука выключено в конфиге")
            return
        self.context.scope.subscribe(WakeWordDetected.NAME, self._on_wake_word)
        self.context.scope.subscribe(VoiceCommandRecognized.NAME, self._on_command)
        self.context.scope.subscribe(AssistantReplied.NAME, self._on_replied)

    async def on_stop(self) -> None:
        """Вернуть громкость: приглушённая навсегда музыка — худший исход."""
        await self._restore()

    # --- приглушение -------------------------------------------------------

    async def _on_wake_word(self, event: Event) -> None:
        """Позвали по имени — убавить всё чужое и ждать команду."""
        self._awaiting_command = True
        await self._duck()

    async def _on_command(self, event: Event) -> None:
        """Команда распознана: следующая реплика вернёт громкость."""
        self._awaiting_command = False

    async def _on_replied(self, event: Event) -> None:
        """Ответ прозвучал — вернуть громкость, если это был ответ на команду."""
        if not restores_volume(event.source, awaiting_command=self._awaiting_command):
            return
        if self._restore_delay > 0:
            # Колонки ещё договаривают последний слог, плюс реверберация
            # комнаты. Вернуть громкость ровно на нём — значит смазать конец
            # фразы: та же причина, по которой микрофон глохнет с запасом.
            await asyncio.sleep(self._restore_delay)
        await self._restore()

    async def _duck(self) -> None:
        """Убавить громкость всем, кроме себя."""
        try:
            sessions = sound_sessions()
        except Exception as exc:  # noqa: BLE001 — нет pycaw или COM не в духе
            self.log.debug("Приглушить звук не удалось: %s: %s", type(exc).__name__, exc)
            return

        plan = plan_ducking(
            [described for _, described in sessions],
            own_pids={os.getpid()},
            level=self._duck_level,
        )
        if not plan and not self._ducked:
            return

        # Позвали второй раз, не дождавшись ответа: прежние громкости
        # перезаписывать нельзя, иначе вернём приглушённые. А вот увести вниз
        # ещё раз — можно и нужно: возврат мог уже начаться.
        if not self._ducked:
            self._ducked = plan

        await self._slide(
            [
                (session, described)
                for session, described in sessions
                if described.pid in self._ducked
            ],
            target=lambda _: self._duck_level,
            seconds=self._fade_out,
        )
        self.log.info(
            "Приглушил на время команды: %s",
            ", ".join(
                sorted({s.name or str(s.pid) for _, s in sessions if s.pid in self._ducked})
            ),
        )
        self._arm_restore_timer()

    async def _slide(
        self,
        targets: Sequence[tuple[Any, SoundSession]],
        *,
        target: Any,
        seconds: float,
    ) -> bool:
        """Плавно перевести громкость сессий к нужным значениям.

        Список сессий собирается **один раз**: перебирать их на каждом шаге
        значило бы три десятка обходов COM за секунду. Пропавшая по дороге
        сессия просто выпадает — приложение закрыли, и возвращать ей нечего.

        :return: ``False``, если переход не доведён до конца — его перебил
            следующий.
        """
        if not targets:
            return True
        self._move += 1
        mine = self._move
        # У каждой сессии своя дорожка: играли они с разной громкостью, и
        # вернуться должны туда же, откуда ушли.
        tracks = [
            (session, described, fade_steps(described.volume, float(target(described)), seconds))
            for session, described in targets
        ]
        length = max(len(steps) for _, _, steps in tracks)

        for index in range(length):
            if self._move != mine:
                # Начался следующий переход — этот больше не нужен.
                return False
            for session, described, steps in tracks:
                if index >= len(steps):
                    continue
                try:
                    session.SimpleAudioVolume.SetMasterVolume(steps[index], None)
                except Exception as exc:  # noqa: BLE001 — сессия могла закрыться
                    self.log.debug("Сессия %s не отозвалась: %s", described.name, exc)
            if index + 1 < length:
                await asyncio.sleep(FADE_STEP_S)
        return True

    def _arm_restore_timer(self) -> None:
        """Страховка: вернуть громкость, даже если ответа так и не будет.

        Сценарий обычный — позвали по имени и передумали. Без таймера музыка
        осталась бы тихой до следующей команды.
        """
        if self._duck_timer is not None:
            self._duck_timer.cancel()
        self._duck_timer = self.context.scope.spawn(
            self._restore_later(), name="windows-unduck"
        )

    async def _restore_later(self) -> None:
        """Подождать и вернуть громкость."""
        await asyncio.sleep(self._duck_timeout)
        # Ссылку снимаем до восстановления: иначе `_restore` отменит задачу,
        # внутри которой сам же и выполняется.
        self._duck_timer = None
        self.log.debug("Ответа не дождался — возвращаю громкость")
        await self._restore()

    async def _restore(self) -> None:
        """Вернуть громкость тем, кого приглушали."""
        if self._duck_timer is not None:
            self._duck_timer.cancel()
            self._duck_timer = None
        self._awaiting_command = False
        saved = dict(self._ducked)
        if not saved:
            return

        try:
            sessions = sound_sessions()
        except Exception as exc:  # noqa: BLE001 — вернуть громкость важнее причины
            self.log.warning("Не удалось вернуть громкость: %s: %s", type(exc).__name__, exc)
            self._ducked = {}
            return

        # Сохранённые громкости живут до конца перехода: позвали посреди
        # возврата — приглушим снова, и вернуть надо будет туда же, откуда
        # уходили в самый первый раз, а не в середину кривой.
        if await self._slide(
            [(session, described) for session, described in sessions if described.pid in saved],
            target=lambda described: saved[described.pid],
            seconds=self._fade_in,
        ):
            self._ducked = {}
            self.log.debug("Громкость вернул: %d приложений", len(saved))

    @tool(routable=False)
    async def duck_others(self, level: float = DUCK_LEVEL) -> ToolResult:
        """Приглушить звук всех приложений, кроме самого ассистента.

        :param level: до какой доли громкости убавить, от 0 до 1.
        """
        self._duck_level = max(0.0, min(1.0, level))
        await self._duck()
        return ToolResult.success(len(self._ducked))

    @tool(routable=False)
    async def restore_others(self) -> ToolResult:
        """Вернуть громкость приложениям, которые приглушали."""
        count = len(self._ducked)
        await self._restore()
        return ToolResult.success(count)

    def _rebuild(self) -> None:
        """Пересобрать каталог известных программ.

        Порядок важен: названное владельцем в конфиге перекрывает найденное
        автоматически.
        """
        catalog: dict[str, str] = dict(BUILT_IN)
        # Ярлыки точнее найденного перебором папок, поэтому идут позже.
        catalog.update(scan_program_files(program_files_dirs()))
        catalog.update(scan_start_menu(start_menu_dirs()))
        catalog.update(self._configured)
        self._catalog = catalog
        self.log.info(
            "Программ в каталоге: %d (своих в конфиге: %d)",
            len(catalog),
            len(self._configured),
        )

    @tool(phrases=["открой {program}", "запусти {program}",
                   "open {program}", "launch {program}", "start {program}"])
    async def launch_program(self, program: str) -> ToolResult:
        """Запустить программу по названию.

        :param program: название, как его произносят: «стим», «обс», «браузер».
        """
        found = match_program(program, self._catalog)
        if found is None:
            # Программы с таким названием нет — возможно, это сайт. «Открой
            # гитхаб» и «открой почту» разумнее открыть в браузере, чем
            # ответить отказом. Порядок именно такой: установленная программа
            # важнее сайта, у Steam и Telegram есть и то, и другое.
            if self.tools.has("browser.open_site"):
                site = await self.tools.invoke("browser.open_site", {"site": program})
                if site.ok:
                    return site

            # Услышанное в оболочку не уходит: незнакомое имя — это отказ.
            suggestions = difflib.get_close_matches(program, self._catalog, n=3, cutoff=0.4)
            hint = f" Может быть: {', '.join(suggestions)}?" if suggestions else ""
            self.log.warning("Программа %r в каталоге не найдена", program)
            return ToolResult.failure(
                f"программа {program!r} не найдена среди {len(self._catalog)} известных",
                speech={
                    "ru": f"Не знаю программу {program}.{hint}",
                    "en": f"I don't know a program called {program}.{hint}",
                },
            )

        name, target = found
        try:
            # Ни shell=True, ни строки-команды: только конкретный путь или URI,
            # который мы сами нашли в каталоге.
            os.startfile(target)  # type: ignore[attr-defined]  # есть только на Windows
        except OSError as exc:
            self.log.error("Не удалось запустить %s (%s): %s", name, target, exc)
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                speech={
                    "ru": f"Не получилось запустить {name}.",
                    "en": f"Couldn't launch {name}.",
                },
            )

        self.log.info("Запущено: %s (%s)", name, target)
        return ToolResult.success(
            {"program": name, "target": target},
            speech={
                "ru": (f"Запускаю {name}.", f"{name} запускается.", f"Открываю {name}.",
                       f"Секунду, {name}."),
                "en": (f"Launching {name}.", f"Starting {name}.", f"{name}, coming up."),
            },
        )

    @tool(phrases=["заблокируй компьютер", "заблокируй пк", "заблокируй экран",
                   "заблокируй ноутбук", "заблокируй комп", "заблокируй",
                   "lock the computer", "lock the pc", "lock screen"])
    async def lock(self) -> ToolResult:
        """Заблокировать компьютер."""
        import ctypes

        # Штатная функция Windows: сеанс не завершается, программы продолжают
        # работать, несохранённое не теряется. Прав администратора не требует.
        if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
            return ToolResult.failure(
                f"LockWorkStation вернула ошибку {ctypes.get_last_error()}",
                speech={
                    "ru": "Не получилось заблокировать компьютер.",
                    "en": "Couldn't lock the computer.",
                },
            )
        self.log.info("Компьютер заблокирован")
        return ToolResult.success(
            True,
            speech={
                "ru": ("Блокирую.", "Запираю компьютер.", "Готово, заблокировал."),
                "en": ("Locking.", "Locking up.", "Screen locked."),
            },
        )

    @tool(phrases=["закрой {program}", "заверши {program}",
                   "close {program}", "quit {program}"])
    async def close_program(self, program: str) -> ToolResult:
        """Закрыть программу: убрать её окно.

        Способы пробуются по очереди, от вежливого к решительному: сначала
        собственная команда выхода, если она задана в конфиге, потом запрос
        окнам (то же, что Alt+F4), и только если программа осталась жива —
        ``taskkill``. Порядок важен: убитая программа не сохраняет настройки.

        Программам из трея (`TRAY_APPS`) закрытием окна всё и заканчивается:
        Steam обязан остаться в трее, иначе перестают работать загрузки и
        оверлей. Чтобы завершить процесс совсем, есть `kill_program`.

        :param program: название программы.
        """
        return await self._shutdown(program, force=False)

    @tool(phrases=["убей {program}", "заверши процесс {program}",
                   "выгрузи {program}", "kill {program}", "force close {program}"])
    async def kill_program(self, program: str) -> ToolResult:
        """Завершить процесс программы принудительно.

        В отличие от `close_program`, окно не спрашивают: процесс снимается
        сразу и вместе со всеми копиями. Несохранённое при этом теряется —
        поэтому команда отдельная, а не флаг у закрытия.

        :param program: название программы.
        """
        return await self._shutdown(program, force=True)

    # Голосом это не зовут — инструмент нужен другим скиллам, поэтому в каталог
    # для модели он не попадает: каждая запись там стоит токенов на каждой фразе.
    @tool(name="list_windows", routable=False)
    async def list_windows(self) -> ToolResult:
        """Перечислить открытые окна: заголовок, программа, номер процесса."""
        windows = await asyncio.to_thread(enum_windows)
        images = {process.pid: process.image for process in await self._processes()}
        return ToolResult.success(
            [
                {"title": title, "image": images.get(pid, ""), "pid": pid}
                for pid, title in windows
            ]
        )

    @tool(name="focus_window", routable=False)
    async def focus_window(self, title: str) -> ToolResult:
        """Поднять окно с указанным заголовком на передний план.

        :param title: заголовок окна, как его вернул list_windows.
        """
        if not await asyncio.to_thread(raise_window, title):
            return ToolResult.failure(f"окно {title!r} не удалось поднять")
        self.log.info("Окно на переднем плане: %r", title)
        return ToolResult.success({"window": title})

    async def _shutdown(self, program: str, *, force: bool) -> ToolResult:
        """Общая часть закрытия и убийства: найти процесс и доложить итог."""
        processes = await self._processes()
        found = match_program(program, process_catalog(processes))
        if found is None:
            return ToolResult.failure(
                f"процесс для {program!r} не найден среди запущенных",
                speech={
                    "ru": f"Не вижу запущенной программы {program}.",
                    "en": f"I don't see {program} running.",
                },
            )

        name, image = found
        # Совпало с заголовком окна, а не с именем программы — значит, просили
        # закрыть именно это окно. Для браузера разница принципиальная: все его
        # окна принадлежат одному процессу, и «закрой YouTube» закрывало заодно
        # всё остальное, а потом ещё и добивало браузер целиком.
        window = name if any(process.title == name for process in processes) else None

        if not _PROCESS_NAME.match(image):
            # Сюда попасть не должно: имена приходят из вывода tasklist. Но
            # аргумент внешней команды проверяется, а не подразумевается.
            return ToolResult.failure(
                f"недопустимое имя процесса {image!r}",
                speech={"ru": "Странное имя процесса, не закрываю.",
                        "en": "Suspicious process name, not closing."},
            )

        pids = {process.pid for process in processes if process.image == image}

        if window is not None and not force:
            return await self._close_window(program, window, pids)

        how = await self._close(
            image, pids, force=force, helpers=helper_pids(processes, image)
        )
        if how is None:
            return ToolResult.failure(
                f"{image} не закрылся",
                speech={"ru": f"{image} не закрывается.", "en": f"{image} won't close."},
            )

        name = image.removesuffix(".exe")
        self.log.info("%s: %s", "Убито" if force else "Закрыто", f"{image} ({how})")
        if how == "трей":
            speech = {"ru": f"{name} и так свёрнут.", "en": f"{name} is already hidden."}
        elif force:
            speech = {"ru": f"Завершаю {name}.", "en": f"Killing {name}."}
        elif image.lower() in self._tray_apps:
            speech = {"ru": f"Сворачиваю {name}.", "en": f"Minimising {name}."}
        else:
            speech = {"ru": f"Закрываю {name}.", "en": f"Closing {name}."}

        return ToolResult.success({"process": image, "method": how}, speech=speech)

    async def _close_window(self, spoken: str, title: str, pids: set[int]) -> ToolResult:
        """Закрыть одно окно по его заголовку.

        Ждать исчезновения процесса тут нечего, а добивать его тем более
        нельзя: у браузера одно окно из десяти — это вкладка, а не программа.
        Не нашлось окна — так и говорим, вместо того чтобы закрыть что-то ещё.
        """
        sent = await asyncio.to_thread(close_windows, pids, title=title)
        if not sent:
            self.log.warning("Окно %r не найдено среди видимых", title)
            return ToolResult.failure(
                f"окно {title!r} не найдено",
                speech={
                    "ru": f"Не нашёл окно {spoken}.",
                    "en": f"I couldn't find the {spoken} window.",
                },
            )

        name = spoken.strip() or title
        self.log.info("Закрыто окно %r (%d шт.)", title, sent)
        return ToolResult.success(
            {"window": title, "closed": sent},
            speech={
                "ru": (f"Закрываю {name}.", f"{name} закрыл.", f"Убрал {name}."),
                "en": (f"Closing {name}.", f"{name} closed.", f"Shut {name} down."),
            },
        )

    async def _close(
        self,
        image: str,
        pids: set[int],
        *,
        force: bool,
        helpers: set[int] = frozenset(),  # type: ignore[assignment]  # только читаем
    ) -> str | None:
        """Закрыть процесс, перебирая способы. Возвращает сработавший.

        :param helpers: процессы-помощники той же программы. К ним обращаемся,
            только если у главного процесса окон не нашлось.
        """
        if force:
            return await self._taskkill(image, force=True)

        quit_uri = self._quit_commands.get(image.lower())
        if quit_uri:
            # У программы есть свой выход — он всегда чище внешнего закрытия.
            self.log.info("Закрываю %s через %s", image, quit_uri)
            try:
                os.startfile(quit_uri)  # type: ignore[attr-defined]  # только Windows
            except OSError as exc:
                self.log.warning("Команда выхода %s не сработала: %s", quit_uri, exc)
            else:
                if await self._wait_gone(image):
                    return quit_uri

        # Запрос окнам: то же, что Alt+F4, но адресно — клавиши ушли бы в окно,
        # которое сейчас в фокусе, а это может оказаться что угодно.
        sent = await asyncio.to_thread(close_windows, pids)

        if not sent and helpers:
            # Своих окон нет — значит, интерфейс держит вспомогательный
            # процесс. Расширяем поиск только сейчас: пока окно нашлось у
            # главного, лезть к соседям незачем.
            self.log.info(
                "У %s нет видимых окон, пробую помощников: %s",
                image,
                ", ".join(str(pid) for pid in sorted(helpers)),
            )
            sent = await asyncio.to_thread(close_windows, helpers)

        if image.lower() in self._tray_apps:
            # Для программы из трея убранное окно — это и есть результат.
            # Ждать её исчезновения бессмысленно: она обязана остаться жить.
            self.log.info("Убрано окон %d у %s, процесс остаётся в трее", sent, image)
            return "окно" if sent else "трей"

        if sent:
            self.log.info("Отправлено закрытие %d окнам %s", sent, image)
            if await self._wait_gone(image):
                return "окно"

        return await self._taskkill(image, force=self._force_close)

    async def _taskkill(self, image: str, *, force: bool) -> str | None:
        """Снять процесс средствами Windows."""
        command = ["taskkill.exe", "/im", image]
        if force:
            command.append("/f")
        result = await self._run(command)
        if result.returncode != 0:
            self.log.warning(
                "taskkill для %s вернул %s: %s", image, result.returncode, result.stderr.strip()
            )
            return None
        return "taskkill"

    async def _wait_gone(self, image: str, *, timeout: float = 4.0) -> bool:
        """Подождать, пока процесс исчезнет.

        Программы закрываются не мгновенно: Steam сохраняет состояние, редакторы
        спрашивают про несохранённое. Без ожидания следующий способ применился бы
        к программе, которая уже закрывается сама.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            alive = {process.image for process in await self._processes()}
            if image not in alive:
                return True
        return False

    @tool(phrases=["какие программы открыты", "что запущено",
                   "what is running", "list programs"])
    async def list_programs(self) -> ToolResult:
        """Перечислить запущенные программы."""
        running = await self._processes()
        if not running:
            return ToolResult.failure(
                "не удалось получить список процессов",
                speech={"ru": "Не смог прочитать список процессов.",
                        "en": "Couldn't read the process list."},
            )

        # Вслух перечислять полсотни процессов бессмысленно.
        processes = sorted({process.image for process in running})
        visible = [name.removesuffix(".exe") for name in processes[:5]]
        return ToolResult.success(
            processes,
            speech={
                "ru": f"Запущено {len(processes)} программ, среди них {', '.join(visible)}.",
                "en": f"{len(processes)} programs running, among them {', '.join(visible)}.",
            },
        )

    @tool(phrases=["поставь громкость {level}", "сделай громкость {level}",
                   "громкость {level}", "звук {level}",
                   "set volume to {level}", "volume {level}"])
    async def set_volume(self, level: int) -> ToolResult:
        """Установить громкость системы.

        :param level: громкость в процентах, от 0 до 100.
        """
        level = max(0, min(100, level))
        try:
            endpoint_volume().SetMasterVolumeLevelScalar(level / 100, None)
        except Exception as exc:  # noqa: BLE001 — COM бросает что угодно
            return self._volume_failure(exc)

        return ToolResult.success(
            level,
            speech={"ru": f"Громкость {level} процентов.",
                    "en": f"Volume {level} percent."},
        )

    @tool(phrases=["погромче", "сделай громче", "louder", "turn it up"],
          routable=False)
    async def louder(self) -> ToolResult:
        """Сделать громче на десять процентов."""
        return await self.change_volume(10)

    @tool(phrases=["потише", "сделай тише", "quieter", "turn it down"],
          routable=False)
    async def quieter(self) -> ToolResult:
        """Сделать тише на десять процентов."""
        return await self.change_volume(-10)

    @tool()
    async def change_volume(self, delta: int = 10) -> ToolResult:
        """Изменить громкость на несколько процентов.

        :param delta: на сколько процентов, отрицательное значение — тише.
        """
        try:
            volume = endpoint_volume()
            current = round(volume.GetMasterVolumeLevelScalar() * 100)
            level = max(0, min(100, current + delta))
            volume.SetMasterVolumeLevelScalar(level / 100, None)
        except Exception as exc:  # noqa: BLE001 — COM бросает что угодно
            return self._volume_failure(exc)

        return ToolResult.success(
            level,
            speech={"ru": f"Громкость {level} процентов.",
                    "en": f"Volume {level} percent."},
        )

    @tool(phrases=["выключи звук", "включи звук", "mute", "unmute"])
    async def mute(self, on: bool = True) -> ToolResult:
        """Выключить или включить звук.

        :param on: ``true`` — выключить звук, ``false`` — вернуть.
        """
        try:
            endpoint_volume().SetMute(bool(on), None)
        except Exception as exc:  # noqa: BLE001 — COM бросает что угодно
            return self._volume_failure(exc)

        return ToolResult.success(
            bool(on),
            speech={
                "ru": "Звук выключен." if on else "Звук включён.",
                "en": "Muted." if on else "Unmuted.",
            },
        )

    def _volume_failure(self, exc: Exception) -> ToolResult:
        """Одинаковый ответ на любую беду с громкостью."""
        self.log.error("Громкость: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, ImportError):
            return ToolResult.failure(
                "нет пакета pycaw",
                speech={
                    "ru": "Управление громкостью не установлено.",
                    "en": "Volume control isn't installed.",
                },
            )
        return ToolResult.failure(
            f"{type(exc).__name__}: {exc}",
            speech={"ru": "Не получилось изменить громкость.",
                    "en": "Couldn't change the volume."},
        )

    @tool(phrases=["обнови список программ", "refresh programs"])
    async def refresh(self) -> ToolResult:
        """Перечитать меню «Пуск» после установки новой программы."""
        self._rebuild()
        return ToolResult.success(
            len(self._catalog),
            speech={
                "ru": f"Знаю {len(self._catalog)} программ.",
                "en": f"I know {len(self._catalog)} programs.",
            },
        )

    async def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        """Выполнить внешнюю команду, не блокируя event loop.

        Команда всегда список, а не строка: оболочка не вызывается вовсе,
        поэтому услышанный текст не может стать её частью.
        """
        return await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            encoding="cp866",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def _processes(self) -> list[Process]:
        """Запущенные процессы: имя, номер, заголовок окна."""
        # Режим /v добавляет заголовки окон: по ним программа узнаётся там,
        # где имя процесса ничего не говорит (FL Studio живёт как FL64.exe).
        result = await self._run(["tasklist.exe", "/fo", "csv", "/nh", "/v"])
        if result.returncode != 0:
            self.log.warning("tasklist вернул %s: %s", result.returncode, result.stderr)
            return []
        return parse_tasklist(result.stdout)

    async def health(self) -> HealthStatus:
        """Скилл исправен, пока в каталоге есть хоть что-то."""
        if not self._catalog:
            return HealthStatus.degraded("каталог программ пуст")
        return HealthStatus.healthy(f"{len(self._catalog)} программ")
