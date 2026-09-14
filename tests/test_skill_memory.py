"""Автопамять: из журнала в профиль попадают факты, а не обрывки фраз.

Разбор владельца 14.09.2026: первая версия записала бы «я из дома» городом
«дома», а «я люблю тебя» — предпочтением «тебя».
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "memory" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_memory_extract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


memory = _load()


@pytest.mark.parametrize(
    "text",
    [
        "я из дома, скоро буду",
        "я люблю тебя",
        "мне нравится это",
        "я работаю над проектом Jarvis",
        "я работаю сегодня до шести",
        "мой день рождения скоро",
        "меня зовут как дедушку",
    ],
)
def test_phrases_are_not_facts(text: str) -> None:
    assert memory.MemorySkill._extract([text]) == {}


def test_real_facts_are_kept() -> None:
    found = memory.MemorySkill._extract([
        "Меня зовут Дима",
        "я живу в Нижний Новгород уже пять лет",
        "я работаю программистом",
        "мой день рождения 12 марта",
        "я не люблю громкую рекламу в роликах и всплывающие окна",
        "мне нравится Queen",
    ])
    assert found == {
        "name": "Дима",
        "city": "Нижний Новгород",
        "job": "программистом",
        "birthday": "12 марта",
        "dislikes": "громкую рекламу в роликах",
        "likes": "Queen",
    }


def test_company_counts_as_job() -> None:
    assert memory.MemorySkill._extract(["я работаю в Яндексе"]) == {"job": "в Яндексе"}
