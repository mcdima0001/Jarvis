"""Перезапуск: по просьбе, с переживающим его состоянием и сам на новое ядро (29.09.2026)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core.config.schema import RestartConfig
from jarvis.core.contracts import Intent, ToolResult
from jarvis.core.dialogue import Conversation
from jarvis.core.pending import Pending
from jarvis.core.restart import Carryover, CoreWatch, Lifecycle, changed, fingerprint
from jarvis.core.state import BRIEF, DEAF, QUIET, Modes


class _Dispatcher:
    def __init__(self, pending: Pending | None = None) -> None:
        self.awaiting = pending

    def ask_again(self, question: Pending) -> None:
        self.awaiting = question


class _Jobs:
    def __init__(self, *titles: str) -> None:
        self.running = tuple(SimpleNamespace(title=title, language="ru") for title in titles)


# --- отпечаток -------------------------------------------------------------------


def test_fingerprint_sees_a_changed_file_and_ignores_the_cache(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("a = 1", "utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "core.py").write_text("x", "utf-8")
    (tmp_path / "notes.txt").write_text("x", "utf-8")
    before = fingerprint([tmp_path])
    assert list(before) == [str(tmp_path / "core.py")]

    (tmp_path / "core.py").write_text("a = 22", "utf-8")
    (tmp_path / "new.py").write_text("", "utf-8")
    assert changed(before, fingerprint([tmp_path])) == ["core.py", "new.py"]


# --- просьба перезапуститься --------------------------------------------------------


async def _ok() -> str:
    return ""


async def _broken() -> str:
    return "SyntaxError: invalid syntax"


async def test_without_the_tray_there_is_nobody_to_restart_the_process() -> None:
    life = Lifecycle(asyncio.Event(), check=_ok)
    assert "трея" in await life.restart("просили")
    assert not life.stopping.is_set()


async def test_broken_new_code_cancels_the_restart() -> None:
    life = Lifecycle(asyncio.Event(), check=_broken)
    life.restartable = True
    assert "не собирается" in await life.restart("просили")
    assert not life.stopping.is_set() and not life.restarting


async def test_unchanged_code_is_not_rebuilt_and_restart_goes() -> None:
    calls: list[int] = []

    async def check() -> str:
        calls.append(1)
        return "не должна звучать"

    life = Lifecycle(asyncio.Event(), check=check, code_changed=lambda: False)
    life.restartable = True
    assert await life.restart("просили") == ""
    assert life.stopping.is_set() and life.restarting and not life.quietly
    assert calls == []


async def test_the_voice_command_names_the_refusal() -> None:
    from jarvis.core.builtin import CoreTools

    async def refuse(reason: str) -> str:
        return "новый код не собирается: ошибка"

    tools = CoreTools(llm=None, memory=None, registry=None, skills=None, restart=refuse)  # type: ignore[arg-type]
    result: ToolResult = await tools.restart()
    assert not result.ok
    assert "не собирается" in result.speech_for("ru")


# --- что переживает перезапуск ---------------------------------------------------------


def _carryover(path: Path, *, modes: Modes, talk: Conversation, dispatcher: _Dispatcher, jobs: _Jobs) -> Carryover:
    return Carryover(path, modes=modes, conversation=talk, dispatcher=dispatcher, jobs=jobs)  # type: ignore[arg-type]


def test_modes_talk_question_and_unfinished_work_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.json"
    modes, talk = Modes(), Conversation()
    modes.on(DEAF, minutes=30)
    modes.on(BRIEF)
    modes.on(QUIET)
    talk.said("что за самолёт взлетел")
    talk.replied("S7, летит в Новосибирск")
    question = Pending.about(
        Intent(tool="telegram.send", arguments={"to": "мама", "text": "буду"}), question="Отправить?"
    )
    _carryover(path, modes=modes, talk=talk, dispatcher=_Dispatcher(question), jobs=_Jobs("погода в Твери")).save(
        restart=True
    )

    modes2, talk2, dispatcher2 = Modes(), Conversation(), _Dispatcher()
    carried = _carryover(path, modes=modes2, talk=talk2, dispatcher=dispatcher2, jobs=_Jobs()).restore()

    assert carried is not None and carried.restart and not carried.quietly
    assert carried.unfinished == ("погода в Твери",)
    deaf = modes2.get(DEAF)
    assert deaf is not None and 29 * 60 < deaf.remaining(time.monotonic()) <= 30 * 60
    assert modes2.active(BRIEF)
    # Тихий режим не флаг: он закрывает панель и отпускает модели — флаг без этого врал бы.
    assert not modes2.active(QUIET)
    assert [turn.text for turn in talk2.turns()] == ["что за самолёт взлетел", "S7, летит в Новосибирск"]
    assert dispatcher2.awaiting is not None
    assert dispatcher2.awaiting.intent.tool == "telegram.send"
    assert dispatcher2.awaiting.intent.arguments == {"to": "мама", "text": "буду"}
    assert not path.exists()


def test_an_old_state_is_not_brought_back(tmp_path: Path) -> None:
    path = tmp_path / "restart.json"
    path.write_text(json.dumps({"saved_at": time.time() - 3600, "restart": True, "modes": [{"name": DEAF, "left_s": 0}]}))
    modes = Modes()
    carried = _carryover(path, modes=modes, talk=Conversation(), dispatcher=_Dispatcher(), jobs=_Jobs()).restore()
    assert carried is None
    assert not modes.active(DEAF)
    assert not path.exists()


def test_a_mode_that_ran_out_during_the_restart_stays_off(tmp_path: Path) -> None:
    path = tmp_path / "restart.json"
    path.write_text(json.dumps({"saved_at": time.time() - 20, "modes": [{"name": DEAF, "left_s": 10}]}))
    modes = Modes()
    _carryover(path, modes=modes, talk=Conversation(), dispatcher=_Dispatcher(), jobs=_Jobs()).restore()
    assert not modes.active(DEAF)


# --- перезапуск сам -----------------------------------------------------------------------


class _Registry:
    def __init__(self, playing: list[str]) -> None:
        self.playing = playing

    def has(self, name: str) -> bool:
        return name == "windows.sound_playing"

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success({"playing": self.playing})


class _Announcer:
    def __init__(self) -> None:
        self.said: list[str] = []

    def offer(self, text: str, **_: Any) -> str:
        self.said.append(text)
        return "spoken"


def _watch(tmp_path: Path, *, auto: str = "idle", check: Any = _ok, playing: list[str] | None = None,
           announcer: _Announcer | None = None) -> tuple[CoreWatch, Lifecycle]:
    life = Lifecycle(asyncio.Event(), check=check)
    life.restartable = True
    settings = RestartConfig(auto=auto, idle_s=120, settle_s=60, busy_tool="windows.sound_playing")
    watch = CoreWatch(
        lifecycle=life, settings=settings, watched=[tmp_path], check=check,
        registry=_Registry(playing or []), announcer=announcer,  # type: ignore[arg-type]
    )
    return watch, life


async def _changed_and_settled(watch: CoreWatch, tmp_path: Path, start: float) -> None:
    (tmp_path / "core.py").write_text("a = 2", "utf-8")
    assert await watch.tick(now=start) == "changing"
    assert await watch.tick(now=start + 30) == "settling"


async def test_a_new_core_restarts_quietly_once_the_owner_is_silent(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("a = 1", "utf-8")
    watch, life = _watch(tmp_path)
    await watch.start()
    assert await watch.tick() == "same"

    start = time.monotonic()
    await _changed_and_settled(watch, tmp_path, start)
    # Только что говорили — ждём.
    assert await watch.tick(now=start + 61) == "busy"
    assert not life.stopping.is_set()

    assert await watch.tick(now=start + 200) == "restart"
    assert life.restarting and life.quietly
    assert "core.py" in life.reason
    await watch.stop()


async def test_playing_sound_holds_the_restart(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("a = 1", "utf-8")
    watch, life = _watch(tmp_path, playing=["AIMP.exe"])
    await watch.start()
    start = time.monotonic()
    await _changed_and_settled(watch, tmp_path, start)
    assert await watch.busy(now=start + 200) == "играет звук: AIMP.exe"
    assert await watch.tick(now=start + 200) == "busy"
    assert not life.stopping.is_set()
    await watch.stop()


async def test_a_new_core_that_does_not_build_is_not_restarted_into(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("a = 1", "utf-8")
    watch, life = _watch(tmp_path, check=_broken)
    await watch.start()
    start = time.monotonic()
    await _changed_and_settled(watch, tmp_path, start)
    assert await watch.tick(now=start + 200) == "broken"
    # Второй раз ту же сборку не пробуем и в лог не пишем.
    assert await watch.tick(now=start + 230) == "settled"
    assert not life.stopping.is_set()
    await watch.stop()


async def test_tell_mode_says_it_once_and_does_not_restart(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("a = 1", "utf-8")
    announcer = _Announcer()
    watch, life = _watch(tmp_path, auto="tell", announcer=announcer)
    await watch.start()
    start = time.monotonic()
    await _changed_and_settled(watch, tmp_path, start)
    assert await watch.tick(now=start + 200) == "told"
    assert await watch.tick(now=start + 230) == "settled"
    assert len(announcer.said) == 1 and "перезапустись" in announcer.said[0]
    assert not life.stopping.is_set()
    await watch.stop()


def test_the_restart_mode_is_checked_by_the_loader(tmp_path: Path) -> None:
    from jarvis.core.config import load_config
    from jarvis.core.errors import ConfigError

    config = tmp_path / "config.yaml"
    config.write_text("runtime:\n  restart:\n    auto: always\n", "utf-8")
    with pytest.raises(ConfigError, match="runtime.restart.auto"):
        load_config(config, root=tmp_path)


# --- скиллы обновились на диске ---------------------------------------------------------------


class _Skills:
    """Менеджер скиллов на папках во временном каталоге."""

    def __init__(self, root: Path) -> None:
        from jarvis.core.skills.discovery import discover

        self._root = root
        self._discover = discover
        self.loaded = ("peace", "browser", "page")
        self.disabled: frozenset[str] = frozenset()
        self.reloaded: list[str] = []
        self.adopted: list[str] = []

    def candidates(self) -> list[Any]:
        return self._discover([self._root])

    def candidate(self, name: str) -> Any:
        return next((c for c in self.candidates() if c.name == name), None)

    def resolve(self, name: str) -> str:
        return name

    async def reload(self, name: str) -> None:
        self.reloaded.append(name)

    async def adopt(self, name: str) -> None:
        self.adopted.append(name)


def _skill_tree(root: Path) -> None:
    for folder in ("peace", "browser", "browser/page"):
        (root / folder).mkdir(parents=True)
        (root / folder / "skill.py").write_text("x = 1", "utf-8")


async def _skill_watch(tmp_path: Path, *, mode: str = "tell") -> tuple[CoreWatch, _Skills, _Announcer]:
    root = tmp_path / "skills"
    _skill_tree(root)
    skills, announcer = _Skills(root), _Announcer()
    watch = CoreWatch(
        lifecycle=Lifecycle(asyncio.Event()), settings=RestartConfig(auto="off", skills=mode),
        watched=[], skills=skills, announcer=announcer,  # type: ignore[arg-type]
    )
    await watch.start()
    return watch, skills, announcer


async def test_updated_skills_are_reloaded_and_said_in_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core import restart

    monkeypatch.setattr(restart, "skill_check", lambda candidate, root: "")
    watch, skills, announcer = await _skill_watch(tmp_path)
    assert await watch.skills_tick() == "same"

    (tmp_path / "skills" / "peace" / "skill.py").write_text("x = 2", "utf-8")
    (tmp_path / "skills" / "browser" / "page" / "skill.py").write_text("x = 2", "utf-8")
    start = time.monotonic() + 100
    assert await watch.skills_tick(now=start) == "changing"
    assert await watch.skills_tick(now=start + 5) == "settling"
    assert await watch.skills_tick(now=start + 30) == "updated"
    # Подскилл — сам по себе: главный браузер не тронут.
    assert sorted(skills.reloaded) == ["page", "peace"]
    assert announcer.said == ["{address}, на 2 модуля пришли обновления, обновил."]
    assert await watch.skills_tick(now=start + 60) == "same"


async def test_a_skill_that_does_not_import_is_left_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core import restart

    monkeypatch.setattr(restart, "skill_check", lambda candidate, root: "SyntaxError: invalid syntax")
    watch, skills, announcer = await _skill_watch(tmp_path)
    (tmp_path / "skills" / "peace" / "skill.py").write_text("x = (", "utf-8")
    start = time.monotonic() + 100
    await watch.skills_tick(now=start)
    assert await watch.skills_tick(now=start + 30) == "failed"
    assert skills.reloaded == []
    assert "peace" in announcer.said[0] and "не поднялся" in announcer.said[0]
    # Второй раз ту же правку не пробуем.
    assert await watch.skills_tick(now=start + 60) == "same"


async def test_a_manual_reload_moves_the_baseline(tmp_path: Path) -> None:
    """«Переподключи модуль» голосом — наблюдатель не повторяет и не докладывает."""
    from jarvis.core.contracts import SkillLoaded

    watch, skills, announcer = await _skill_watch(tmp_path)
    (tmp_path / "skills" / "peace" / "skill.py").write_text("x = 3", "utf-8")
    await watch._on_skill_loaded(SkillLoaded(source="skills", skill="peace"))
    assert await watch.skills_tick() == "same"


async def test_quiet_mode_reloads_without_a_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core import restart

    monkeypatch.setattr(restart, "skill_check", lambda candidate, root: "")
    watch, skills, announcer = await _skill_watch(tmp_path, mode="quiet")
    (tmp_path / "skills" / "peace" / "skill.py").write_text("x = 4", "utf-8")
    start = time.monotonic() + 100
    await watch.skills_tick(now=start)
    assert await watch.skills_tick(now=start + 30) == "updated"
    assert skills.reloaded == ["peace"] and announcer.said == []


def test_a_tool_call_is_not_the_owner_talking() -> None:
    """Наблюдатель сам зовёт «что играет», и с `tool.invoked` в признаках разговора
    сбрасывал себе отсчёт тишины: перезапуск ждал вечно (лог 29.09.2026)."""
    assert "tool.invoked" not in CoreWatch.ACTIVE


async def test_a_manual_reload_of_a_skill_with_a_subskill_is_not_repeated(tmp_path: Path) -> None:
    """Живой лог 29.09.2026: «переподключи модуль браузер» — и через полминуты
    браузер переподключился сам ещё раз: файлы `page` засчитались браузеру."""
    from jarvis.core.contracts import SkillLoaded

    watch, skills, announcer = await _skill_watch(tmp_path)
    (tmp_path / "skills" / "browser" / "skill.py").write_text("x = 5", "utf-8")
    await watch._on_skill_loaded(SkillLoaded(source="skills", skill="browser"))
    assert await watch.skills_tick() == "same"
