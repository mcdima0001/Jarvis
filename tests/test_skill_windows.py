"""Скилл Windows: подбор программы по услышанному названию.

Скилл работает только на Windows, но вся логика подбора — чистые функции, и
проверяются они где угодно. Это и есть причина, по которой они вынесены из
класса: иначе разбор названий тестировался бы только на боевой машине.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "windows" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_windows", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Регистрация обязательна: dataclass ищет модуль класса в sys.modules,
    # и без неё падает с AttributeError. Настоящий загрузчик скиллов делает
    # то же самое.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


windows = _load()

CATALOG = {
    "OBS Studio": "C:/Menu/OBS Studio.lnk",
    "Steam": "C:/Menu/Steam.lnk",
    "Discord": "C:/Menu/Discord.lnk",
    "Google Chrome": "C:/Menu/Google Chrome.lnk",
    "REAPER (x64)": "C:/Menu/REAPER.lnk",
    "проводник": "explorer.exe",
}


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("OBS Studio", "OBS Studio"),
        ("obs", "OBS Studio"),
        ("обс", "OBS Studio"),          # Whisper пишет кириллицей
        ("стим", "Steam"),
        ("steam", "Steam"),
        ("дискорд", "Discord"),
        ("хром", "Google Chrome"),
        ("google chrome", "Google Chrome"),
        ("рипер", "REAPER (x64)"),
        ("проводник", "проводник"),
    ],
)
def test_program_found_by_spoken_name(spoken: str, expected: str) -> None:
    """Название узнаётся в любом алфавите и с любой пунктуацией.

    Whisper записывает английские названия кириллицей («обс», «стим»), а в
    меню «Пуск» они латиницей и с уточнениями в скобках.
    """
    found = windows.match_program(spoken, CATALOG)
    assert found is not None, f"не найдено: {spoken}"
    assert found[0] == expected


BIG_CATALOG = {
    name: f"{name}.lnk"
    for name in [
        "OBS Studio", "Steam", "Discord", "Google Chrome", "Mozilla Firefox",
        "REAPER (x64)", "Ableton Live 12 Suite", "Telegram Desktop", "Spotify",
        "Visual Studio Code", "Blender", "Adobe Photoshop 2024", "The Sims 4",
        "Notion", "Zoom", "Epic Games Launcher", "Audacity", "VLC media player",
        "7-Zip File Manager", "qBittorrent",
    ]
}


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("фотошоп", "Adobe Photoshop 2024"),   # ph читается как ф
        ("файрфокс", "Mozilla Firefox"),       # x читается как кс
        ("хром", "Google Chrome"),             # ch читается как х
        ("зум", "Zoom"),                       # двойная гласная
        ("ноушен", "Notion"),                  # tion читается как шн
        ("торрент", "qBittorrent"),            # узнаётся по концу слова
        ("аблетон", "Ableton Live 12 Suite"),
        ("эпик", "Epic Games Launcher"),
    ],
)
def test_pronunciation_differences_survive(spoken: str, expected: str) -> None:
    """Русское произношение и английское написание расходятся сильно.

    Побуквенная транслитерация тут не помогает: «фотошоп» и «photoshop»
    совпадают меньше чем наполовину. Спасает согласный костяк слова — гласные
    на слух плывут, согласные остаются.
    """
    found = windows.match_program(spoken, BIG_CATALOG)
    assert found is not None, f"не найдено: {spoken}"
    assert found[0] == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "кто такой трамп",
        "что такое питон",
        "окно",
        "погода на завтра",
        "включи свет",
        "сколько времени",
        "найди рецепт борща",
    ],
)
def test_ordinary_speech_launches_nothing(phrase: str) -> None:
    """Обычная фраза не должна ничего запускать.

    Все три случая были настоящими: «telegramdesktop» содержит «кто», поэтому
    вопрос про Трампа открывал Telegram; «блокнот» содержит «окно»; «такое»
    похоже на «task». Отсюда правила: слова сравниваются краем, а не любым
    куском, и запрос не разбирается на слова — только название программы.
    """
    assert windows.match_program(phrase, BIG_CATALOG) is None


def test_long_phrase_is_never_a_program() -> None:
    """Длинная фраза программой не бывает — сколько бы слов в ней ни совпало.

    Настоящий случай: «открой видео, как я обманывал всех десять лет на сайте»
    нашло «4K Video Downloader+» по слову «видео» и вызвало запрос прав
    администратора. Отказ отправит такую фразу дальше — в браузер.
    """
    catalog = {**BIG_CATALOG, "4K Video Downloader+": "4kvideodownloader.exe"}

    assert windows.match_program(
        "видео как я обманывал всех десять лет на сайте", catalog
    ) is None
    # А короткое название по-прежнему находится.
    assert windows.match_program("4K Video Downloader", catalog) is not None


def test_unknown_program_is_refused() -> None:
    """Незнакомое название — отказ, а не попытка что-то выполнить.

    Название приходит из распознавания через модель, а Jarvis работает с
    правами администратора: догадки тут недопустимы.
    """
    assert windows.match_program("кутузов вертолёт", CATALOG) is None
    assert windows.match_program("", CATALOG) is None
    assert windows.match_program("   ", CATALOG) is None


def test_shortest_match_wins() -> None:
    """При нескольких подходящих побеждает самое короткое название."""
    catalog = {
        "OBS": "obs.lnk",
        "OBS Studio Portable Edition": "obs-portable.lnk",
    }
    assert windows.match_program("обс", catalog)[0] == "OBS"


def test_start_menu_scanned(tmp_path: Path) -> None:
    """Установленные программы находятся сами, без правки конфига."""
    programs = tmp_path / "Programs"
    (programs / "OBS Studio").mkdir(parents=True)
    (programs / "OBS Studio" / "OBS Studio.lnk").touch()
    (programs / "Steam.lnk").touch()

    found = windows.scan_start_menu([programs])

    assert found["Steam"].endswith("Steam.lnk")
    assert "OBS Studio" in found


def test_service_shortcuts_skipped(tmp_path: Path) -> None:
    """Деинсталляторы и справка в каталог не попадают.

    Иначе «удали стим» рискует найти «Uninstall Steam» — а этого голосом
    точно никто не просил.
    """
    programs = tmp_path / "Programs"
    programs.mkdir()
    for name in ("Steam.lnk", "Uninstall Steam.lnk", "Справка Steam.lnk",
                 "Readme.lnk", "Сайт разработчика.lnk"):
        (programs / name).touch()

    found = windows.scan_start_menu([programs])

    assert set(found) == {"Steam"}


def test_start_menu_limit_respected(tmp_path: Path) -> None:
    """Разросшееся меню не должно раздувать каталог до бесконечности."""
    programs = tmp_path / "Programs"
    programs.mkdir()
    for index in range(20):
        (programs / f"Программа {index}.lnk").touch()

    assert len(windows.scan_start_menu([programs], limit=5)) == 5


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    """Отсутствующий каталог меню — не повод падать."""
    assert windows.scan_start_menu([tmp_path / "нет-такого"]) == {}
    assert windows.scan_program_files([tmp_path / "нет-такого"]) == {}


def test_desktop_shortcuts_indexed(tmp_path: Path) -> None:
    """Ярлыки бывают только на рабочем столе — их тоже нужно видеть.

    Steam и часть установщиков в меню «Пуск» не пишут ничего.
    """
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "Soundpad.lnk").touch()
    (desktop / "Игра.url").touch()

    found = windows.scan_start_menu([desktop])

    assert set(found) == {"Soundpad", "Игра"}


def test_programs_without_shortcuts_found(tmp_path: Path) -> None:
    """Программа без ярлыка находится по папке установки.

    Так нашёлся Soundpad: ярлыка он не оставил, а лежит в Program Files
    в папке со своим именем.
    """
    root = tmp_path / "Program Files"
    (root / "Soundpad").mkdir(parents=True)
    (root / "Soundpad" / "Soundpad.exe").touch()
    (root / "Soundpad" / "unins000.exe").touch()

    found = windows.scan_program_files([root])

    assert list(found) == ["Soundpad"]
    assert found["Soundpad"].endswith("Soundpad.exe")


def test_random_executables_ignored(tmp_path: Path) -> None:
    """В каталог идёт только файл, названный как папка.

    Иначе туда попадут установщики, обновлялки и служебные утилиты — их в
    Program Files больше, чем самих программ.
    """
    root = tmp_path / "Program Files"
    (root / "Какая-то программа").mkdir(parents=True)
    (root / "Какая-то программа" / "setup.exe").touch()
    (root / "Какая-то программа" / "updater.exe").touch()

    assert windows.scan_program_files([root]) == {}


def test_steam_libraries_read(tmp_path: Path) -> None:
    """Игры Steam лежат в библиотеках, список которых он хранит сам.

    Библиотек бывает несколько и на разных дисках.
    """
    steam = tmp_path / "Steam" / "steamapps"
    steam.mkdir(parents=True)
    library = tmp_path / "D_Games"
    (library / "steamapps" / "common").mkdir(parents=True)
    (steam / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        f'\t"0"\n\t{{\n\t\t"path"\t\t"{tmp_path / "Steam"}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{library}"\n\t}}\n}}\n',
        encoding="utf-8",
    )

    import os

    os.environ["ProgramFiles(x86)"] = str(tmp_path)
    try:
        libraries = windows.steam_library_dirs()
    finally:
        del os.environ["ProgramFiles(x86)"]

    assert library / "steamapps" / "common" in libraries


#: Вывод `tasklist /fo csv /nh /v`: девять колонок, последняя — заголовок окна.
_TASKLIST = (
    '"chrome.exe","1234","Console","1","250 000 КБ","Running","DESK\\user",'
    '"0:03:12","YouTube — Google Chrome"\r\n'
    '"FL64.exe","5678","Console","1","900 000 КБ","Running","DESK\\user",'
    '"1:20:04","FL Studio 21"\r\n'
    '"steam.exe","4321","Console","1","180 000 КБ","Running","DESK\\user",'
    '"0:00:41","Steam"\r\n'
    '"svchost.exe","900","Services","0","8 КБ","Unknown","N/A","0:00:00","N/A"\r\n'
    '"System Idle Process","0","Services","0","8 КБ","Unknown","N/A","0:00:00","N/A"\r\n'
)


def test_tasklist_parsed() -> None:
    """Из вывода tasklist берутся имя, номер процесса и заголовок окна."""
    processes = windows.parse_tasklist(_TASKLIST)
    by_image = {process.image: process for process in processes}

    assert by_image["FL64.exe"].pid == 5678
    assert by_image["FL64.exe"].title == "FL Studio 21"
    # У процесса без окна заголовка нет, а «N/A» — это не заголовок.
    assert by_image["svchost.exe"].title == ""
    assert "System Idle Process" not in by_image


def test_program_closed_by_window_title() -> None:
    """Имя процесса и название программы совпадают далеко не всегда.

    FL Studio работает как FL64.exe, и «закрой фл студио» по именам процессов
    не находило ничего. Название, под которым программу знает человек, есть
    в заголовке окна.
    """
    catalog = windows.process_catalog(windows.parse_tasklist(_TASKLIST))

    assert windows.match_program("фл студио", catalog)[1] == "FL64.exe"
    assert windows.match_program("FL Studio", catalog)[1] == "FL64.exe"
    assert windows.match_program("стим", catalog)[1] == "steam.exe"
    assert windows.match_program("хром", catalog)[1] == "chrome.exe"


#: Steam так и выглядит на самом деле: окно рисует не тот процесс, что запускали.
_STEAM_TASKLIST = (
    '"steam.exe","4321","Console","1","180 000 КБ","Running","DESK\\user",'
    '"0:00:41","N/A"\r\n'
    '"steamwebhelper.exe","4400","Console","1","400 000 КБ","Running","DESK\\user",'
    '"0:00:30","Steam"\r\n'
    '"steamwebhelper.exe","4401","Console","1","90 000 КБ","Running","DESK\\user",'
    '"0:00:30","N/A"\r\n'
    '"steamservice.exe","4402","Services","0","9 000 КБ","Running","N/A",'
    '"0:00:01","N/A"\r\n'
    '"explorer.exe","300","Console","1","70 000 КБ","Running","DESK\\user",'
    '"0:10:00","Steam"\r\n'
)


def test_helpers_found_by_process_name() -> None:
    """Помощники — те, чьё имя начинается с имени главного процесса."""
    processes = windows.parse_tasklist(_STEAM_TASKLIST)

    assert windows.helper_pids(processes, "steam.exe") == {4400, 4401, 4402}


def test_window_of_another_program_is_not_a_helper() -> None:
    """Папка «Steam» в проводнике — не Steam.

    Поэтому помощники ищутся по имени процесса, а не по заголовку окна:
    заголовок «Steam» бывает и у проводника, и у вкладки браузера.
    """
    processes = windows.parse_tasklist(_STEAM_TASKLIST)

    assert 300 not in windows.helper_pids(processes, "steam.exe")


def test_program_without_helpers() -> None:
    """У обычной программы помощников нет — и искать нечего."""
    processes = windows.parse_tasklist(_TASKLIST)

    assert windows.helper_pids(processes, "chrome.exe") == set()


def test_short_process_name_has_no_helpers() -> None:
    """Короткая основа цепляла бы посторонних: «fl» нашлось бы во «flux»."""
    processes = [
        windows.Process(image="fl.exe", pid=1),
        windows.Process(image="flux.exe", pid=2),
    ]

    assert windows.helper_pids(processes, "fl.exe") == set()


def test_steam_lives_in_tray() -> None:
    """Для Steam «закрой» означает «убери окно», а не «выйди».

    Клиент обязан остаться в трее: без него не работают загрузки и оверлей
    в играх. Полный выход — отдельная команда «убей стим».
    """
    assert "steam.exe" in windows.TRAY_APPS


@pytest.mark.parametrize(
    "name",
    ["chrome.exe", "obs64.exe", "ms-teams.exe"],
)
def test_process_names_accepted(name: str) -> None:
    """Обычные имена процессов проходят проверку."""
    assert windows._PROCESS_NAME.match(name)


@pytest.mark.parametrize(
    "name",
    ['chrome.exe" & del /f', "chrome.exe ; rm", "../../evil.exe", "chrome.bat", ""],
)
def test_dangerous_process_names_rejected(name: str) -> None:
    """Всё, что похоже на попытку подмешать команду, отбивается.

    Имена берутся из вывода tasklist и в оболочку не попадают в принципе, но
    аргумент внешней команды проверяется явно, а не подразумевается.
    """
    assert not windows._PROCESS_NAME.match(name)


def test_built_in_tools_present() -> None:
    """Встроенные средства Windows доступны и по-русски, и по-английски."""
    assert windows.match_program("диспетчер задач", windows.BUILT_IN)[1] == "taskmgr.exe"
    assert windows.match_program("настройки", windows.BUILT_IN)[1] == "ms-settings:"
    assert windows.match_program("калькулятор", windows.BUILT_IN)[1] == "calc.exe"


# --- порядок закрытия -------------------------------------------------------


def _closer(**overrides: Any) -> Any:
    """Скилл с подменёнными внешними вызовами: без Windows и без процессов."""
    import logging

    class Closer(windows.WindowsSkill):
        """Логгер вместо контекста — остальное настоящее."""

        log = logging.getLogger("test-windows")

    skill = object.__new__(Closer)
    skill._quit_commands = dict(windows.QUIT_URIS)
    skill._tray_apps = set(windows.TRAY_APPS)
    skill._force_close = False
    for name, value in overrides.items():
        setattr(skill, name, value)
    return skill


async def test_tray_app_only_loses_its_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """У программы из трея окно убирается, а процесс остаётся жить.

    Дожидаться её исчезновения незачем — она обязана остаться, — и добивать
    её taskkill тем более нельзя.
    """
    monkeypatch.setattr(windows, "close_windows", lambda pids: 1)

    skill = _closer()
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    assert await skill._close("steam.exe", {1234}, force=False) == "окно"


async def test_hidden_tray_app_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если окна уже нет, программу из трея трогать не за что.

    Без этого правила отсутствие окна выглядело бы как неудача, и Steam
    добивался бы через taskkill — ровно то, чего просили не делать.
    """
    monkeypatch.setattr(windows, "close_windows", lambda pids: 0)

    skill = _closer()
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    assert await skill._close("steam.exe", {1234}, force=False) == "трей"


