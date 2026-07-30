"""Поиск скиллов на диске.

Скиллы намеренно лежат **вне** пакета `jarvis` и грузятся по пути файла:
это плагины, а не часть ядра. Поддерживаются три раскладки:

    skills/telegram/skill.py         — каталог со скиллом (рекомендуется)
    skills/timer.py                  — одиночный файл
    skills/browser/page/skill.py     — подскилл внутри своего главного

**Подскилл — это часть главного, а не сосед.** Появился он не ради красоты
каталога: скилл `page` не работает без `browser` в принципе (он зовёт
`browser.page_run`), а в общем списке они выглядели как две независимые
возможности «для браузера». Вложенность говорит правду и даёт три вещи:

* порядок — главный грузится первым;
* зависимость — не загрузился главный, подскилл даже не пробуем;
* выгрузку вместе: перезагрузили главный — перезагрузились и его части.

**Уровень вложенности ровно один.** Дерево из подподскиллов ничего не
объясняет, а путаницы добавляет; нужно глубже — значит это отдельный скилл.

**Имена инструментов от вложенности не зависят.** Они берутся из паспорта
(`meta.name`), поэтому `page.pause` остаётся `page.pause`. Это не мелочь: на
такие имена ссылается выученное в памяти, а `browser.page.pause` рядом с
существующим `browser.page_run` читалось бы как одно и то же.
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
    #: Имя главного скилла, если этот лежит внутри него. Пусто — сам главный.
    parent: str = ""

    @property
    def module_name(self) -> str:
        """Имя, под которым модуль попадёт в ``sys.modules``.

        У подскилла имя составное: одинаковые короткие имена в разных главных
        скиллах — вопрос времени, а ``sys.modules`` один на весь процесс.
        """
        if self.parent:
            return f"jarvis_skills.{self.parent}.{self.name}"
        return f"jarvis_skills.{self.name}"

    @property
    def label(self) -> str:
        """Как называть в логах и отчётах: ``browser/page``."""
        return f"{self.parent}/{self.name}" if self.parent else self.name


def _entry(directory: Path) -> Path | None:
    """Файл скилла внутри каталога, если он там есть."""
    module_file = directory / _ENTRY_FILE
    return module_file if module_file.is_file() else None


def _readable(directory: Path) -> list[Path]:
    """Содержимое каталога по порядку, без служебных имён."""
    return [
        entry
        for entry in sorted(directory.iterdir())
        if not entry.name.startswith((".", "_"))
    ]


def discover(paths: Iterable[Path]) -> list[SkillCandidate]:
    """Обойти каталоги и собрать кандидатов в скиллы.

    Главные скиллы возвращаются раньше своих подскиллов: порядок загрузки
    важен, а зависимость направлена только в одну сторону.
    """
    main: dict[str, SkillCandidate] = {}
    nested: list[SkillCandidate] = []

    for directory in paths:
        if not directory.is_dir():
            logger.warning("Каталог скиллов не найден: %s", directory)
            continue

        for entry in _readable(directory):
            if entry.is_dir():
                module_file = _entry(entry)
                if module_file is None:
                    continue
                main[entry.name] = SkillCandidate(name=entry.name, path=module_file)
                # Внутри — подскиллы. Глубже не идём: см. правило в шапке.
                for inner in _readable(entry):
                    inner_file = _entry(inner) if inner.is_dir() else None
                    if inner_file is not None:
                        nested.append(
                            SkillCandidate(
                                name=inner.name, path=inner_file, parent=entry.name
                            )
                        )
                continue

            if entry.suffix == ".py":
                main[entry.stem] = SkillCandidate(name=entry.stem, path=entry)

    found = [main[name] for name in sorted(main)]
    found += sorted(nested, key=lambda item: (item.parent, item.name))
    logger.debug("Найдено кандидатов в скиллы: %d", len(found))
    return found
