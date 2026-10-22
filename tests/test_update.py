"""Обновление из git: перемотка вперёд, и ничего сверх неё (20.10.2026)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis.core.config.schema import UpdateConfig
from jarvis.core.update import (
    DIVERGED,
    FAILED,
    NO_GIT,
    OTHER_BRANCH,
    SAME,
    UPDATED,
    GitUpdate,
    Pulled,
    pull,
    updated_speech,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"правка {name}")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """Удалённый репозиторий, машина владельца (клон) и место, откуда пушат."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    author = tmp_path / "author"
    _git(tmp_path, "clone", "-q", str(origin), str(author))
    _commit(author, "jarvis/core.py", "a = 1\n")
    _git(author, "push", "-q", "origin", "HEAD:main")
    machine = tmp_path / "machine"
    _git(tmp_path, "clone", "-q", str(origin), str(machine))
    return author, machine


def test_nothing_new_is_same(repos: tuple[Path, Path]) -> None:
    _, machine = repos
    assert pull(machine).status == SAME


def test_a_push_arrives_by_fast_forward(repos: tuple[Path, Path]) -> None:
    author, machine = repos
    _commit(author, "skills/rates/skill.py", "x = 1\n")
    _commit(author, "jarvis/core.py", "a = 2\n")
    _git(author, "push", "-q", "origin", "HEAD:main")

    pulled = pull(machine)

    assert pulled.status == UPDATED
    assert pulled.commits == 2
    assert set(pulled.files) == {"skills/rates/skill.py", "jarvis/core.py"}
    assert pulled.core and not pulled.dependencies
    assert (machine / "jarvis/core.py").read_text("utf-8") == "a = 2\n"
    assert pull(machine).status == SAME


def test_only_a_skill_is_not_core(repos: tuple[Path, Path]) -> None:
    author, machine = repos
    _commit(author, "skills/rates/skill.py", "x = 1\n")
    _commit(author, "pyproject.toml", "[project]\n")
    _git(author, "push", "-q", "origin", "HEAD:main")

    pulled = pull(machine)

    assert pulled.status == UPDATED
    assert not pulled.core
    assert pulled.dependencies


def test_own_commits_on_the_machine_are_left_alone(repos: tuple[Path, Path]) -> None:
    author, machine = repos
    _commit(author, "jarvis/core.py", "a = 2\n")
    _git(author, "push", "-q", "origin", "HEAD:main")
    _commit(machine, "notes.txt", "моё\n")
    head = _git(machine, "rev-parse", "HEAD")

    pulled = pull(machine)

    assert pulled.status == DIVERGED
    assert _git(machine, "rev-parse", "HEAD") == head


def test_a_local_edit_the_merge_would_overwrite_stops_it(repos: tuple[Path, Path]) -> None:
    author, machine = repos
    _commit(author, "jarvis/core.py", "a = 2\n")
    _git(author, "push", "-q", "origin", "HEAD:main")
    (machine / "jarvis/core.py").write_text("a = 99  # правлю руками\n", "utf-8")

    pulled = pull(machine)

    assert pulled.status == FAILED
    assert (machine / "jarvis/core.py").read_text("utf-8") == "a = 99  # правлю руками\n"


def test_an_unrelated_local_edit_does_not_block(repos: tuple[Path, Path]) -> None:
    author, machine = repos
    _commit(author, "skills/rates/skill.py", "x = 1\n")
    _git(author, "push", "-q", "origin", "HEAD:main")
    (machine / "jarvis/core.py").write_text("a = 99\n", "utf-8")

    assert pull(machine).status == UPDATED
    assert (machine / "jarvis/core.py").read_text("utf-8") == "a = 99\n"


def test_another_branch_is_not_touched(repos: tuple[Path, Path]) -> None:
    _, machine = repos
    _git(machine, "checkout", "-q", "-b", "fix/voice")
    assert pull(machine).status == OTHER_BRANCH


def test_not_a_clone(tmp_path: Path) -> None:
    assert pull(tmp_path).status == NO_GIT


def test_missing_git_is_not_a_crash(repos: tuple[Path, Path]) -> None:
    _, machine = repos
    assert pull(machine, git="no-such-git-binary").status == NO_GIT


