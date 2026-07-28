"""Импорт модуля скилла по пути файла и поиск в нём класса скилла."""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from types import ModuleType

from jarvis.core.errors import SkillLoadError

from .base import Skill
from .discovery import SkillCandidate

logger = logging.getLogger(__name__)


def import_module(candidate: SkillCandidate, *, reload: bool = False) -> ModuleType:
    """Импортировать модуль скилла по пути к файлу.

    Исходник читается и компилируется вручную, а не через штатный
    `spec.loader.exec_module`. Причина в кеше байткода: заголовок ``.pyc``
    хранит время правки с точностью до секунды, поэтому файл, переписанный в ту
    же секунду и не изменивший размер, считается неизменным — и перезагрузка
    молча поднимает старый код. Для плагинов, которые правят и тут же
    перезагружают, это неприемлемо.

    :param candidate: найденный кандидат.
    :param reload: выбросить прежнюю версию модуля из кеша перед импортом.
    """
    if reload:
        sys.modules.pop(candidate.module_name, None)
        importlib.invalidate_caches()

    spec = importlib.util.spec_from_file_location(candidate.module_name, candidate.path)
    if spec is None:
        raise SkillLoadError(f"Не удалось подготовить импорт {candidate.path}")

    try:
        source = candidate.path.read_text(encoding="utf-8")
        code = compile(source, str(candidate.path), "exec")
    except (OSError, SyntaxError) as exc:
        raise SkillLoadError(f"Не удалось прочитать {candidate.path}: {exc}") from exc

    module = importlib.util.module_from_spec(spec)
    # Модуль кладётся в sys.modules до выполнения: иначе dataclass и
    # get_type_hints внутри скилла не найдут собственный модуль.
    sys.modules[candidate.module_name] = module
    try:
        exec(code, module.__dict__)
    except Exception as exc:
        sys.modules.pop(candidate.module_name, None)
        raise SkillLoadError(f"Ошибка импорта {candidate.path}: {exc}") from exc
    return module


def find_skill_class(module: ModuleType, *, path: Path) -> type[Skill]:
    """Найти в модуле единственный класс-наследник `Skill`."""
    classes = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Skill) and obj is not Skill and obj.__module__ == module.__name__
    ]
    if not classes:
        raise SkillLoadError(f"В {path} нет класса, унаследованного от Skill")
    if len(classes) > 1:
        names = ", ".join(cls.__name__ for cls in classes)
        raise SkillLoadError(f"В {path} несколько классов Skill ({names}) — оставь один")

    skill_class = classes[0]
    if not hasattr(skill_class, "meta"):
        raise SkillLoadError(f"{skill_class.__name__} в {path} не объявил meta = SkillMeta(...)")
    return skill_class