async def test_window_is_closed_through_a_helper_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно ищется и у процессов-помощников, если у главного его нет.

    Настоящий сбой: интерфейс Steam рисует steamwebhelper.exe, у steam.exe
    видимых окон нет вовсе. Ассистент отвечал «Steam и так свёрнут», когда
    Steam был развёрнут во весь экран.
    """
    asked: list[set[int]] = []

    def close(pids: set[int]) -> int:
        asked.append(set(pids))
        return 0 if pids == {1234} else 1

    monkeypatch.setattr(windows, "close_windows", close)

    skill = _closer()
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    how = await skill._close("steam.exe", {1234}, force=False, helpers={5678})

    assert how == "окно"
    assert asked == [{1234}, {5678}]


async def test_helpers_are_left_alone_while_the_main_window_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока окно нашлось у самой программы, к соседям не лезем."""
    asked: list[set[int]] = []

    def close(pids: set[int]) -> int:
        asked.append(set(pids))
        return 1

    monkeypatch.setattr(windows, "close_windows", close)

    skill = _closer()
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    assert await skill._close("steam.exe", {1234}, force=False, helpers={5678}) == "окно"
    assert asked == [{1234}]


_BROWSER_TASKLIST = (
    '"browser.exe","2000","Console","1","300 000 КБ","Running","DESK\\user",'
    '"0:05:00","YouTube - Яндекс Браузер"\r\n'
    '"browser.exe","2000","Console","1","300 000 КБ","Running","DESK\\user",'
    '"0:05:00","Почта - Яндекс Браузер"\r\n'
    '"FL64.exe","5678","Console","1","900 000 КБ","Running","DESK\\user",'
    '"1:20:04","FL Studio 21"\r\n'
)


