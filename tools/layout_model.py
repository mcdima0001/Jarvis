"""Собрать модель «слово набрано не в той раскладке» для скилла keys.

Модель — логарифмы вероятностей пар букв (с краями слова) для русского и
английского. Русские слова берутся из документации проекта, английские — из
докстрингов стандартной библиотеки Python: оба корпуса лежат на машине и сети
не требуют.

Замер 15.09.2026 на отложенной половине слов (от четырёх букв, порог 1.0):
русское в английской раскладке ловится в 96.0%, ложно на английском — 0.21%;
английское в русской — 92.1%, ложно на русском — 0.00%.

    python tools/layout_model.py      # перезаписывает skills/keys/layout_model.json
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "skills" / "keys" / "layout_model.json"
DOCS = ("docs/lessons.md", "CLAUDE.md", "ARCHITECTURE.md", "docs/wakeword.md")


def russian_words() -> list[str]:
    text = "".join(
        (ROOT / name).read_text(encoding="utf-8").lower() for name in DOCS if (ROOT / name).exists()
    )
    return re.findall(r"[а-яё]{3,}", text)


def english_words() -> list[str]:
    library = pathlib.Path(sys.base_prefix) / "Lib"
    if not library.exists():
        library = pathlib.Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    words: list[str] = []
    for path in sorted(library.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    words += re.findall(r"[a-z]{3,}", doc.lower())
    return words


def table(words: list[str], alphabet_size: int) -> dict[str, object]:
    """Логарифмы вероятностей пар и значение для невиданной пары по первой букве."""
    pairs: Counter[str] = Counter()
    firsts: Counter[str] = Counter()
    for word in words:
        padded = f"^{word}$"
        for a, b in zip(padded, padded[1:], strict=False):
            pairs[a + b] += 1
            firsts[a] += 1
    size = alphabet_size + 2
    return {
        "pairs": {pair: round(math.log((count + 1) / (firsts[pair[0]] + size)), 3) for pair, count in sorted(pairs.items())},
        "unseen": {char: round(math.log(1 / (count + size)), 3) for char, count in sorted(firsts.items())},
        "floor": round(math.log(1 / size), 3),
    }


def main() -> None:
    ru, en = russian_words(), english_words()
    model = {"ru": table(ru, 33), "en": table(en, 26), "words": {"ru": len(ru), "en": len(en)}}
    TARGET.write_text(json.dumps(model, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{TARGET.relative_to(ROOT)}: слов русских {len(ru)}, английских {len(en)}, {TARGET.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
