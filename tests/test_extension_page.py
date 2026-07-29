"""Интерпретатор шагов внутри страницы — через самопроверку на node.

Код, который выполняется в странице, написан на JavaScript, и на сервере его
проверить нечем, кроме node. Поэтому проверка живёт рядом с самим кодом
(`extension/selftest.js`), а отсюда только запускается — чтобы обычный `pytest`
её не пропускал. Нет node — тест пропускается: это не повод валить сборку.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SELFTEST = Path(__file__).resolve().parent.parent / "extension" / "selftest.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node — JS не проверить")
def test_page_interpreter() -> None:
    """Шаги выбирают нужный элемент: плеер, кнопку по подписи, селектор."""
    done = subprocess.run(
        ["node", str(_SELFTEST)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stdout + done.stderr