async def test_site_is_closed_by_window_not_by_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Закрой YouTube» убирает одно окно, а не весь браузер.

    Настоящий сбой: все окна браузера принадлежат одному процессу, поэтому
    запрос уходил всем сразу, браузер не исчезал, и следом его добивал
    taskkill — вместе с остальными вкладками.
    """
    asked: list[tuple[set[int], str | None]] = []

    def close(pids: set[int], *, title: str | None = None) -> int:
        asked.append((set(pids), title))
        return 1

    monkeypatch.setattr(windows, "close_windows", close)

    skill = _closer()
    skill._processes = _processes(_BROWSER_TASKLIST)
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    result = await skill._shutdown("YouTube", force=False)

    assert result.ok
    assert asked == [({2000}, "YouTube - Яндекс Браузер")]
    assert result.value["window"] == "YouTube - Яндекс Браузер"


async def test_missing_window_is_not_a_reason_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно не нашлось — отказ, а не «тогда закроем всё остальное»."""
    monkeypatch.setattr(windows, "close_windows", lambda pids, *, title=None: 0)

    skill = _closer()
    skill._processes = _processes(_BROWSER_TASKLIST)
    skill._wait_gone = _should_not_wait
    skill._run = _should_not_run

    result = await skill._shutdown("YouTube", force=False)

    assert not result.ok


