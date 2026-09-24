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
import ctypes
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
    AssistantSpeaking,
    Choice,
    Event,
    Intent,
    ToolResult,
    VoiceCommandRecognized,
    WakeDismissed,
    WakeWordDetected,
    numbered,
)
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match, closeness, rank, romanize, skeleton, squash, touches
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

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

#: Насколько сопоставимы должны быть длины при нечётком сравнении: короткое
#: «окно» иначе находит «блокнот» с похожестью 0.73.
_BALANCE = 0.7

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


#: Сколько слов может быть в названии программы. Длинная фраза программой не
#: бывает: «открой видео, как я обманывал всех десять лет на сайте» находило
#: «4K Video Downloader+» — по слову «видео» — и вызывало запрос прав
#: администратора. Запускать что-то от администратора по такому основанию
#: нельзя, а отказ отправит фразу дальше, в браузер.
MAX_PROGRAM_WORDS = 5

#: На сколько децибел убавлять чужой звук — на тихой и на громкой системе.
#:
#: Одной цифрой тут не обойтись, и это не придирка. На системной громкости 20%
#: музыка микрофону почти не мешает, и глубокий рез превратил бы её в тишину
#: посреди трека; на 100% микрофон захлёбывается, и мягкого реза не хватает
#: вовсе. Поэтому глубина едет по прямой между двумя точками.
QUIET_CUT_DB = 10.0
LOUD_CUT_DB = 35.0

#: Сколько ждать блютуз-устройство после переключения служб, прежде чем сказать
#: «не отозвалось». Замер 24.09.2026 (`tools/bluetooth_bench.py`): у живой
#: колонки вызов сам блокируется на 6.1–6.5 с и возвращается, когда она уже
#: подключена; в живом сбое он вернулся за 0.34 с, а колонка подключилась через
#: три-четыре секунды. Шесть секунд накрывают второй случай с запасом, а
#: выключенное устройство столько и стоит: неверное «не отозвалось» хуже, чем
#: медленное верное, а молчать эти секунды ассистенту не даёт «секунду».
BT_SETTLE_S = 6.0
BT_ASK_EVERY_S = 0.4

#: Между какими значениями системной громкости натянута прямая. Ниже тихой
#: точки режем мягко, выше громкой — на полную.
QUIET_AT = 0.2
LOUD_AT = 1.0

#: Мельче этого резать незачем: разницу не слышно, а сессию мы уже потрогали.
MIN_CUT_DB = 2.0

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


#: По чему видно, что в конфиге написан путь, а не имя другой программы.
#: Разделитель каталогов, расширение файла или схема URI — всё это встречается
#: в пути и не встречается в том, как программу называют вслух.
_PATH_MARKS = ("\\", "/", ":")


def looks_like_path(value: str) -> bool:
    """Путь это или название программы."""
    text = value.strip()
    return any(mark in text for mark in _PATH_MARKS) or text.lower().endswith(
        (".exe", ".lnk", ".bat", ".cmd", ".url")
    )


def resolve_alias(value: str, catalog: Mapping[str, str]) -> str | None:
    """Найти, куда ведёт псевдоним «называю так, а запускать вот это».

    Нужно там, где привычное имя и установленная программа разошлись: у
    владельца нет Telegram, стоит форк AyuGram, и «открой телеграм» не находило
    ничего. Написать путь в конфиг можно было и раньше, но путь придётся чинить
    после каждой переустановки, а имя программы переживёт её.

    :return: чем запускать, либо ``None``, если это не псевдоним.
    """
    if looks_like_path(value):
        return None
    found = match_program(value, catalog)
    return found[1] if found else None


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
        if any(touches(part, key) for key in keys for part in wanted)
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
                ratio = closeness(part, key, balance=_BALANCE)
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


#: Сколько уровней вглубь искать папку и сколько всего их набирать. Предел не
#: от жадности: каталог собирается на каждую просьбу, а рекурсия по диску целиком
#: занимает минуты — больше предела ожидания инструмента.
FOLDER_DEPTH = 2
FOLDER_LIMIT = 4000

#: Как называют вслух стандартные папки. Ключ — то, что говорят, значение — имя
#: переменной окружения или подпапка профиля. Нужны отдельно: «загрузки» на
#: диске зовутся `Downloads`, и никакое сравнение строк одно из другого не
#: выведет — это перевод, ровно как «браузер» и `browser`.
HOME_FOLDERS: dict[str, str] = {
    "загрузки": "Downloads",
    "скачанное": "Downloads",
    "рабочий стол": "Desktop",
    "стол": "Desktop",
    "документы": "Documents",
    "изображения": "Pictures",
    "картинки": "Pictures",
    "фотографии": "Pictures",
    "музыка": "Music",
    "видео": "Videos",
}


def folder_roots() -> list[Path]:
    """Где искать папку по названию.

    Профиль пользователя и корни дисков: там лежит всё, что человек называет
    «папкой такой-то». Системные каталоги не трогаем — в них голосом не ходят.
    """
    roots: list[Path] = []
    home = os.environ.get("USERPROFILE")
    if home:
        roots.append(Path(home))
    for letter in "DEFG":
        drive = Path(f"{letter}:/")
        if drive.is_dir():
            roots.append(drive)
    return roots


def folder_catalog(
    roots: list[Path], *, depth: int = FOLDER_DEPTH, limit: int = FOLDER_LIMIT
) -> dict[str, str]:
    """Собрать папки: название → путь. Ближние к корню побеждают.

    Обход по уровням, а не вглубь: папка, названная вслух, почти всегда лежит
    неглубоко, а полный обход диска не уложится в предел ожидания.
    """
    found: dict[str, str] = {}
    level = [root for root in roots if root.is_dir()]
    for _ in range(max(1, depth)):
        following: list[Path] = []
        for directory in level:
            try:
                children = sorted(item for item in directory.iterdir() if item.is_dir())
            except OSError:
                continue
            for child in children:
                if child.name.startswith((".", "$")):
                    continue
                found.setdefault(child.name, str(child))
                following.append(child)
                if len(found) >= limit:
                    return found
        level = following
    return found


def match_folder(query: str, catalog: Mapping[str, str], home: Path | None = None) -> str | None:
    """Найти папку, которую назвали вслух. ``None`` — не узнали.

    Сначала стандартные папки профиля: «загрузки» — это `Downloads`, и такое
    сравнением строк не выводится. Потом каталог с диска, обычной лестницей
    сопоставления — она берёт на себя падежи, транслитерацию и опечатки.
    """
    asked = " ".join(query.split()).lower().strip(" .,")
    for prefix in ("папку ", "папка ", "каталог ", "folder ", "директорию "):
        if asked.startswith(prefix):
            asked = asked[len(prefix) :].strip()
    if not asked:
        return None
    if home is not None:
        tail = HOME_FOLDERS.get(asked)
        if tail and (home / tail).is_dir():
            return str(home / tail)
    found = best_match(asked, list(catalog), similarity=_SIMILARITY, prefer=len)
    return catalog.get(found) if found else None


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
    """Разобрать вывод ``tasklist /fo csv /nh``.

    Заголовок читается, если он в строке есть: с ключом ``/v`` столбцов девять,
    без него пять. Сам ключ больше не используется (см. `with_window_titles`),
    но разбор обеих форм оставлен — вывод чужой команды, и терять на нём данные
    из-за лишнего столбца незачем.
    """
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