def test_git_env_with_and_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.update import _git_env

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:8888")
    monkeypatch.setenv("NO_PROXY", "localhost")

    plain = _git_env(proxy=False)
    assert "HTTPS_PROXY" not in plain and "http_proxy" not in plain
    assert plain["NO_PROXY"] == "localhost" and plain["GIT_TERMINAL_PROMPT"] == "0"
    assert _git_env(proxy=True)["HTTPS_PROXY"] == "http://127.0.0.1:8888"


def test_dead_proxy_falls_back_to_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Туннель лёг — fetch идёт напрямую (20.10.2026 обновление легло вместе с ним)."""
    from jarvis.core import update

    calls: list[bool] = []

    def git(root: Path, binary: str, *args: str, proxy: bool = True, timeout: float = 0) -> str:
        calls.append(proxy)
        if proxy:
            raise update.GitError("git fetch: Failed to connect via 100.98.141.99")
        return ""

    monkeypatch.setattr(update, "_git", git)
    monkeypatch.setenv("HTTPS_PROXY", "http://100.98.141.99:8888")
    update._fetch(tmp_path, "git")
    assert calls == [True, False]


def test_both_ways_failing_names_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jarvis.core import update

    def git(root: Path, binary: str, *args: str, proxy: bool = True, timeout: float = 0) -> str:
        raise update.GitError("через прокси" if proxy else "сброшено")

    monkeypatch.setattr(update, "_git", git)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    with pytest.raises(update.GitError, match="через прокси; напрямую — сброшено"):
        update._fetch(tmp_path, "git")


def test_speech_counts_commits() -> None:
    assert updated_speech(Pulled(UPDATED, commits=3)) == "Скачал 3 коммита."
    assert updated_speech(Pulled(UPDATED, commits=1)) == "Скачал 1 коммит."


# --- голосом ---------------------------------------------------------------------


class _Restart:
    def __init__(self, answer: str = "") -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def __call__(self, reason: str) -> str:
        self.calls.append(reason)
        return self.answer


def _updater(tmp_path: Path, pulled: Pulled, restart: _Restart) -> GitUpdate:
    return GitUpdate(settings=UpdateConfig(), root=tmp_path, restart=restart, pull=lambda: pulled)


async def test_voice_update_with_core_restarts_at_once(tmp_path: Path) -> None:
    restart = _Restart()
    updater = _updater(tmp_path, Pulled(UPDATED, commits=2, files=("jarvis/app.py",)), restart)

    result = await updater.update()

    assert result.ok and restart.calls
    assert "Перезапускаюсь" in result.speech_for("ru")


async def test_voice_update_with_skills_only_reloads_them_at_once(tmp_path: Path) -> None:
    restart = _Restart()
    reloads: list[str] = []

    async def reload_skills() -> tuple[list[str], list[str]]:
        reloads.append("now")
        return ["rates"], []

    updater = GitUpdate(
        settings=UpdateConfig(), root=tmp_path, restart=restart, reload_skills=reload_skills,
        pull=lambda: Pulled(UPDATED, commits=1, files=("skills/rates/skill.py",)),
    )

    result = await updater.update()

    assert result.ok and not restart.calls and reloads == ["now"]
    assert result.speech_for("ru") == "Скачал 1 коммит. Обновил модуль: rates."


def test_modules_speech() -> None:
    from jarvis.core.update import modules_speech

    assert modules_speech(["page", "relay"], []) == "Обновил модули: page, relay."
    assert modules_speech([], ["browser/page"]) == "Модуль page не поднялся, подробности в логе."
    assert modules_speech([], []) == "Модули не менялись."


async def test_voice_update_with_nothing_new(tmp_path: Path) -> None:
    result = await _updater(tmp_path, Pulled(SAME), _Restart()).update()
    assert result.ok and "Обновлений нет" in result.speech_for("ru")


async def test_voice_update_refused_says_why(tmp_path: Path) -> None:
    result = await _updater(tmp_path, Pulled(DIVERGED, reason="своих коммитов: 1"), _Restart()).update()
    assert not result.ok and "свои коммиты" in result.speech_for("ru")