async def test_program_named_directly_still_closes_whole_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Закрой браузер» по имени программы закрывает её целиком.

    Разница именно в том, чем совпало: заголовок окна — это окно, имя
    процесса — это программа.
    """
    asked: list[tuple[set[int], str | None]] = []

    def close(pids: set[int], *, title: str | None = None) -> int:
        asked.append((set(pids), title))
        return 2

    monkeypatch.setattr(windows, "close_windows", close)

    skill = _closer()
    skill._processes = _processes(_BROWSER_TASKLIST)
    skill._run = _should_not_run

    async def gone(image: str, *, timeout: float = 4.0) -> bool:
        return True

    skill._wait_gone = gone
    result = await skill._shutdown("browser", force=False)

    assert result.ok
    assert asked[0][1] is None


async def test_kill_ignores_window_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Убей X» снимает процесс, даже если название совпало с заголовком окна."""
    monkeypatch.setattr(windows, "close_windows", _should_not_close)

    ran: list[list[str]] = []

    async def run(command: list[str]) -> Any:
        ran.append(command)
        return _Completed()

    skill = _closer()
    skill._processes = _processes(_BROWSER_TASKLIST)
    skill._wait_gone = _should_not_wait
    skill._run = run

    result = await skill._shutdown("YouTube", force=True)

    assert result.ok
    assert ran == [["taskkill.exe", "/im", "browser.exe", "/f"]]