def with_window_titles(
    processes: list[Process], windows: list[tuple[int, str]]
) -> list[Process]:
    """Дописать процессам заголовки их окон.

    Заголовки раньше приносил сам ``tasklist`` по ключу ``/v``, и на живой
    машине этот ключ стоил **сорока секунд** против полусекунды без него: 40.6 с
    и 0.5 с на одном и том же наборе из 354 процессов. Голосовая команда столько
    не живёт — она умирала по общему пределу ожидания в 30 секунд, и вместе с ней
    «закрой программу», «убей программу» и перечисление окон. В живом логе это
    выглядело как «убей браузер → не удалась», без единой строки о причине.

    Ровно те же заголовки лежат в `enum_windows` и берутся за миллисекунды: это
    обход окон, а не пересчёт всех процессов системы. У процесса окон бывает
    несколько, берётся первое — столько же, сколько давал ``/v``.
    """
    titles: dict[int, str] = {}
    for pid, title in windows:
        titles.setdefault(pid, title)
    return [
        Process(image=process.image, pid=process.pid, title=titles.get(process.pid, ""))
        for process in processes
    ]


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

#: Сколько раз и как часто повторять просьбу об оверлее: игра поднимается
#: полминуты, и RTSS, цепляясь к ней, возвращает себе прежнее состояние.
OVERLAY_TRIES = 12
OVERLAY_EVERY_S = 5.0

#: Как часто заглядывать, не появилось ли окно запущенной программы.
FOCUS_STEP_S = 0.3
#: Сколько всего его ждать. Лаунчеры (Prism, Steam) рисуют окно не сразу, а
#: висеть дольше незачем: через десять секунд человек уже сам щёлкнул мышкой.
FOCUS_WAIT_S = 10.0


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


#: Сколько ждать между двумя снимками счётчиков, секунд. Меряется **разница**:
#: у Windows на процесс копится время с его запуска, и без второго снимка вышел
#: бы рейтинг долгожителей, а не рейтинг тех, кто греет прямо сейчас.
CPU_SAMPLE_S = 1.0

#: Формы слова «процент» под число. Вслух это звучит, а «4 процентов» режет ухо.
PERCENT = ("процент", "процента", "процентов")


def process_cpu() -> dict[int, tuple[str, float]]:
    """Сколько процессорного времени накопил каждый процесс, по номерам.

    Через ctypes, а не `tasklist`: тот про процессорное время не говорит вовсе,
    а запуск чужой программы ради каждого снимка стоил бы дороже самого замера.

    Процессы, к которым нет доступа (системные, чужого пользователя), молча
    пропускаются: спрашивать о них права ради строки в отчёте незачем.
    """
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Без объявленных типов обработчик процесса на 64 битах обрезается до
    # четырёх байт — та же грабля, что в скилле клавиатуры.
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    count = 4096
    pids = (wintypes.DWORD * count)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return {}
    total = needed.value // ctypes.sizeof(wintypes.DWORD)

    #: Право «спросить, но не трогать» — минимальное из подходящих.
    query_limited = 0x1000
    found: dict[int, tuple[str, float]] = {}
    for index in range(total):
        pid = pids[index]
        if not pid:
            continue
        handle = kernel32.OpenProcess(query_limited, False, pid)
        if not handle:
            continue
        try:
            creation, exited = wintypes.FILETIME(), wintypes.FILETIME()
            kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                ctypes.c_void_p(handle),
                ctypes.byref(creation),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                continue
            # FILETIME — два слова по 32 бита, счёт в сотнях наносекунд.
            spent = sum(
                ((part.dwHighDateTime << 32) | part.dwLowDateTime) / 1e7
                for part in (kernel, user)
            )
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size)
            ):
                name = buffer.value.rsplit("\\", 1)[-1]
            else:
                name = f"pid {pid}"
            found[pid] = (name, spent)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return found


def cpu_hogs(
    before: Mapping[int, tuple[str, float]],
    after: Mapping[int, tuple[str, float]],
    seconds: float,
    *,
    cores: int = 1,
) -> list[tuple[str, float]]:
    """Кто ел процессор между двумя снимками, долей всего процессора.

    Одноимённые процессы складываются: у браузера их полтора десятка, и по
    отдельности каждый выглядит скромно, а вместе — как раз то, что грело.

    :return: пары «имя, доля всего процессора», от жадного к скромному.
    """
    if seconds <= 0:
        return []
    grown: dict[str, float] = {}
    for pid, (name, spent) in after.items():
        was = before.get(pid)
        # Процесса не было в прошлом снимке — он родился только что, и всё его
        # время считать нашим отрезком нельзя: получился бы выброс на ровном месте.
        if was is None:
            continue
        delta = spent - was[1]
        if delta > 0:
            grown[name] = grown.get(name, 0.0) + delta
    share = {name: value / seconds / max(1, cores) for name, value in grown.items()}
    return sorted(share.items(), key=lambda item: -item[1])


def describe_hogs(hogs: Sequence[tuple[str, float]], *, limit: int = 3) -> str:
    """Назвать вслух тех, кто греет. Пустой список — так и сказать.

    Имя файла программы вслух не годится (`msedgewebview2.exe` синтез читает по
    буквам), поэтому расширение снимается, а проценты округляются до целых:
    десятые доли на слух не значат ничего.
    """
    named = [
        (name.removesuffix(".exe").removesuffix(".EXE"), round(share * 100))
        for name, share in hogs
        if share >= 0.01
    ][:limit]
    if not named:
        return ""
    return ", ".join(
        f"{name} {percent} {plural_form(percent, PERCENT)}" for name, percent in named
    )


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

    return bring_to_front(target)


def bring_to_front(handle: int) -> bool:
    """Поднять окно с этим номером на передний план.

    Windows не даёт программе перехватывать фокус просто так — иначе окна
    дрались бы за него. Обходной приём стандартный: на время вызова свой поток
    ввода привязывается к потоку того окна, что сейчас впереди, и запрет
    снимается. Прав администратора это не требует и не заменяет.
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.ShowWindow(handle, _SW_RESTORE)
    if user32.SetForegroundWindow(handle):
        return True

    foreground = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(foreground, None)
    ours = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(ours, theirs, True)
    try:
        user32.BringWindowToTop(handle)
        return bool(user32.SetForegroundWindow(handle))
    finally:
        user32.AttachThreadInput(ours, theirs, False)


def window_handles() -> dict[int, str]:
    """Видимые подписанные окна: номер окна -> заголовок.

    Нужно, чтобы поймать **новое** окно: у только что запущенной программы
    заголовок заранее неизвестен, а вот то, что его не было секунду назад, —
    признак надёжный.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        found[int(handle)] = buffer.value
        return True

    user32.EnumWindows(visit, 0)
    return found


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


#: Модуль про паузу видео — рядом со скиллом. Загружается по пути и один раз на
#: жизнь скилла: звать его приходится на каждой реплике, а «переподключи модуль
#: windows» перечитывает и его вместе со скиллом.
_MEDIA: Any = None
#: То же для модуля про питание и память.
_POWER: Any = None
#: И для оверлея RivaTuner.
_OSD: Any = None


def osd() -> Any:
    """Соседний модуль `osd.py`: оверлей RivaTuner."""
    global _OSD
    if _OSD is None:
        _OSD = _sibling("osd.py", "jarvis_skills.windows_osd")
    return _OSD


def power() -> Any:
    """Соседний модуль `power.py`: питание, память, клавиши."""
    global _POWER
    if _POWER is None:
        _POWER = _sibling("power.py", "jarvis_skills.windows_power")
    return _POWER


