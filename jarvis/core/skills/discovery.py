"""Поиск скиллов на диске.

Скиллы намеренно лежат **вне** пакета `jarvis` и грузятся по пути файла:
это плагины, а не часть ядра. Поддерживаются две раскладки:

    skills/esp32/skill.py     — каталог со скиллом (рекомендуется)
    skills/timer.py           — одиночный файл
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_ENTRY_FILE = "skill.py"


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillCandidate:
    """Найденный на диске модуль, который может оказаться скиллом."""

    name: str
    path: Path

    @property
    def module_name(self) -> str:
        """Имя, под которым модуль попадёт в ``sys.modules``."""
        return f"jarvis_skills.{self.name}"


def discover(paths: Iterable[Path]) -> list[SkillCandidate]:
    """Обойти каталоги и собрать кандидатов в скиллы."""
    found: dict[str, SkillCandidate] = {}

    for directory in paths:
        if not directory.is_dir():
            logger.warning("Каталог скиллов не найден: %s", directory)
            continue

        for entry in sorted(directory.iterdir()):
            if entry.name.startswith((".", "_")):
                continue

            if entry.is_dir():
                module_file = entry / _ENTRY_FILE
                if module_file.is_file():
                    found[entry.name] = SkillCandidate(name=entry.name, path=module_file)
                continue

            if entry.suffix == ".py":
                found[entry.stem] = SkillCandidate(name=entry.stem, path=entry)

    logger.debug("Найдено кандидатов в скиллы: %d", len(found))
    return [found[name] for name in sorted(found)]
