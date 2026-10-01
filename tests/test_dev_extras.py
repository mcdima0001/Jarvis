"""Набор `dev` — всё, что нужно тестам: «поставил dev — тесты идут».

CI ставит только базовые зависимости и `dev`. С 14.09.2026 тест буфера обмена
импортировал Pillow, которого в `dev` не было, и CI семнадцать дней не выполнял ни
одного теста: сбор прерывался на первом же файле (аудит 01.10.2026). Локально этого
не видно — у владельца стоит всё, — поэтому сверка идёт по исходникам тестов, а не
по тому, что установлено.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

#: Имя при импорте → имя пакета там, где они расходятся.
_DISTRIBUTIONS = {"PIL": "pillow", "yaml": "pyyaml"}
#: Своё: пакеты и папки проекта, которые тесты зовут по имени.
_OWN = {"jarvis", "skills", "tests", "launcher", "tools"}
#: Тяжёлое и необязательное: пропускается внутри теста, в `dev` не входит намеренно.
_HEAVY = {"faster_whisper"}


def _package(spec: str) -> str:
    """Имя пакета из строки зависимости: `pillow>=10.0` → `pillow`."""
    return re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].strip().lower().replace("_", "-")


def _imported_by_tests() -> dict[str, str]:
    """Верхнее имя модуля → первый файл тестов, который его импортирует.

    Считается и `pytest.importorskip("...")`: пропуск наверху файла убирает из
    прогона все его тесты разом, а в отчёте от них остаётся одна буква «s».
    """
    found: dict[str, str] = {}
    for source in sorted((_ROOT / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "importorskip"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names = [node.args[0].value]
            for name in names:
                found.setdefault(name.split(".")[0], source.name)
    return found


def test_every_package_the_tests_import_is_in_dev() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    provided = {_package(spec) for spec in [*project["dependencies"], *project["optional-dependencies"]["dev"]]}
    imported = _imported_by_tests()
    assert "pytest" in imported, "не нашёл импортов в тестах — проверка была бы пустой"

    missing = sorted(
        f"{_DISTRIBUTIONS.get(name, name)} ({where})"
        for name, where in imported.items()
        if name not in sys.stdlib_module_names
        and name not in _OWN
        and name not in _HEAVY
        and _package(_DISTRIBUTIONS.get(name, name)) not in provided
    )
    assert not missing, f"тесты импортируют то, чего нет в наборе dev: {missing}"


# --- пропуск модуля целиком -------------------------------------------------------------------

#: Хук из conftest.py проекта, подключённый в отдельный прогон pytest.
_HOOK = "from tests.conftest import pytest_make_collect_report  # noqa: F401\n"
#: Отдельному прогону асинхронность не нужна, а pytest-asyncio без своих
#: настроек сыплет предупреждениями в общий отчёт.
_PLAIN = ("-p", "no:asyncio")


def test_a_module_skipped_whole_is_a_collection_error(pytester: pytest.Pytester) -> None:
    """Без Pillow 98 тестов «где снято» выпадали одной буквой «s», и прогон был зелёным."""
    pytester.makeconftest(_HOOK)
    pytester.makepyfile(
        test_whole="import pytest\n\npytest.importorskip('no_such_package_jarvis')\n\n\ndef test_a():\n    pass\n"
    )
    result = pytester.runpytest(*_PLAIN)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["*модуль пропущен целиком*no_such_package_jarvis*"])


def test_a_single_test_still_skips_itself(pytester: pytest.Pytester) -> None:
    """Тяжёлое и необязательное (faster-whisper) пропускается внутри теста — это видно поштучно."""
    pytester.makeconftest(_HOOK)
    pytester.makepyfile(
        test_one="import pytest\n\n\ndef test_a():\n    pytest.importorskip('no_such_package_jarvis')\n"
    )
    result = pytester.runpytest(*_PLAIN)
    result.assert_outcomes(skipped=1)
