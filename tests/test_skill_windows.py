"""Скилл Windows: подбор программы по услышанному названию.

Скилл работает только на Windows, но вся логика подбора — чистые функции, и
проверяются они где угодно. Это и есть причина, по которой они вынесены из
класса: иначе разбор названий тестировался бы только на боевой машине.
"""

from __future__ import annotations

import importlib.util
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
    """Из вывода tasklist берутся имена процессов и заголовки окон."""
    catalog = windows.parse_tasklist(_TASKLIST)

    assert catalog["chrome"] == "chrome.exe"
    assert catalog["steam"] == "steam.exe"
    # Процессов без окна это не касается.
    assert "N/A" not in catalog
    assert "System Idle Process" not in catalog.values()


def test_program_closed_by_window_title() -> None:
    """Имя процесса и название программы совпадают далеко не всегда.

    FL Studio работает как FL64.exe, и «закрой фл студио» по именам процессов
    не находило ничего. Название, под которым программу знает человек, есть
    в заголовке окна.
    """
    catalog = windows.parse_tasklist(_TASKLIST)

    assert windows.match_program("фл студио", catalog)[1] == "FL64.exe"
    assert windows.match_program("FL Studio", catalog)[1] == "FL64.exe"
    assert windows.match_program("стим", catalog)[1] == "steam.exe"
    assert windows.match_program("хром", catalog)[1] == "chrome.exe"


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
