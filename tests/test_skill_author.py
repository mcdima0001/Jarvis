"""Скилл, который пишет ассистенту новые умения.

Проверяется не качество написанного — его проверяет человек, — а то, что чужой
текст не может навредить по дороге. Ответ приходит из облака и превращается в
**путь на диске** и в **код, который потом будет исполняться**. Оба перехода
здесь и закрыты.

Сеть не трогаем: запрос к панели проверяется живьём, а тут важна обвязка вокруг
него.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Загрузить скилл как модуль: он плагин и лежит вне пакета."""
    path = _ROOT / "skills" / "author" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_author", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


author = _load()

_GOOD = '''"""Скилл про диск."""

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import Skill, SkillMeta
from jarvis.core.tools import tool


class DiskSkill(Skill):
    """Сколько места."""

    meta = SkillMeta(name="disk", description="Место на диске")

    @tool(phrases=["сколько места"], reversible=True)
    async def free(self) -> ToolResult:
        """Свободное место."""
        return ToolResult.success(1, speech="Много.")
'''


# --- имя превращается в путь, поэтому проверяется белым списком -------------


def test_normal_names_pass() -> None:
    """Обычное имя скилла проходит и приводится к строчным."""
    assert author.safe_name("disk") == "disk"
    assert author.safe_name("  Disk  ") == "disk"
    assert author.safe_name("my_skill2") == "my_skill2"


def test_names_that_would_escape_the_folder_are_refused() -> None:
    """Имя приходит из текста, написанного моделью, и становится каталогом.

    Чёрный список тут негоден в принципе: перечислить все способы выйти из
    каталога нельзя. Поэтому белый список, и всё остальное — отказ.
    """
    for bad in ("../evil", "..", "/etc/passwd", "a/b", "disk.py", "диск", "", "1disk"):
        assert author.safe_name(bad) == "", bad


def test_draft_path_always_stays_inside_drafts(tmp_path: Path) -> None:
    """Черновик ложится только в `drafts/`, и это проверяется путём, а не верой."""
    path = author.draft_path(tmp_path, "disk")
    assert path == tmp_path / "drafts" / "disk" / "skill.py"
    assert path.resolve().is_relative_to((tmp_path / "drafts").resolve())


def test_bad_name_never_becomes_a_path(tmp_path: Path) -> None:
    """На негодном имени путь не строится вовсе."""
    with pytest.raises(ValueError):
        author.draft_path(tmp_path, "../../etc")


def test_drafts_are_not_skills() -> None:
    """Каталог черновиков намеренно не тот, из которого грузятся скиллы.

    Иначе написанное подключилось бы само при ближайшем перезапуске, и вся
    затея с одобрением человеком потеряла бы смысл.
    """
    assert author.DRAFTS != "skills"


# --- ответ модели превращается в код ----------------------------------------


def test_fenced_answer_is_unwrapped() -> None:
    """Обратные кавычки вокруг кода снимаются.

    Просьбу их не ставить модель слышит не всегда, а лишние кавычки превращают
    файл в синтаксическую ошибку. Дешевле снять, чем полагаться на послушание.
    """
    assert author.extract_code("```python\nx = 1\n```") == "x = 1\n"
    assert author.extract_code("```\nx = 1\n```") == "x = 1\n"


def test_plain_answer_survives_untouched() -> None:
    """Без кавычек код не портится."""
    assert author.extract_code("x = 1") == "x = 1\n"


def test_skill_finds_its_own_name() -> None:
    """Имя каталога берётся из паспорта самого скилла."""
    assert author.skill_name(_GOOD) == "disk"
    assert author.skill_name("нет тут имени") == ""


# --- отказ от того, что скиллом не является ---------------------------------


def test_real_skill_is_accepted() -> None:
    """Нормально написанный скилл проходит проверку."""
    assert author.looks_like_skill(_GOOD) == ""


def test_polite_refusal_is_not_a_skill() -> None:
    """Модель иногда отвечает извинением вместо файла.

    Без этой проверки в черновиках оказался бы вежливый текст, и понял бы это
    владелец, только открыв файл.
    """
    assert author.looks_like_skill("Извините, я не могу этого сделать.")
    assert author.looks_like_skill("")


def test_code_without_tools_is_refused() -> None:
    """Скилл без единого инструмента бесполезен: звать в нём нечего."""
    code = _GOOD.replace('@tool(phrases=["сколько места"], reversible=True)', "")
    assert author.looks_like_skill(code) == "в ответе нет ни одного инструмента"


def test_nameless_skill_is_refused() -> None:
    """Без имени в паспорте некуда класть: имя — это каталог."""
    code = _GOOD.replace('name="disk"', 'description="без имени"')
    assert author.looks_like_skill(code) == "скилл не назвал себя в meta"


# --- доклад -----------------------------------------------------------------


def test_report_tells_where_it_is_and_what_to_say(tmp_path: Path) -> None:
    """В докладе есть и путь для чтения, и команда для одобрения.

    Доклад звучит вслух один раз, и если из него непонятно, что делать дальше,
    то вся работа встанет: человек не вспомнит имя черновика.
    """
    said = author.report("disk", author.draft_path(tmp_path, "disk"), 2)
    assert "disk" in said
    assert "прими скилл disk" in said
    assert "drafts/disk/skill.py" in said


def test_prompt_carries_the_conventions() -> None:
    """Агент работает в пустом каталоге и нашего кода не видит.

    Поэтому соглашения едут в запросе целиком: угадывать их он не должен, а
    без них напишет скилл, не похожий на остальные.
    """
    prompt = author.build_prompt("выключать монитор")
    assert "выключать монитор" in prompt
    assert "asyncio.to_thread" in prompt
    assert "reversible" in prompt
    assert "SkillMeta" in prompt


# --- проверка написанного ---------------------------------------------------


def test_report_puts_failed_checks_first(tmp_path: Path) -> None:
    """Про непройденные проверки говорится сразу.

    Доклад звучит один раз, и «готово» про код с ошибками — худший вид вранья:
    он выглядит как успех.
    """
    path = author.draft_path(tmp_path, "disk")
    assert "проверки прошёл" in author.report("disk", path, 1)
    assert "не прошёл" in author.report("disk", path, 1, findings="типы: беда")


def test_repair_prompt_carries_code_and_findings() -> None:
    """На исправление уходит и сам код, и что именно в нём нашли.

    Без кода агент напишет файл заново и, скорее всего, с той же ошибкой;
    без находок — не поймёт, что чинить.
    """
    prompt = author.repair_prompt(_GOOD, 'типы: нет атрибута "unhealthy"')
    assert "class DiskSkill" in prompt
    assert "unhealthy" in prompt
    assert "Имя в meta не меняй" in prompt
