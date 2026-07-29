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
from typing import Mapping

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
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

#: Кириллица в латиницу — «обс» должно находить «OBS».
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

#: Насколько похожими должны быть названия, чтобы счесть их одним и тем же.
#: Порог низкий: транслитерация огрубляет слова, «влс» против «vlc» даёт всего
#: 0.67. Запас до ближайшего известного ложного срабатывания («трамп» против
#: «telegram», 0.62) невелик — понижать дальше нельзя.
_SIMILARITY = 0.66


def _romanize(text: str) -> str:
    """Записать кириллицу латиницей для сравнения названий."""
    return "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in text)


#: Английское написание против русского произношения: photoshop — «фотошоп»,
#: notion — «ноушен», firefox — «файрфокс». Правила выравнивают обе записи.
_PHONETIC: tuple[tuple[str, str], ...] = (
    ("tion", "shn"), ("ph", "f"), ("ck", "k"), ("x", "ks"),
    ("w", "v"), ("j", "dz"), ("qu", "kv"), ("c", "k"),
    # После c→k: «Chrome» становится «khrome», а по-русски это «хром».
    ("kh", "h"),
)

_VOWELS = "aeiouy"


def _skeleton(text: str) -> str:
    """Согласный костяк слова — то, что переживает произношение вслух.

    Гласные при переводе на слух плывут сильнее всего («зум» против «zoom»),
    а согласные остаются. Сравнение костяков идёт только на точное совпадение:
    приём грубый, и нечёткость поверх него давала бы ложные попадания.
    """
    lowered = _romanize(text.strip().lower())
    for source, target in _PHONETIC:
        lowered = lowered.replace(source, target)
    letters = [char for char in lowered if char.isalnum() and char not in _VOWELS]
    # Двойные согласные на слух неразличимы: «Discord» и «дискорд».
    collapsed = [
        char for index, char in enumerate(letters) if index == 0 or char != letters[index - 1]
    ]
    return "".join(collapsed)


def _normalize(text: str) -> str:
    """Привести название к виду, в котором его можно сравнивать.

    Пробелы и пунктуация в названиях расставлены как попало: «OBS Studio»,
    «obs-studio», «ОБС».
    """
    return re.sub(r"[^a-zа-яё0-9]+", "", text.strip().lower())


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
    variants = [lowered, _romanize(lowered)]
    if split:
        for word in _significant_words(text):
            variants += [word, _romanize(word)]

    keys = (_normalize(variant) for variant in variants)
    return tuple(dict.fromkeys(key for key in keys if key))


def _skeletons(text: str) -> set[str]:
    """Костяки названия целиком и каждого значащего слова."""
    found = {_skeleton(text)} | {_skeleton(word) for word in _significant_words(text)}
    # Костяк из одной буквы совпадёт с чем угодно.
    return {item for item in found if len(item) >= 2}


def _touches(part: str, key: str) -> bool:
    """Совпадают ли слова краем — началом или концом."""
    if len(part) < 3 or len(key) < 3:
        return False
    short, long = sorted((part, key), key=len)
    return long.startswith(short) or long.endswith(short)


def match_program(query: str, catalog: Mapping[str, str]) -> tuple[str, str] | None:
    """Найти программу в каталоге по услышанному названию.

    Сравнение идёт в три захода: точное совпадение, вхождение (чтобы «обс»
    находило «OBS Studio»), и только потом нечёткое. Порядок важен: при
    обратном коротний запрос цепляет случайного соседа по алфавиту.

    :param query: название, как его произнесли.
    :param catalog: известные программы, имя → чем запускать.
    :return: пара «найденное имя» и «чем запускать», либо ``None``.
    """
    wanted = _keys(query, split=False)
    if not wanted:
        return None

    prepared = [(name, target, _keys(name)) for name, target in catalog.items()]

    for name, target, keys in prepared:
        if any(key in wanted for key in keys):
            return name, target

    # Совпадение краем слова: «обс» находит «OBS Studio», «торрент» —
    # «qBittorrent». Именно краем, а не любым куском: «telegramdesktop»
    # содержит «кто», и вопрос «кто такой трамп» открывал Telegram, а «блокнот»
    # содержит «окно». Побеждает самое короткое название, иначе «обс» уезжает
    # в «OBS Studio Portable Edition».
    contained = [
        (name, target)
        for name, target, keys in prepared
        if any(_touches(part, key) for key in keys for part in wanted)
    ]
    if contained:
        return min(contained, key=lambda item: len(item[0]))

    # Согласный костяк: «фотошоп» и «photoshop» пишутся по-разному, а звучат
    # одинаково. Совпадение требуется точное — костяк и так огрубляет слово.
    skeletons = {_skeleton(query)} - {""}
    by_skeleton = [
        (name, target)
        for name, target, _ in prepared
        if _skeletons(name) & skeletons
    ]
    if by_skeleton:
        return min(by_skeleton, key=lambda item: len(item[0]))

    # Нечёткое сравнение — последняя попытка. Транслитерация огрубляет слова
    # («стим» → «stim» против «steam»), поэтому порог невысокий, зато берётся
    # лучшее совпадение из всех, а не первое подошедшее.
    best: tuple[float, str, str] | None = None
    for name, target, keys in prepared:
        for key in keys:
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
                if _skeleton(executable.stem) == _skeleton(folder.name):
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


def close_windows(pids: set[int]) -> int:
    """Послать окнам процессов запрос на закрытие — то же, что Alt+F4.

    Именно сообщение окну, а не нажатие клавиш: клавиши ушли бы в то окно,
    которое сейчас в фокусе, а это может оказаться что угодно. Программа при
    этом успевает спросить про несохранённое — в отличие от ``taskkill /f``.

    :return: скольким окнам отправлен запрос.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    sent = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        """Проверить одно окно и, если оно наше, попросить его закрыться."""
        nonlocal sent
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(handle):
            user32.PostMessageW(handle, _WM_CLOSE, 0, 0)
            sent += 1
        return True

    user32.EnumWindows(visit, 0)
    return sent


#: Сообщение «закройся», которое Windows шлёт окну по Alt+F4.
_WM_CLOSE = 0x0010


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
            speech={"ru": f"Запускаю {name}.", "en": f"Launching {name}."},
        )

    @tool(phrases=["заблокируй компьютер", "заблокируй пк", "заблокируй экран",
                   "заблокируй", "lock the computer", "lock the pc", "lock screen"])
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
        return ToolResult.success(True, speech={"ru": "Блокирую.", "en": "Locking."})

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

        image = found[1]
        if not _PROCESS_NAME.match(image):
            # Сюда попасть не должно: имена приходят из вывода tasklist. Но
            # аргумент внешней команды проверяется, а не подразумевается.
            return ToolResult.failure(
                f"недопустимое имя процесса {image!r}",
                speech={"ru": "Странное имя процесса, не закрываю.",
                        "en": "Suspicious process name, not closing."},
            )

        pids = {process.pid for process in processes if process.image == image}
        how = await self._close(image, pids, force=force)
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

    async def _close(self, image: str, pids: set[int], *, force: bool) -> str | None:
        """Закрыть процесс, перебирая способы. Возвращает сработавший."""
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
