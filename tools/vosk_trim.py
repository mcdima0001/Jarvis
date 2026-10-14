"""Модель имени без языковой модели: −60 МБ памяти на сервере.

    python tools/vosk_trim.py models/wakeword/vosk-model-small-ru-0.22 models/wakeword/vosk-name-only

Детектор имени слушает со словарём из «джарвис» и слов-соперников, и граф для
такого словаря Vosk собирает сам — большая языковая модель (`graph/Gr.fst`)
ему не нужна, но грузится всё равно: 185 МБ памяти вместо 124 (замер
14.10.2026 на сервере). Выбросить файл просто так нельзя — из него же Vosk
берёт таблицу слов. Поэтому таблица вынимается в `graph/words.txt` (так Vosk
читает её, когда `Gr.fst` нет), а копия модели собирается без него.

Проверено на 92 обращениях из `bench/wake/`: имя появляется на том же кадре,
что и с полной моделью, расхождений ноль. Vosk при загрузке пишет в stderr
«Can't open file … Gr.fst» — это он ищет файл, которого нет намеренно.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path


def symbols(fst: bytes) -> list[tuple[str, int]]:
    """Таблица выходных слов из заголовка OpenFST (бинарный формат)."""
    pos = 0

    def i32() -> int:
        nonlocal pos
        (value,) = struct.unpack_from("<i", fst, pos)
        pos += 4
        return int(value)

    def i64() -> int:
        nonlocal pos
        (value,) = struct.unpack_from("<q", fst, pos)
        pos += 8
        return int(value)

    def text() -> str:
        nonlocal pos
        size = i32()
        value = fst[pos : pos + size].decode("utf-8")
        pos += size
        return value

    i32()  # магия
    text()  # тип графа
    text()  # тип дуг
    i32()  # версия
    flags = i32()
    pos += 8  # свойства
    i64(), i64(), i64()  # начало, состояний, дуг

    def table() -> list[tuple[str, int]]:
        i32()  # магия таблицы
        text()  # имя
        i64()  # следующий свободный ключ
        return [(text(), i64()) for _ in range(i64())]

    tables = []
    if flags & 1:
        tables.append(table())
    if flags & 2:
        tables.append(table())
    if not tables:
        raise ValueError("в графе нет таблицы слов")
    return tables[-1]


def trim(source: Path, target: Path) -> int:
    """Скопировать модель без `Gr.fst`, положив таблицу слов рядом. Возвращает число слов."""
    words = symbols((source / "graph" / "Gr.fst").read_bytes())
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("Gr.fst"))
    with open(target / "graph" / "words.txt", "w", encoding="utf-8") as file:
        for word, key in words:
            file.write(f"{word} {key}\n")
    return len(words)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    count = trim(args.source, args.target)
    print(f"Готово: {args.target}, слов {count}. В конфиге: audio.wake_word.model: {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