def _sibling(filename: str, name: str) -> Any:
    """Загрузить модуль рядом со скиллом: скилл грузится по файлу, без пакета."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def media() -> Any:
    """Соседний модуль `media.py`: скилл грузится по файлу, без пакета."""
    global _MEDIA
    if _MEDIA is None:
        _MEDIA = _sibling("media.py", "jarvis_skills.windows_media")
    return _MEDIA


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
    #: Идёт ли звук прямо сейчас. Открытый, но поставленный на паузу плеер
    #: Windows показывает неактивной сессией — по этому и отличаем «играет» от
    #: «просто запущен». Нужно паузе: остановленное нами потом продолжится, а
    #: остановленное владельцем — нет.
    playing: bool = True


def plan_ducking(
    sessions: Sequence[SoundSession], *, own_pids: Container[int], cut_db: float
) -> dict[int, float]:
    """Кого приглушить и какая у него сейчас громкость.

    Своя сессия не трогается принципиально: Jarvis отвечает голосом, и
    приглушённый ответ утонул бы вместе с музыкой. Совсем тихие пропускаем: у
    них после реза не останется ничего, а «восстановление» потом сделало бы их
    громче, чем было.

    Ключ — номер процесса: у одного приложения бывает несколько сессий, и
    возвращать их по отдельности незачем — громкость у них общая по смыслу.
    """
    if cut_db < MIN_CUT_DB:
        return {}
    plan: dict[int, float] = {}
    for session in sessions:
        if session.pid <= 0 or session.pid in own_pids:
            continue
        if session.volume <= 0.01:
            continue
        plan[session.pid] = max(plan.get(session.pid, 0.0), session.volume)
    return plan


def system_volume() -> float:
    """Громкость системы, 0..1. Выключенный звук — это ноль."""
    volume = endpoint_volume()
    if volume.GetMute():
        return 0.0
    return float(volume.GetMasterVolumeLevelScalar())


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


def cut_for(system: float, *, quiet_db: float = QUIET_CUT_DB, loud_db: float = LOUD_CUT_DB) -> float:
    """На сколько децибел убавлять при такой громкости системы.

    Прямая между двумя точками: тише тихой — режем как на тихой, громче
    громкой — как на громкой. Никакой физики, чистая настройка на слух: цифры
    правятся в конфиге, а смысл виден без чтения кода.
    """
    span = max(1e-6, LOUD_AT - QUIET_AT)
    share = (max(0.0, min(1.0, system)) - QUIET_AT) / span
    share = max(0.0, min(1.0, share))
    return quiet_db + (loud_db - quiet_db) * share


def quieter_by(volume: float, cut_db: float) -> float:
    """Громкость после реза на столько-то децибел.

    Децибелы — потому что глушим **относительно того, что было**: у одного
    приложения свой ползунок на 30%, у другого на 100%, и одинаковая доля
    оставила бы первое неслышным, а второе громким.
    """
    if cut_db <= 0:
        return volume
    return max(0.0, min(1.0, volume * 10 ** (-cut_db / 20)))


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
        # AudioSessionState: 1 — звук идёт, 0 — сессия есть, но молчит.
        playing = int(getattr(session, "State", 1) or 0) == 1
        found.append(
            (session, SoundSession(pid=pid, name=name, volume=level, playing=playing))
        )
    return found


def process_is_admin() -> bool:
    """Запущен ли сам ассистент с правами администратора."""
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def start_plain(target: str) -> None:
    """Запустить с **обычными** правами, даже если сам ассистент — администратор.

    Запущенное через `os.startfile` наследует права ассистента, а он работает
    от администратора: любая программа, открытая голосом, получала полные права
    (просьба владельца 19.09.2026 — по умолчанию без них). Проводник же всегда
    работает с обычными правами: новый `explorer.exe` передаёт путь уже
    запущенной оболочке и завершается, а программу запускает она.
    """
    if not process_is_admin():
        os.startfile(target)  # type: ignore[attr-defined]  # есть только на Windows
        return
    subprocess.Popen(["explorer.exe", target], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def start_elevated(target: str) -> None:
    """Запустить с правами администратора. Ассистент сам с правами — окна UAC не будет."""
    if process_is_admin():
        os.startfile(target)  # type: ignore[attr-defined]  # есть только на Windows
        return
    shell32 = ctypes.WinDLL("shell32")
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    code = shell32.ShellExecuteW(None, "runas", target, None, None, 1)
    if (code or 0) <= 32:
        raise OSError(f"ShellExecute отказал ({code}): возможно, отказались в окне UAC")


class WindowsSkill(Skill):
    """Запуск программ, блокировка компьютера и громкость."""

    meta = SkillMeta(
        name="windows",
        description="Управление компьютером студии",
        version="0.9.1",
        platforms=("windows",),
        spoken=("система", "виндовс", "компьютер", "windows"),
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
        #: Выводить ли запущенную программу вперёд (просьба владельца
        #: 23.09.2026). Windows сама этого не делает: окно, открытое не тем
        #: процессом, который сейчас в фокусе, честно встаёт позади.
        self._focus_launched = bool(self.context.setting("focus_launched", True))
        self._focus_wait = float(self.context.setting("focus_wait_s", FOCUS_WAIT_S))
        #: Какая схема питания была до того, как мы её сменили.
        self._plan_before = ""
        self._rebuild()
        self._setup_ducking()

    def _setup_ducking(self) -> None:
        """Подписаться на голосовые события, чтобы приглушать чужой звук.

        Политика простая: позвали по имени — музыку убавить, ответили на
        команду — вернуть. А вот «ответили» оказалось хитрее, чем выглядит, —
        см. `restores_volume`.
        """
        ducking = dict(self.context.setting("ducking", {}))
        cut = dict(ducking.get("cut_db", {}))
        self._quiet_cut = float(cut.get("quiet", QUIET_CUT_DB))
        self._loud_cut = float(cut.get("loud", LOUD_CUT_DB))
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
        #: Сколько раз ассистент начинал говорить. По нему видно, что за паузу
        #: перед возвратом громкости зазвучала новая реплика.
        self._speech_count = 0
        #: Видео на время реплики останавливается, а не приглушается (просьба
        #: владельца 23.09.2026): из музыки приглушение не крадёт ничего, а из
        #: фильма крадёт кусок, и его потом отматывают руками.
        self._pause_video = bool(ducking.get("pause_video", True))
        self._players = tuple(
            str(name).lower() for name in ducking.get("video_players", ()) if str(name).strip()
        ) or media().VIDEO_PLAYERS
        #: Кого остановили: вернуть к игре надо ровно их и никого больше.
        self._paused: tuple[int, ...] = ()
        #: VLC слушает только свой веб-интерфейс (замер 23.09.2026) — если
        #: владелец его включил, останавливаем VLC через него.
        vlc = dict(ducking.get("vlc_http", {}))
        self._vlc = media().Vlc(
            url=str(vlc.get("url", "http://127.0.0.1:8080")),
            password=str(vlc.get("password", "") or ""),
        )
        #: Остановлен ли VLC по HTTP — его возвращать тем же путём.
        self._vlc_paused = False
        #: Пауза и возврат — по очереди, и это не педантизм. Реплика бывает
        #: короткой, а пауза идёт через чужие службы: без очереди «верни» успеет
        #: раньше «останови», и видео замрёт навсегда (поймано живой проверкой
        #: 23.09.2026: после «ответил» плеер оказался на паузе, а не наоборот).
        self._video_turn = asyncio.Lock()

        if not bool(ducking.get("enabled", True)):
            self.log.debug("Приглушение звука выключено в конфиге")
            return
        if self._pause_video and self._vlc.ready:
            # Первый запрос в процессе стоит пять секунд (замер 23.09.2026):
            # поднимается httpx и открывается соединение. Платить это время
            # посреди первой же реплики нельзя — платим заранее и молча.
            self.context.scope.spawn(self._warm_vlc(), name="windows-vlc-warmup")
        self.context.scope.subscribe(AssistantSpeaking.NAME, self._on_speaking)
        self.context.scope.subscribe(WakeWordDetected.NAME, self._on_wake_word)
        self.context.scope.subscribe(VoiceCommandRecognized.NAME, self._on_command)
        self.context.scope.subscribe(AssistantReplied.NAME, self._on_replied)
        self.context.scope.subscribe(WakeDismissed.NAME, self._on_dismissed)

    async def on_stop(self) -> None:
        """Вернуть громкость: приглушённая навсегда музыка — худший исход.

        Без плавности намеренно. При выходе ассистент прощается, то есть музыка
        приглушена, и плавный подъём легко не успеет: задачи скилла отменяются
        вместе со scope, а брошенный на середине переезд оставил бы музыку тихой
        до следующего запуска. Слушать эту плавность в момент выхода всё равно
        некому.
        """
        await self._restore(fade=False)

    # --- приглушение -------------------------------------------------------

    async def _on_speaking(self, event: Event) -> None:
        """Ассистент начинает говорить — убавить всё чужое, чтобы его было слышно.

        Приглушение по имени этого не покрывает: здоровается и прощается он сам,
        никто его об этом не просил, и ровно эти реплики тонули в музыке.
        """
        self._speech_count += 1
        # Пауза первой: приглушение потом наверстает, а кусок фильма — нет.
        await self._hold_video()
        await self._duck()

    async def _on_wake_word(self, event: Event) -> None:
        """Позвали по имени — убавить всё чужое и ждать команду."""
        self._awaiting_command = True
        await self._duck()

    async def _on_command(self, event: Event) -> None:
        """Команда распознана: следующая реплика вернёт громкость."""
        self._awaiting_command = False
        # И заодно продлеваем страховку: работа идёт, бросать её посреди
        # выполнения незачем.
        self._arm_restore_timer()

    async def _on_dismissed(self, event: Event) -> None:
        """Фраза оказалась не к ассистенту — вернуть громкость сразу.

        Раньше возвращал только страховочный таймер: услышав «имя» в песне,
        ассистент молча убавлял музыку на двадцать секунд (15.09.2026, 09:12).
        """
        if not self._ducked:
            return
        self.log.debug("Не ко мне — возвращаю громкость")
        await self._restore()

    async def _on_replied(self, event: Event) -> None:
        """Ответ прозвучал — вернуть громкость, если это был ответ на команду."""
        if not restores_volume(event.source, awaiting_command=self._awaiting_command):
            # Реплика от диспетчера означает «сейчас буду говорить»: до конца
            # речи страховка сработать не должна, а реплика бывает длинной.
            if self._ducked:
                self._arm_restore_timer()
            return
        spoke = self._speech_count
        if self._restore_delay > 0:
            # Колонки ещё договаривают последний слог, плюс реверберация
            # комнаты. Вернуть громкость ровно на нём — значит смазать конец
            # фразы: та же причина, по которой микрофон глохнет с запасом.
            await asyncio.sleep(self._restore_delay)
        if self._speech_count != spoke:
            # За паузу зазвучала новая реплика — ответ после заполнителя или
            # напоминание. Её конец и вернёт громкость.
            self.log.debug("Снова говорю — громкость пока не возвращаю")
            return
        await self._restore()

    async def _warm_vlc(self) -> None:
        """Разогреть соединение с VLC, ничего им не управляя."""
        state = await asyncio.to_thread(self._vlc.state)
        self.log.debug("VLC по HTTP: %s", state or "не отвечает")

    async def _hold_video(self) -> None:
        """Остановить видео на время реплики.

        Приглушение при этом не отменяется: если плеер команду не понял (а
        понимают её не все), останется хотя бы тихий звук — ровно то, что было
        до 23.09.2026. Ставить паузу на одном лишь имени ассистента незачем:
        владелец говорит поверх фильма сам, а перебивает его ответ.
        """
        if not self._pause_video:
            return
        async with self._video_turn:
            if self._paused or self._vlc_paused:
                return
            await self._hold_vlc()
            await self._hold_players()

    async def _hold_vlc(self) -> None:
        """VLC — отдельно, и не из вредности: его не видно в звуковых сессиях.

        Живая проверка 23.09.2026: VLC играет видео, а в списке сессий Windows
        его нет вовсе (звук идёт мимо — своим выводом). Значит, искать его
        там, где ищутся остальные плееры, бессмысленно. Зато он сам говорит,
        играет ли: его же веб-интерфейс отдаёт состояние — по нему и решаем.
        """
        if not self._vlc.ready:
            return
        if await asyncio.to_thread(self._vlc.state) != "playing":
            return
        self._vlc_paused = await asyncio.to_thread(self._vlc.pause)
        if self._vlc_paused:
            self.log.info("Ставлю VLC на паузу на время реплики")

    async def _hold_players(self) -> None:
        """Остальные плееры: находим по звуку и останавливаем сообщением окну."""
        try:
            sessions = [described for _, described in sound_sessions()]
        except Exception as exc:  # noqa: BLE001 — нет pycaw или COM не в духе
            self.log.debug("Не посмотрел, играет ли видео: %s", exc)
            return
        found = media().plan_pausing(sessions, own_pids={os.getpid()}, players=self._players)
        if not found:
            return
        sent = await asyncio.to_thread(media().pause, set(found))
        if not sent:
            # Окон нет — команде некуда прийти. Молча забыть нельзя: иначе на
            # «возврате» мы решим, что снимаем с паузы то, что сами не ставили.
            self.log.debug("Видео нашёл, а окна нет — паузу не ставлю")
            return
        self._paused = found
        names = ", ".join(sorted({s.name for s in sessions if s.pid in found}))
        self.log.info("Ставлю видео на паузу на время реплики: %s", names)

    async def _release_video(self) -> None:
        """Вернуть видео к игре. Возвращаем ровно то, что сами остановили.

        Ждём свою очередь: если пауза ещё ставится, снимать нечего, а вот
        прийти раньше неё — значит оставить видео замершим до следующей реплики.
        """
        async with self._video_turn:
            if not self._paused and not self._vlc_paused:
                return
            paused, self._paused = self._paused, ()
            if self._vlc_paused:
                self._vlc_paused = False
                await asyncio.to_thread(self._vlc.play)
            sent = await asyncio.to_thread(media().play, set(paused))
            self.log.debug("Видео снял с паузы: %d окон", sent)

    async def _duck(self) -> None:
        """Убавить громкость всем, кроме себя."""
        try:
            sessions = sound_sessions()
        except Exception as exc:  # noqa: BLE001 — нет pycaw или COM не в духе
            self.log.debug("Приглушить звук не удалось: %s: %s", type(exc).__name__, exc)
            return

        # Глубина считается по громкости системы: на тихой музыка микрофону
        # почти не мешает, на громкой он захлёбывается.
        try:
            system = system_volume()
        except Exception as exc:  # noqa: BLE001 — не прочли, считаем громкой
            self.log.debug("Громкость системы не прочиталась: %s", exc)
            system = 1.0
        cut_db = cut_for(system, quiet_db=self._quiet_cut, loud_db=self._loud_cut)

        plan = plan_ducking(
            [described for _, described in sessions],
            own_pids={os.getpid()},
            cut_db=cut_db,
        )
        if not plan and not self._ducked:
            return

        # Позвали второй раз, не дождавшись ответа: прежние громкости
        # перезаписывать нельзя, иначе вернём приглушённые. А вот увести вниз
        # ещё раз — можно и нужно: возврат мог уже начаться.
        already = bool(self._ducked)
        if not self._ducked:
            self._ducked = plan

        await self._slide(
            [
                (session, described)
                for session, described in sessions
                if described.pid in self._ducked
            ],
            # Считаем от **сохранённой** громкости, а не от текущей. Иначе
            # второе приглушение подряд режет уже приглушённое: позвали по
            # имени, потом ассистент заговорил — и музыка ушла бы вдвое глубже,
            # а вернулась бы всё равно к исходной.
            target=lambda described: quieter_by(self._ducked[described.pid], cut_db),
            seconds=self._fade_out,
        )
        # На одну команду приглушение зовут дважды: сперва когда позвали по
        # имени, потом когда ассистент заговорил. Второй раз он ничего не
        # меняет — цель считается от той же сохранённой громкости, — и в логе
        # это выглядело как задвоенная строка (жалоба владельца 01.08.2026).
        # Новость тут только первая: музыка стала тише. Повтор — просто «держу».
        names = ", ".join(
            sorted({s.name or str(s.pid) for _, s in sessions if s.pid in self._ducked})
        )
        if already:
            self.log.debug("Держу приглушённым: %s", names)
        else:
            self.log.info(
                "Приглушил на %.0f дБ (громкость системы %.0f%%): %s",
                cut_db,
                system * 100,
                names,
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

        **Отсчёт начинается заново на каждый признак жизни**, и это не мелочь.
        Сначала таймер заводился один раз от приглушения, и на живом запуске
        (01.08.2026) он выстрелил ровно в ту секунду, когда началась длинная
        реплика: шесть секунд окна ответа, три на распознавание, пара на
        выполнение — двадцать секунд набираются законным путём. Со стороны это
        выглядело как «Джарвис не успевает договорить, а музыка уже орёт».
        Теперь таймер означает то, чем и был задуман: **ничего не происходит
        столько-то секунд**.
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

    async def _restore(self, *, fade: bool = True) -> None:
        """Вернуть громкость тем, кого приглушали.

        :param fade: вести плавно. ``False`` — поставить сразу: при остановке
            приложения плавность некому слушать, а недоведённый переезд оставил
            бы музыку тихой.
        """
        if self._duck_timer is not None:
            self._duck_timer.cancel()
            self._duck_timer = None
        self._awaiting_command = False
        # Пауза снимается первой и безусловно: остановленное навсегда видео —
        # худший исход из всех возможных, хуже навсегда приглушённой музыки.
        await self._release_video()
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
            seconds=self._fade_in if fade else 0.0,
        ):
            self._ducked = {}
            self.log.debug("Громкость вернул: %d приложений", len(saved))

    @tool(routable=False, reversible=True)
    async def duck_others(self, cut_db: float = 0.0) -> ToolResult:
        """Приглушить звук всех приложений, кроме самого ассистента.

        :param cut_db: на сколько децибел убавить; 0 — на своё усмотрение,
            по громкости системы.
        """
        if cut_db > 0:
            self._quiet_cut = self._loud_cut = float(cut_db)
        await self._duck()
        return ToolResult.success(len(self._ducked))

    @tool(routable=False, reversible=True)
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

        # Своё из конфига идёт последним и перекрывает найденное. Значение тут
        # бывает двух видов: путь — берём как есть, имя другой программы —
        # разрешаем по уже собранному каталогу. Второе живёт дольше: путь
        # ломается при переустановке, имя — нет.
        aliases = 0
        for name, value in self._configured.items():
            target = resolve_alias(value, catalog)
            if target is not None:
                aliases += 1
                self.log.debug("Псевдоним: %r -> %r (%s)", name, value, target)
            catalog[name] = target or value

        self._catalog = catalog
        self.log.info(
            "Программ в каталоге: %d (своих в конфиге: %d, из них псевдонимов: %d)",
            len(catalog),
            len(self._configured),
            aliases,
        )

    # --- блютуз ----------------------------------------------------------------

    def _bt(self) -> Any:
        """Модуль блютуза рядом со скиллом — по пути: скилл грузится без пакета.

        Модуль берётся заново на каждый вызов, чтобы «переподключи модуль
        windows» подхватывал и правки блютуза.
        """
        import importlib.util
        import sys

        name = "jarvis_skills.windows_bluetooth"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("bluetooth.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    @tool(phrases=["включи блютуз", "включи bluetooth", "включи блютус", "turn on bluetooth"], reversible=True, routable=False)
    async def bluetooth_on(self) -> ToolResult:
        """Включить блютуз."""
        return await self._bt_radio("On")

    @tool(phrases=["выключи блютуз", "выключи bluetooth", "выключи блютус", "turn off bluetooth"], reversible=True, routable=False)
    async def bluetooth_off(self) -> ToolResult:
        """Выключить блютуз."""
        return await self._bt_radio("Off")

    async def _bt_radio(self, state: str) -> ToolResult:
        bt = self._bt()
        try:
            status = await asyncio.to_thread(bt.radio, state)
        except bt.BluetoothError as exc:
            return ToolResult.failure(str(exc), speech={"ru": str(exc), "en": "Bluetooth is not available."})
        if status == "none":
            return ToolResult.failure(
                "нет блютуза", speech={"ru": "Блютуза на этом компьютере нет.", "en": "There is no Bluetooth here."}
            )
        if status != "Allowed":
            return ToolResult.failure(
                f"радио ответило {status}",
                speech={"ru": "Windows не дала переключить блютуз.", "en": "Windows refused to switch Bluetooth."},
            )
        on = state == "On"
        return ToolResult.success(
            {"bluetooth": on},
            speech={
                "ru": "Блютуз включён." if on else "Блютуз выключен.",
                "en": "Bluetooth is on." if on else "Bluetooth is off.",
            },
        )

    @tool(
        phrases=["какие блютуз устройства", "что подключено по блютузу", "список блютуз устройств",
                 "блютуз устройства", "bluetooth devices"],
        reversible=True, routable=False)
    async def bluetooth_devices(self) -> ToolResult:
        """Перечислить сопряжённые блютуз-устройства и что из них подключено."""
        bt = self._bt()
        try:
            found = await asyncio.to_thread(bt.devices)
        except bt.BluetoothError as exc:
            return ToolResult.failure(str(exc), speech={"ru": str(exc), "en": "Bluetooth is not available."})
        if not found:
            return ToolResult.success([], speech={"ru": "Сопряжённых устройств нет.", "en": "No paired devices."})
        connected = [device.name for device in found if device.connected]
        others = [device.name for device in found if not device.connected]
        parts = []
        if connected:
            parts.append("подключено: " + ", ".join(connected))
        if others:
            parts.append("не подключено: " + ", ".join(others[:5]))
        line = "; ".join(parts)
        return ToolResult.success(
            [{"name": device.name, "connected": device.connected} for device in found],
            speech={"ru": f"{line[:1].upper()}{line[1:]}.", "en": line},
        )

    @tool(
        phrases=["подключи {device}", "подключись к {device}", "подключи блютуз {device}", "connect {device}"],
        reversible=True,
    )
    async def bluetooth_connect(self, device: str) -> ToolResult:
        """Подключить сопряжённое блютуз-устройство: наушники, колонку или по названию.

        :param device: название устройства или его род («наушники», «колонку»).
        """
        return await self._bt_switch(device, True)

    @tool(
        phrases=["отключи {device}", "отключись от {device}", "отключи блютуз {device}",
                 "отключи {device} по блютузу", "disconnect {device}"],
        reversible=True,
    )
    async def bluetooth_disconnect(self, device: str) -> ToolResult:
        """Отключить блютуз-устройство, не разрывая сопряжения.

        :param device: название устройства или его род («наушники», «колонку»).
        """
        return await self._bt_switch(device, False)

    async def _bt_switch(self, device: str, connect: bool) -> ToolResult:
        """Найти устройство по услышанному и переключить; не уверен — предложить выбор."""
        bt = self._bt()
        try:
            found = await asyncio.to_thread(bt.devices)
        except bt.BluetoothError as exc:
            return ToolResult.failure(str(exc), speech={"ru": str(exc), "en": "Bluetooth is not available."})
        verb = "подключить" if connect else "отключить"
        tool_name = "windows.bluetooth_connect" if connect else "windows.bluetooth_disconnect"
        # Подключать имеет смысл отключённые, отключать — подключённые.
        pool = [item for item in found if item.connected != connect] or found
        kind = bt.category(device)
        if kind:
            fitting = bt.by_category(kind, pool)
            if len(fitting) == 1:
                return await self._bt_apply(bt, fitting[0], connect)
            options = fitting or pool
        else:
            names = {item.name: item for item in pool}
            exact = best_match(device, list(names), similarity=0.75)
            if exact is not None:
                return await self._bt_apply(bt, names[exact], connect)
            options = pool
        ranked = rank(device, {item.name: (item.name,) for item in options}, limit=5)
        if not ranked:
            return ToolResult.failure(
                f"нет устройства {device!r}",
                speech={"ru": f"Не нашёл блютуз-устройство {device}.", "en": f"No Bluetooth device {device}."},
            )
        return ToolResult.choosing(
            [Choice(name, Intent(tool=tool_name, arguments={"device": name})) for name in ranked],
            question={"ru": f"Что {verb}? {numbered(ranked)}.", "en": f"Which one? {numbered(ranked)}."},
        )

    async def _bt_apply(self, bt: Any, device: Any, connect: bool) -> ToolResult:
        """Подключить или отключить найденное устройство и дождаться, чем кончилось."""
        if device.connected == connect:
            state = "уже подключено" if connect else "и так отключено"
            return ToolResult.success(
                {"device": device.name}, speech={"ru": f"{device.name} {state}.", "en": f"{device.name}: nothing to do."}
            )
        try:
            accepted = await asyncio.to_thread(bt.set_connected, device.address, connect)
        except bt.BluetoothError as exc:
            return ToolResult.failure(str(exc), speech={"ru": str(exc), "en": "Bluetooth refused."})
        settled = await self._bt_settled(bt, device.name, connect)
        self.log.info(
            "Блютуз: %s %s — %s (служб приняло: %d)",
            "подключаю" if connect else "отключаю", device.name,
            "получилось" if settled else "не дождался", accepted,
        )
        if not settled:
            return ToolResult.failure(
                f"{device.name} не {'подключилось' if connect else 'отключилось'}",
                speech={"ru": f"{device.name} не отозвалось. Оно включено и рядом?", "en": f"{device.name} did not respond."},
            )
        return ToolResult.success(
            {"device": device.name, "connected": connect},
            speech={
                "ru": f"{'Подключил' if connect else 'Отключил'} {device.name}.",
                "en": f"{'Connected' if connect else 'Disconnected'} {device.name}.",
            },
        )

    async def _bt_settled(self, bt: Any, name: str, connect: bool) -> bool:
        """Дождаться, пока устройство действительно сменит состояние.

        Судить по коду возврата `BluetoothSetServiceState` нельзя — замер
        24.09.2026 (`tools/bluetooth_bench.py`) показал, что он не знает
        результата ни в одну сторону: выключенная колонка «приняла» службу и не
        подключилась, а у живой вызов вернул ноль принявших, после чего она
        подключилась. Правду знает только `fConnected` в списке устройств.
        """
        deadline = time.monotonic() + BT_SETTLE_S
        while True:
            found = next((item for item in await asyncio.to_thread(bt.devices) if item.name == name), None)
            if found is not None and found.connected == connect:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(BT_ASK_EVERY_S)

    @tool(
        phrases=[
            "открой папку {folder}",
            "покажи папку {folder}",
            "открой каталог {folder}",
            "открой директорию {folder}",
            "open folder {folder}",
            "show folder {folder}",
        ],
        reversible=True,
    )
    async def open_folder(self, folder: str) -> ToolResult:
        """Открыть папку в проводнике.

        Своим инструментом, а не через запуск программы: «открой папку
        Photostock» шаблон `открой {program}` забирал себе целиком вместе со
        словом «папку», искал такую программу и не находил (живой запуск
        12.09.2026). Шаблон тут длиннее и потому выигрывает.

        :param folder: название папки, как его произносят: «загрузки»,
            «рабочий стол», «Photostock».
        """
        home = Path(os.environ["USERPROFILE"]) if os.environ.get("USERPROFILE") else None
        catalog = await asyncio.to_thread(folder_catalog, folder_roots())
        path = match_folder(folder, catalog, home)
        if path is None:
            self.log.warning("Папка %r не найдена среди %d", folder, len(catalog))
            return ToolResult.failure(
                f"папка {folder!r} не найдена",
                speech={
                    "ru": f"Не нашёл папку {folder}.",
                    "en": f"I couldn't find the folder {folder}.",
                },
            )
        # Тем же способом, что и программы: имя в оболочку не попадает никогда,
        # открывается найденный путь.
        await asyncio.to_thread(os.startfile, path)
        self.log.info("Открыл папку: %s", path)
        return ToolResult.success(
            {"folder": path},
            speech={
                "ru": f"Открыл {Path(path).name}.",
                "en": f"Opened {Path(path).name}.",
            },
        )

    @tool(phrases=["открой {program}", "запусти {program}",
                   "open {program}", "launch {program}", "start {program}"],
          reversible=True)
    async def launch_program(self, program: str) -> ToolResult:
        """Запустить программу по названию — с обычными правами, без администратора.

        :param program: название, как его произносят: «стим», «обс», «браузер».
        """
        return await self._launch(program, elevated=False)

    @tool(phrases=["запусти {program} от имени администратора", "запусти {program} с правами администратора",
                   "запусти {program} от админа", "запусти {program} с правами админа",
                   "открой {program} от имени администратора", "открой {program} с правами администратора",
                   "открой {program} от админа", "run {program} as administrator", "run {program} as admin"],
          reversible=False, routable=False)
    async def launch_program_admin(self, program: str) -> ToolResult:
        """Запустить программу с правами администратора — только по прямой просьбе.

        :param program: название программы.
        """
        return await self._launch(program, elevated=True)

    async def _launch(self, program: str, *, elevated: bool) -> ToolResult:
        """Найти программу в каталоге и запустить с обычными правами или с правами администратора."""
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
        # Какие окна были до запуска: новое окно ищется вычитанием (просьба
        # владельца 23.09.2026 — «чтобы запущенное сразу было в фокусе»).
        before = set(window_handles()) if self._focus_launched else set()
        try:
            # Ни shell=True, ни строки-команды: только конкретный путь или URI,
            # который мы сами нашли в каталоге.
            await asyncio.to_thread(start_elevated if elevated else start_plain, target)
        except OSError as exc:
            self.log.error("Не удалось запустить %s (%s): %s", name, target, exc)
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                speech={
                    "ru": f"Не получилось запустить {name}.",
                    "en": f"Couldn't launch {name}.",
                },
            )

        self.log.info("Запущено%s: %s (%s)", " с правами администратора" if elevated else "", name, target)
        if self._focus_launched and not self.context.modes.active("quiet"):
            # Фоном, а не здесь: программа поднимается секундами, а «Запускаю
            # Steam» должно прозвучать сразу — молчащий ассистент неотличим от
            # зависшего, и команду повторяют.
            #
            # В тихом режиме в фокус не лезем вовсе: вырвать человека из
            # полноэкранной игры хуже, чем оставить окно позади.
            self.context.scope.spawn(self._focus_new(name, before), name="windows-focus")
        if elevated:
            return ToolResult.success(
                {"program": name, "target": target, "elevated": True},
                speech={"ru": f"Запускаю {name} от имени администратора.", "en": f"Launching {name} as administrator."},
            )
        return ToolResult.success(
            {"program": name, "target": target},
            speech={
                "ru": (f"Запускаю {name}.", f"{name} запускается.", f"Открываю {name}.",
                       f"Секунду, {name}."),
                "en": (f"Launching {name}.", f"Starting {name}.", f"{name}, coming up."),
            },
        )

    async def _focus_new(self, name: str, before: set[int]) -> None:
        """Дождаться окна запущенной программы и вывести его вперёд.

        Заголовок заранее неизвестен — у одной и той же программы он меняется
        от открытого файла, — поэтому ищем **новое** окно: то, которого секунду
        назад не было. Ждём с запасом: лаунчеры и тяжёлые программы рисуют окно
        далеко не сразу.

        Ошибиться тут можно только в одну сторону — поднять чужое окно,
        открывшееся в ту же секунду. Поэтому берём **первое** новое и на этом
        останавливаемся, а не гоняемся за каждым следующим.
        """
        deadline = time.monotonic() + self._focus_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(FOCUS_STEP_S)
            fresh = {handle: title for handle, title in window_handles().items()
                     if handle not in before and title}
            if not fresh:
                continue
            handle, title = next(iter(fresh.items()))
            raised = await asyncio.to_thread(bring_to_front, handle)
            self.log.info(
                "Окно %r %s", title, "вывел вперёд" if raised else "поднять не дала Windows"
            )
            return
        self.log.debug("Новое окно %s за %.0f с не появилось", name, self._focus_wait)

    # --- питание, память, клавиши ------------------------------------------
    #
    # Появились ради игрового режима (23.09.2026), но самого «игрового режима»
    # как подсистемы нет и не нужно: он собирается протоколом из этих же шагов.

    @tool(
        phrases=["схема питания {mode}", "поставь питание {mode}",
                 "режим питания {mode}", "power plan {mode}"],
        reversible=False, routable=False)
    async def set_power_plan(self, mode: str = "") -> ToolResult:
        """Переключить схему электропитания: производительность, баланс, экономия.

        Прежняя запоминается, и «верни схему питания» возвращает именно её, а не
        «сбалансированную» наугад: у человека схема бывает своя.

        :param mode: как её называют вслух — «производительность», «экономия».
        """
        wanted = power().PLAN_WORDS.get(mode.strip().lower(), "")
        if not wanted:
            known = ", ".join(sorted(set(power().PLAN_WORDS.values())))
            return ToolResult.failure(
                f"неизвестная схема питания {mode!r}; знаю: {known}",
                speech={"ru": f"Не знаю схему питания «{mode}».",
                        "en": f"I don't know the power plan {mode}."},
            )
        was = await asyncio.to_thread(power().power_plan, power().POWER_PLANS[wanted])
        if not was:
            return ToolResult.failure(
                "powercfg не ответил",
                speech={"ru": "Не получилось сменить схему питания.",
                        "en": "Couldn't change the power plan."},
            )
        if power().POWER_PLANS[wanted] != was:
            # Запоминаем только настоящую прежнюю: два переключения подряд не
            # должны стереть то, к чему возвращаться.
            self._plan_before = was
        self.log.info("Схема питания: %s (была %s)", wanted, was)
        return ToolResult.success(
            {"plan": wanted, "was": was},
            speech={"ru": (f"Питание — {mode}.", f"Схема питания: {mode}."),
                    "en": (f"Power plan: {mode}.",)},
        )

    @tool(
        phrases=["верни схему питания", "верни питание", "схема питания как была",
                 "restore power plan"],
        reversible=False, routable=False)
    async def restore_power_plan(self) -> ToolResult:
        """Вернуть ту схему питания, что была до переключения."""
        if not self._plan_before:
            return ToolResult.success(
                {},
                speech={"ru": "Схему питания я не менял.", "en": "I haven't changed the power plan."},
            )
        await asyncio.to_thread(power().power_plan, self._plan_before)
        self.log.info("Схема питания возвращена: %s", self._plan_before)
        self._plan_before = ""
        return ToolResult.success(
            {"restored": True},
            speech={"ru": ("Питание как было.", "Схему питания вернул."),
                    "en": ("Power plan restored.",)},
        )

    @tool(
        phrases=["что ест память", "кто ест память", "кто ест озу",
                 "что занимает память", "what eats memory", "memory hogs"],
        reversible=True,
    )
    async def memory_hogs(self, top: int = 3) -> ToolResult:
        """Сказать, какие программы держат больше всего памяти.

        Рядом с `hogs` про процессор и по той же причине: «ноутбук тормозит» —
        это чаще про память, чем про такты (замер 23.09.2026: у Jarvis 0.6%
        процессора и два гигабайта памяти).

        :param top: сколько программ назвать.
        """
        sizes = await asyncio.to_thread(power().memory_of, None)
        if not sizes:
            return ToolResult.failure(
                "psutil недоступен — память не посчитать",
                speech={"ru": "Не смог посмотреть память.", "en": "Couldn't check memory."},
            )
        names = dict(self.context.setting("spoken_programs", {}) or {})
        best = sorted(sizes.items(), key=lambda item: -item[1])[: max(1, top)]
        said = ", ".join(
            f"{power().spoken_name(name, names)} {size:.1f}" for name, size in best
        )
        return ToolResult.success(
            dict(best),
            speech={"ru": f"Память держат: {said} гигабайт.",
                    "en": f"Memory: {said} gigabytes."},
        )

    @tool(
        phrases=["включи оверлей", "покажи оверлей", "покажи счётчик кадров",
                 "покажи фпс", "show overlay", "show fps"],
        reversible=True, routable=False)
    async def overlay_on(self) -> ToolResult:
        """Показать оверлей RivaTuner — тот самый счётчик кадров поверх игры."""
        return await self._overlay(True)

    @tool(
        phrases=["выключи оверлей", "убери оверлей", "убери счётчик кадров",
                 "убери фпс", "hide overlay", "hide fps"],
        reversible=True, routable=False)
    async def overlay_off(self) -> ToolResult:
        """Убрать оверлей RivaTuner."""
        return await self._overlay(False)

    async def _overlay(self, on: bool) -> ToolResult:
        """Задать видимость оверлея — именно задать, а не переключить.

        Переключатель рассинхронизируется на первой же осечке: не дошло — и
        дальше «включи» начинает выключать. Поэтому состояние читается.
        """
        path = str(self.context.setting("rtss_dll", "") or "")
        where = Path(path) if path else osd().RTSS_DLL
        if osd().visible(where) is None:
            return ToolResult.failure(
                "RivaTuner не найден или не запущен",
                speech={"ru": "Не нашёл RivaTuner — оверлеем управлять нечем.",
                        "en": "I couldn't find RivaTuner."},
            )
        if not await asyncio.to_thread(osd().show, on, where):
            return ToolResult.failure(
                "RTSS не принял команду",
                speech={"ru": "Оверлей не переключился.", "en": "The overlay didn't switch."},
            )
        self.log.info("Оверлей %s", "включён" if on else "выключен")
        # Игра запускается не мгновенно, а RTSS, цепляясь к новому процессу,
        # возвращает себе прежнее состояние. Поэтому просьбу повторяем ещё
        # несколько раз: команда задаёт состояние, а не переключает, и лишний
        # повтор не стоит ничего (24.09.2026 — оверлей до игры не дожил).
        self.context.scope.spawn(self._hold_overlay(on, where), name="windows-overlay-hold")
        return ToolResult.success(
            {"overlay": on},
            speech={
                "ru": ("Оверлей включён.", "Показал счётчик.") if on else ("Оверлей убрал.", "Счётчик спрятал."),
                "en": ("Overlay on.",) if on else ("Overlay off.",),
            },
        )

    async def _hold_overlay(self, on: bool, where: Path) -> None:
        """Удержать состояние оверлея, пока игра поднимается."""
        for _ in range(OVERLAY_TRIES):
            await asyncio.sleep(OVERLAY_EVERY_S)
            if osd().visible(where) is on:
                continue
            if await asyncio.to_thread(osd().show, on, where):
                self.log.info("Оверлей сбросился — поставил снова (%s)", "вкл" if on else "выкл")

    @tool(routable=False, reversible=False)
    async def press_keys(self, combination: str) -> ToolResult:
        """Нажать сочетание клавиш за человека — например, включить OSD.

        В каталог модели не идёт намеренно: это не команда, а рычаг для шага
        протокола. У Afterburner и RTSS своей ручки для OSD нет (общая память,
        из которой мы читаем графики, только на чтение), зато есть горячая
        клавиша — её и нажимаем.

        :param combination: например «ctrl+alt+o».
        """
        if not await asyncio.to_thread(power().press, combination):
            return ToolResult.failure(
                f"не разобрал сочетание {combination!r}",
                speech={"ru": "Не понял, какие клавиши нажать.", "en": "I didn't get the hotkey."},
            )
        self.log.info("Нажал %s", combination)
        return ToolResult.success({"pressed": combination})

    @tool(phrases=["заблокируй компьютер", "заблокируй пк", "заблокируй экран",
                   "заблокируй ноутбук", "заблокируй комп", "заблокируй",
                   "lock the computer", "lock the pc", "lock screen"],
          reversible=False)
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
                   "close {program}", "quit {program}"],
          reversible=False)
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
                   "выгрузи {program}", "kill {program}", "force close {program}"],
          reversible=False)
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
    @tool(name="list_windows", routable=False, reversible=True)
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

    @tool(name="focus_window", routable=False, reversible=True)
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
                   "what is running", "list programs"],
          reversible=True)
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

    @tool(
        phrases=[
            "что греет",
            "что греет ноутбук",
            "кто грузит процессор",
            "какая нагрузка",
            "что грузит процессор",
            "кто жрёт процессор",
            "что жрёт процессор",
            "кто ест процессор",
            "почему ноутбук греется",
            "почему греется ноутбук",
            "отчего греется ноутбук",
            "what is eating the cpu",
            "why is the laptop hot",
        ],
        reversible=True,
    )
    async def hogs(self) -> ToolResult:
        """Назвать программы, которые прямо сейчас грузят процессор.

        Отвечает на вопрос, на который `core.load` ответить не может: тот
        считает **только сам ассистент**, и на жалобу «ноутбук греется» честно
        говорит «я ем полпроцента». Кто ест остальное, до сих пор было не
        спросить ни голосом, ни по логу.

        Меряется разница двух снимков, а не накопленное: Windows копит
        процессорное время с запуска программы, и без второго снимка вышел бы
        рейтинг долгожителей — браузер, открытый с утра, обогнал бы что угодно.
        """
        before = await asyncio.to_thread(process_cpu)
        if not before:
            return ToolResult.failure(
                "не удалось прочитать счётчики процессов",
                speech={
                    "ru": "Не смог посмотреть, кто грузит процессор.",
                    "en": "I couldn't check what's loading the processor.",
                },
            )
        started = time.monotonic()
        await asyncio.sleep(CPU_SAMPLE_S)
        after = await asyncio.to_thread(process_cpu)
        cores = os.cpu_count() or 1
        hogs = cpu_hogs(before, after, time.monotonic() - started, cores=cores)
        spoken = describe_hogs(hogs)
        busy = sum(share for _, share in hogs)
        self.log.info(
            "Процессор занят на %.0f%%, больше всего: %s",
            busy * 100,
            ", ".join(f"{name} {share * 100:.1f}%" for name, share in hogs[:5]) or "никто",
        )
        return ToolResult.success(
            {
                "busy": round(busy, 4),
                "cores": cores,
                "top": [(name, round(share, 4)) for name, share in hogs[:10]],
            },
            speech={
                "ru": (
                    f"Процессор занят на {round(busy * 100)} "
                    f"{plural_form(round(busy * 100), PERCENT)}. "
                    + (f"Больше всего: {spoken}." if spoken else "Заметно никто не грузит.")
                ),
                "en": (
                    f"The processor is {busy * 100:.0f} percent busy. "
                    + (f"Mostly: {spoken}." if spoken else "Nothing stands out.")
                ),
            },
        )

    @tool(phrases=["поставь громкость {level}", "сделай громкость {level}",
                   "громкость {level}", "звук {level}",
                   "set volume to {level}", "volume {level}"],
          reversible=True)
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

    @tool(phrases=["погромче", "сделай громче", "включи громче", "включи погромче",
                   "louder", "turn it up"],
          routable=False,
          reversible=True)
    async def louder(self) -> ToolResult:
        """Сделать громче на десять процентов."""
        return await self.change_volume(10)

    @tool(phrases=["потише", "сделай тише", "quieter", "turn it down"],
          routable=False,
          reversible=True)
    async def quieter(self) -> ToolResult:
        """Сделать тише на десять процентов."""
        return await self.change_volume(-10)

    @tool(reversible=True)
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

    @tool(phrases=["выключи звук", "включи звук", "mute", "unmute"], reversible=True)
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

    @tool(phrases=["обнови список программ", "refresh programs"], reversible=True, routable=False)
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
        """Запущенные процессы: имя, номер, заголовок окна.

        Заголовки нужны там, где имя процесса ничего не говорит: FL Studio
        живёт как ``FL64.exe``. Но спрашивать их у ``tasklist /v`` нельзя —
        сорок секунд на вызов, см. `with_window_titles`.
        """
        result = await self._run(["tasklist.exe", "/fo", "csv", "/nh"])
        if result.returncode != 0:
            self.log.warning("tasklist вернул %s: %s", result.returncode, result.stderr)
            return []
        windows = await asyncio.to_thread(enum_windows)
        return with_window_titles(parse_tasklist(result.stdout), windows)

    async def health(self) -> HealthStatus:
        """Скилл исправен, пока в каталоге есть хоть что-то."""
        if not self._catalog:
            return HealthStatus.degraded("каталог программ пуст")
        return HealthStatus.healthy(f"{len(self._catalog)} программ")
