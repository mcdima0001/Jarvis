"""Видео на паузу, музыка — на приглушение (просьба владельца 23.09.2026).

Разница не вкусовая: приглушённая музыка звучит тише, но играет то же самое, а
приглушённое видео теряет кусок сюжета, и вернуть его можно только руками.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    """Загрузить модуль скилла так же, как это делает загрузчик."""
    path = _ROOT / "skills" / "windows" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"windows_{name}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


media = _load("media")


@dataclass(frozen=True)
class Sound:
    """Звуковая сессия — ровно те поля, по которым принимается решение."""

    pid: int
    name: str
    playing: bool = True


def test_a_player_is_recognised_by_its_process_name() -> None:
    assert media.is_video_player("vlc.exe")
    assert media.is_video_player("PotPlayerMini64.exe")
    assert media.is_video_player("Video.UI.exe"), "«Кино и ТВ» в Windows"
    assert not media.is_video_player("")


def test_the_browser_is_not_a_video_player() -> None:
    """Во вкладке одинаково бывает фильм и музыка — снаружи их не различить."""
    for name in ("chrome.exe", "msedge.exe", "browser.exe", "firefox.exe"):
        assert not media.is_video_player(name)


def test_music_keeps_playing_and_only_video_is_stopped() -> None:
    sessions = [Sound(pid=10, name="browser.exe"), Sound(pid=11, name="vlc.exe"),
                Sound(pid=12, name="AIMP.exe")]
    assert media.plan_pausing(sessions, own_pids=set()) == (11,)


def test_a_silent_player_is_left_alone() -> None:
    """Молчащий плеер уже на паузе — «возврат» запустил бы его без просьбы."""
    sessions = [Sound(pid=11, name="vlc.exe", playing=False)]
    assert media.plan_pausing(sessions, own_pids=set()) == ()


def test_the_assistant_never_pauses_itself() -> None:
    sessions = [Sound(pid=7, name="vlc.exe")]
    assert media.plan_pausing(sessions, own_pids={7}) == ()


def test_one_player_is_named_once_even_with_several_sessions() -> None:
    sessions = [Sound(pid=11, name="vlc.exe"), Sound(pid=11, name="vlc.exe")]
    assert media.plan_pausing(sessions, own_pids=set()) == (11,)


def test_the_owner_can_add_his_own_player() -> None:
    sessions = [Sound(pid=11, name="MyPlayer.exe")]
    assert media.plan_pausing(sessions, own_pids=set(), players=("myplayer",)) == (11,)


# --- VLC по HTTP -------------------------------------------------------------


def test_vlc_without_a_password_is_not_used_at_all() -> None:
    """Пустой пароль означает «владелец не включал» — идём прежним путём."""
    vlc = media.Vlc(password="")
    assert not vlc.ready and not vlc.pause() and not vlc.play()


def test_vlc_pause_is_believed_only_when_vlc_says_it_paused(monkeypatch) -> None:
    """Ответ VLC разбирается, а не принимается на веру: осечку надо видеть."""
    vlc = media.Vlc(password="secret")
    answers = {"pl_forcepause": "<root><state>paused</state></root>",
               "pl_forceresume": "<root><state>playing</state></root>"}
    monkeypatch.setattr(vlc, "_ask", lambda command="": answers.get(command, ""))
    assert vlc.pause() and vlc.play()

    monkeypatch.setattr(vlc, "_ask", lambda command="": "")
    assert not vlc.pause(), "не ответил — значит не остановлен"


def test_a_file_named_paused_does_not_fool_us(monkeypatch) -> None:
    """Разбираем поле, а не ищем слово: в названии файла бывает что угодно."""
    vlc = media.Vlc(password="secret")
    answer = "<root><state>playing</state><info name='filename'>paused.mkv</info></root>"
    monkeypatch.setattr(vlc, "_ask", lambda command="": answer)
    assert not vlc.pause(), "VLC ответил, что всё ещё играет"


def test_vlc_state_is_read_from_its_answer(monkeypatch) -> None:
    vlc = media.Vlc(password="secret")
    monkeypatch.setattr(vlc, "_ask", lambda command="": "<root><state>playing</state></root>")
    assert vlc.state() == "playing"
    monkeypatch.setattr(vlc, "_ask", lambda command="": "")
    assert vlc.state() == ""
