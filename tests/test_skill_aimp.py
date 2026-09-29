r"""Фонотека AIMP: разбор плейлистов и то, как трек узнаётся на слух.

Владелец 24.09.2026 спросил, не написать ли для AIMP свой плагин. Плагин не
понадобился: плейлисты лежат обычным текстом, а управление идёт через окно,
которое AIMP держит для совместимости с Winamp.

Здесь проверяется та половина, что от Windows не зависит, — разбор файла и
выбор открытого плейлиста. Вторая половина (окно, кнопки, номера) измерена на
живой машине и описана в `docs/lessons.md`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> object:
    path = _ROOT / "skills" / "aimp" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aimp_{name}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


library = _load("library")
remote = _load("remote")

#: Кусок настоящего файла владельца — со всеми его особенностями: пустые теги,
#: точка с запятой между исполнителями, кириллица в пути.
REAL = """\ufeff#-----SUMMARY-----#
Name=Любимые
ContentFiles=4

#-----CONTENT-----#
D:\\Music\\Любимое\\RJ Pasin, ptasinski - neon clouds.mp3|||||||||||320|2|44100|111125|4444996|0|1|0||||0||||
D:\\Music\\Любимое\\Lights & Motion - Reborn.mp3|Reborn|Lights & Motion|Мне нравится|Lights & Motion||NaN|||||320|2|44100|247420|9929339|0|1|2||||0||||
D:\\Music\\Любимое\\NCS, buckeye - Ark.mp3|Ark|NCS;buckeye|Ark|NCS;buckeye|electronics|2026|2||||320|2|44100|180886|7345207|0|1|3||||0||||
D:\\Music\\Любимое\\Нервы - Нарушим.mp3|Нарушим|Нервы|Всё решено|Нервы||2015|||||320|2|44100|180000|7200000|0|1|4||||0||||
"""


def test_a_playlist_is_read_with_tags_and_order() -> None:
    playlist = library.parse(REAL, name="Любимые")
    assert len(playlist) == 4
    assert [track.number for track in playlist.tracks] == [0, 1, 2, 3]
    assert playlist.tracks[2].artist == "NCS;buckeye"
    assert playlist.tracks[3].title == "Нарушим"


def test_a_track_without_tags_is_named_by_its_file() -> None:
    """У владельца теги пустые примерно у трети фонотеки.

    Имя файла почти всегда и есть «Исполнитель - Название»: так их сохраняют
    качалки, и терять из-за пустого тега треть музыки было бы глупо.
    """
    playlist = library.parse(REAL)
    assert playlist.tracks[0].said == "RJ Pasin, ptasinski - neon clouds"


def test_several_artists_are_read_out_with_commas() -> None:
    """В теге они через точку с запятой, а вслух так не говорят."""
    playlist = library.parse(REAL)
    assert playlist.tracks[2].said == "NCS, buckeye - Ark"


def test_a_broken_file_is_not_a_crash() -> None:
    """Плейлист может быть пустым, обрезанным или вообще не тем файлом."""
    assert len(library.parse("")) == 0
    assert len(library.parse("#-----CONTENT-----#\n")) == 0
    assert len(library.parse("мусор без разделителя")) == 0


def test_the_open_playlist_is_found_by_length() -> None:
    """AIMP наружу отдаёт только длину списка и номер трека — по ним и ищем.

    Знать открытый плейлист нужно затем, чтобы включать трек **номером**: иначе
    пришлось бы добавлять файл в список, то есть править фонотеку владельца.
    """
    big = library.parse(REAL, name="Любимые")
    small = library.Playlist(name="Default", tracks=big.tracks[:1])
    assert library.guess_loaded([big, small], size=4) is big
    assert library.guess_loaded([big, small], size=1) is small
    assert library.guess_loaded([big, small], size=7) is None


def test_a_tie_in_length_is_broken_by_the_playing_track() -> None:
    """Два списка одной длины — спрашиваем, что играет на этом номере."""
    first = library.parse(REAL, name="Первый")
    other = library.Playlist(
        name="Второй",
        tracks=tuple(
            library.Track(path=t.path, title="Другое", artist="Кто-то", album="", number=t.number)
            for t in first.tracks
        ),
    )
    found = library.guess_loaded([first, other], size=4, playing="Кто-то - Другое", number=2)
    assert found is other


def test_the_window_title_is_stripped_to_the_track() -> None:
    """Замер на живой машине: «11. Xtreem - Covet - Winamp»."""
    assert remote.said_from_title("11. Xtreem - Covet - Winamp") == "Xtreem - Covet"
    assert remote.said_from_title("33. Beats and Styles; Papa Dee - Take It Back - Winamp") == (
        "Beats and Styles; Papa Dee - Take It Back"
    )
    assert remote.said_from_title("Winamp") == ""
    assert remote.said_from_title("") == ""


# --- закрыт, а просят музыку (живой лог 29.09.2026, 23:38) ----------------------------


def _skill_with(fake_remote: object, launched: list[str]) -> object:
    import logging
    from types import SimpleNamespace

    from jarvis.core.contracts import ToolResult

    module = _load("skill")
    module._REMOTE = fake_remote  # type: ignore[attr-defined]

    class Tools:
        def has(self, name: str) -> bool:
            return name == "windows.launch_program"

        async def invoke(self, name: str, arguments: dict) -> ToolResult:
            launched.append(arguments["program"])
            fake_remote.alive = True  # type: ignore[attr-defined]
            return ToolResult.success(None)

    class Aimp(module.AimpSkill):  # type: ignore[attr-defined,name-defined]
        log = logging.getLogger("test-aimp")
        tools = Tools()  # type: ignore[assignment]

    skill = object.__new__(Aimp)
    skill._context = SimpleNamespace(logger=logging.getLogger("test-aimp"))
    return skill


class _Remote:
    PLAY, PAUSE, NEXT, PREVIOUS = 1, 2, 3, 4

    def __init__(self) -> None:
        self.alive = False
        self.pressed: list[int] = []

    def running(self) -> bool:
        return self.alive

    def press(self, command: int) -> bool:
        self.pressed.append(command)
        return True

    def playing(self) -> object:
        return remote.Playing(said="Xtreem - Covet", number=0, total=1,  # type: ignore[attr-defined]
                              state=1 if self.pressed else 2, position_s=0, length_s=1)


async def test_play_starts_a_closed_aimp_and_says_so() -> None:
    fake, launched = _Remote(), []
    skill = _skill_with(fake, launched)
    result = await skill.control("play")  # type: ignore[attr-defined]
    assert result.ok and launched == ["AIMP"] and fake.pressed == [fake.PLAY]
    assert result.speech_for("ru") == "Запускаю AIMP, играет Xtreem - Covet."


async def test_pause_does_not_start_a_closed_aimp() -> None:
    fake, launched = _Remote(), []
    skill = _skill_with(fake, launched)
    assert not (await skill.control("pause")).ok  # type: ignore[attr-defined]
    assert launched == []


async def test_music_in_aimp_is_not_a_track_name() -> None:
    """«Включи музыку в AAMP» искало в фонотеке трек «музыку»."""
    fake, launched = _Remote(), []
    skill = _skill_with(fake, launched)
    assert (await skill.play_track("музыку")).ok  # type: ignore[attr-defined]
    assert fake.pressed == [fake.PLAY]
