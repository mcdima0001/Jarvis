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

import csv
import difflib
import io
import os
import re
import subprocess
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
        for shortcut in sorted(directory.rglob("*.lnk")):
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
    """Каталоги меню «Пуск» — общий и пользовательский."""
    parts = [
        (os.environ.get("ProgramData"), "Microsoft/Windows/Start Menu/Programs"),
        (os.environ.get("APPDATA"), "Microsoft/Windows/Start Menu/Programs"),
    ]
    return [Path(root) / tail for root, tail in parts if root]


def parse_tasklist(output: str) -> set[str]:
    """Достать имена процессов из вывода ``tasklist /fo csv /nh``."""
    return {
        row[0]
        for row in csv.reader(io.StringIO(output))
        if row and row[0].lower().endswith(".exe")
    }


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
        self._catalog: dict[str, str] = {}
        self._rebuild()

    def _rebuild(self) -> None:
        """Пересобрать каталог известных программ.

        Порядок важен: названное владельцем в конфиге перекрывает найденное
        автоматически.
        """
        catalog: dict[str, str] = dict(BUILT_IN)
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
        """Закрыть программу по названию.

        :param program: название программы.
        """
        running = await self._running()
        found = match_program(program, {name: name for name in running})
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

        command = ["taskkill.exe", "/im", image]
        if self._force_close:
            command.append("/f")
        result = await self._run(command)
        if result.returncode != 0:
            return ToolResult.failure(
                result.stderr.strip() or f"taskkill вернул {result.returncode}",
                speech={"ru": f"Не получилось закрыть {image}.",
                        "en": f"Couldn't close {image}."},
            )

        self.log.info("Закрыто: %s", image)
        return ToolResult.success(
            {"process": image},
            speech={"ru": f"Закрываю {image}.", "en": f"Closing {image}."},
        )

    @tool(phrases=["какие программы открыты", "что запущено",
                   "what is running", "list programs"])
    async def list_programs(self) -> ToolResult:
        """Перечислить запущенные программы."""
        running = await self._running()
        if not running:
            return ToolResult.failure(
                "не удалось получить список процессов",
                speech={"ru": "Не смог прочитать список процессов.",
                        "en": "Couldn't read the process list."},
            )

        # Вслух перечислять полсотни процессов бессмысленно.
        visible = [name.removesuffix(".exe") for name in sorted(running)[:5]]
        return ToolResult.success(
            sorted(running),
            speech={
                "ru": f"Запущено {len(running)} программ, среди них {', '.join(visible)}.",
                "en": f"{len(running)} programs running, among them {', '.join(visible)}.",
            },
        )

    @tool()
    async def set_volume(self, level: int) -> ToolResult:
        """Установить громкость системы.

        :param level: громкость в процентах, от 0 до 100.
        """
        level = max(0, min(100, level))
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError:
            return ToolResult.failure(
                "нет пакета pycaw",
                speech={
                    "ru": "Управление громкостью не установлено.",
                    "en": "Volume control isn't installed.",
                },
            )

        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level / 100, None)

        return ToolResult.success(
            level,
            speech={"ru": f"Громкость {level} процентов.",
                    "en": f"Volume {level} percent."},
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
        import asyncio

        return await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            encoding="cp866",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def _running(self) -> set[str]:
        """Имена запущенных процессов."""
        result = await self._run(["tasklist.exe", "/fo", "csv", "/nh"])
        if result.returncode != 0:
            self.log.warning("tasklist вернул %s: %s", result.returncode, result.stderr)
            return set()
        return parse_tasklist(result.stdout)

    async def health(self) -> HealthStatus:
        """Скилл исправен, пока в каталоге есть хоть что-то."""
        if not self._catalog:
            return HealthStatus.degraded("каталог программ пуст")
        return HealthStatus.healthy(f"{len(self._catalog)} программ")