def _processes(output: str):
    """Подменить чтение списка процессов готовым выводом tasklist."""

    async def read():
        return windows.parse_tasklist(output)

    return read


async def test_kill_skips_windows_and_forces(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Убей» снимает процесс сразу, минуя окна и вежливые способы."""
    import subprocess

    monkeypatch.setattr(windows, "close_windows", _should_not_close)
    commands: list[list[str]] = []

    async def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    skill = _closer()
    skill._wait_gone = _should_not_wait
    skill._run = run

    assert await skill._close("steam.exe", {1234}, force=True) == "taskkill"
    assert commands == [["taskkill.exe", "/im", "steam.exe", "/f"]]


async def test_quit_command_used_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заданная в конфиге команда выхода пробуется первой.

    Так можно вернуть полный выход для программы из трея, если он нужен:
    quit_commands: {steam.exe: "steam://exit"}.
    """
    started: list[str] = []
    monkeypatch.setattr(windows.os, "startfile", started.append, raising=False)

    skill = _closer()
    skill._quit_commands = {"steam.exe": "steam://exit"}
    skill._wait_gone = _always_gone
    skill._run = _should_not_run

    assert await skill._close("steam.exe", {1234}, force=False) == "steam://exit"
    assert started == ["steam://exit"]


async def test_ordinary_program_closed_by_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обычной программе шлётся запрос окну — то же, что Alt+F4.

    Так она успевает спросить про несохранённое, чего taskkill /f не даёт.
    """
    monkeypatch.setattr(windows, "close_windows", lambda pids: len(pids))

    skill = _closer()
    skill._wait_gone = _always_gone
    skill._run = _should_not_run

    assert await skill._close("notepad.exe", {1, 2}, force=False) == "окно"


async def test_stubborn_program_falls_back_to_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если программа не закрылась ни сама, ни по окну — остаётся taskkill."""
    import subprocess

    monkeypatch.setattr(windows, "close_windows", lambda pids: 1)
    commands: list[list[str]] = []

    async def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    async def never_gone(image: str, *, timeout: float = 4.0) -> bool:
        return False

    skill = _closer()
    skill._wait_gone = never_gone
    skill._run = run

    assert await skill._close("stubborn.exe", {7}, force=False) == "taskkill"
    assert commands == [["taskkill.exe", "/im", "stubborn.exe"]]
    # Без force_close ключ /f не добавляется: несохранённое дороже.
    assert "/f" not in commands[0]


async def _always_gone(image: str, *, timeout: float = 4.0) -> bool:
    """Процесс исчез сразу."""
    return True


async def _should_not_run(command: list[str]) -> Any:
    """Внешняя команда на этом пути вызываться не должна."""
    raise AssertionError(f"лишний вызов: {command}")


async def _should_not_wait(image: str, *, timeout: float = 4.0) -> bool:
    """Ожидание исчезновения на этом пути бессмысленно."""
    raise AssertionError(f"лишнее ожидание: {image}")


def _should_not_close(pids: set[int], *, title: str | None = None) -> int:
    """Окна на этом пути трогать не должны."""
    raise AssertionError(f"лишнее закрытие окон: {pids}")


class _Completed:
    """Успешный результат внешней команды."""

    returncode = 0
    stdout = ""
    stderr = ""


# --- чужое не хватать -------------------------------------------------------


@pytest.mark.parametrize("query", ["гитхап", "гитхаб", "ютуб", "почта", "твич"])
def test_site_names_do_not_match_programs(query: str) -> None:
    """Название сайта не должно находить программу с похожим словом внутри.

    Настоящий сбой: «открой гитхап» запускало «Ample Guitar» — «githap»
    похоже на «guitar» на 0.73. Нечёткое сравнение теперь идёт только с
    названием целиком: отдельное слово внутри длинного названия — слишком
    слабое основание, чтобы что-то запускать.
    """
    catalog = {**CATALOG, "Ample Guitar": "C:/Menu/Ample Guitar.url"}

    assert windows.match_program(query, catalog) is None


def test_guitar_still_found_by_its_own_name() -> None:
    """При этом сама программа по своему названию находится."""
    catalog = {**CATALOG, "Ample Guitar": "C:/Menu/Ample Guitar.url"}

    assert windows.match_program("гитар", catalog)[0] == "Ample Guitar"
    assert windows.match_program("ампл гитар", catalog)[0] == "Ample Guitar"


# --- приглушение чужого звука -----------------------------------------------


def _session(pid: int, name: str, volume: float) -> Any:
    """Описание звуковой сессии для проверок."""
    return windows.SoundSession(pid=pid, name=name, volume=volume)


def test_own_session_is_never_ducked() -> None:
    """Свой звук не трогаем: иначе ответ утонет вместе с музыкой.

    Ради этого приглушение и делается по сессиям приложений, а не общей
    громкостью системы — та убавила бы и Jarvis.
    """
    sessions = [
        _session(100, "python.exe", 1.0),
        _session(200, "browser.exe", 0.8),
    ]

    plan = windows.plan_ducking(sessions, own_pids={100}, level=0.2)

    assert plan == {200: 0.8}


def test_already_quiet_sessions_are_left_alone() -> None:
    """Тихую сессию приглушать нечего, а «вернуть» значило бы сделать громче."""
    sessions = [_session(300, "quiet.exe", 0.1), _session(400, "loud.exe", 0.9)]

    plan = windows.plan_ducking(sessions, own_pids=set(), level=0.2)

    assert plan == {400: 0.9}


def test_several_sessions_of_one_program_share_a_pid() -> None:
    """У приложения бывает несколько сессий — запоминаем громче всех звучащую."""
    sessions = [_session(500, "game.exe", 0.4), _session(500, "game.exe", 0.9)]

    plan = windows.plan_ducking(sessions, own_pids=set(), level=0.2)

    assert plan == {500: 0.9}


def test_sessions_without_a_process_are_skipped() -> None:
    """Системные звуки приходят без номера процесса — их не вернуть обратно."""
    sessions = [_session(0, "", 1.0)]

    assert windows.plan_ducking(sessions, own_pids=set(), level=0.2) == {}
