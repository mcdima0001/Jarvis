"""Какая версия кода сейчас работает.

Нужно ровно для одного: читая лог, знать, к какому коду он относится. Логи с
машины владельца разбираются на сервере, иногда через день-другой, и вопрос
«а это уже с исправлением или ещё до него» до сих пор решался гаданием по
поведению. Номер версии на такой вопрос не отвечает — он меняется редко;
отвечает **коммит**.

Читается коммит из каталога `.git` напрямую, без запуска `git`: у владельца
Jarvis стоит из клона, но git в PATH может и не быть, а поднимать процесс на
каждом старте ради одной строки — расточительство. Нет `.git` (копия, архив,
установка пакетом) — просто нет коммита, и это не ошибка.
"""

from __future__ import annotations

import logging
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis import __version__

logger = logging.getLogger(__name__)

#: Сколько знаков коммита показывать. Семи хватает, чтобы `git show` его нашёл.
SHORT = 7


@dataclass(frozen=True, slots=True)
class Build:
    """Что именно сейчас запущено."""

    version: str
    #: Короткий хеш коммита; пусто, если каталога `.git` рядом нет.
    commit: str = ""
    #: Ветка; пусто при отсоединённой голове или без `.git`.
    branch: str = ""

    @property
    def label(self) -> str:
        """Одной строкой: ``0.1.0 (a1b2c3d, main)``."""
        inside = ", ".join(part for part in (self.commit, self.branch) if part)
        return f"{self.version} ({inside})" if inside else self.version


def _read(path: Path) -> str:
    """Прочитать строку из файла; чего нет, то и не мешает."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def git_head(root: Path) -> tuple[str, str]:
    """Достать коммит и ветку из каталога `.git`.

    Разбирается ровно то, что нужно, и ничего больше:

    * ``ref: refs/heads/main`` в `HEAD` — обычное состояние, коммит лежит в
      файле ссылки либо в `packed-refs` (свежий клон ссылок на диске не хранит);
    * голый хеш в `HEAD` — отсоединённая голова, ветки нет.

    :return: пара «коммит» и «ветка»; пустые строки, если репозитория нет.
    """
    head = _read(root / ".git" / "HEAD")
    if not head:
        return "", ""

    if not head.startswith("ref:"):
        return head[:SHORT], ""

    ref = head.split(":", 1)[1].strip()
    branch = ref.rsplit("/", 1)[-1]

    commit = _read(root / ".git" / ref)
    if not commit:
        # Клон без распакованных ссылок: они лежат одним файлом.
        for line in _read(root / ".git" / "packed-refs").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                commit = parts[0]
                break
    return commit[:SHORT], branch


def current(root: Path | None = None) -> Build:
    """Собрать сведения о запущенном коде.

    :param root: корень проекта; по умолчанию — каталог над пакетом.
    """
    base = root or Path(__file__).resolve().parents[2]
    commit, branch = git_head(base)
    return Build(version=__version__, commit=commit, branch=branch)


def platform_line() -> str:
    """Строка про машину: система и версия Python.

    Половина разборов упирается в «а на чём это было»: у владельца Windows и
    свой Python, у сервера — Linux и venv, и поведение у них расходится.
    """
    return f"Python {platform.python_version()}, {platform.system()} {platform.release()}"


def describe(root: Path | None = None, *, skills: dict[str, str] | None = None) -> str:
    """Полная строка для лога и для `--check`."""
    parts = [f"Jarvis {current(root).label}", platform_line()]
    if skills:
        parts.append("скиллы: " + ", ".join(f"{name} {ver}" for name, ver in sorted(skills.items())))
    return "; ".join(parts)


def executable_line() -> str:
    """Каким интерпретатором запущено.

    У владельца их два — системный `C:\\Python314` и venv проекта, — и
    установленные пакеты у них разные. «Не работает синтез» не раз оказывалось
    «запущено не тем питоном».
    """
    return sys.executable
