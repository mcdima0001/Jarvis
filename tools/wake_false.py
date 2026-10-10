"""Ложные срабатывания имени: какое слово разбудило и помогут ли новые соперники.

    python tools/wake_false.py                       # виновники по записям с ноута
    python tools/wake_false.py --try жалко,джарвиса   # замер с этими словами в соперниках
    python tools/wake_false.py --synth               # плюс старые случаи из логов, синтезом

Записи кладёт на ноуте `audio/false_wake.py` (`audio.wake_word.false_log`), сюда их
привозит `/root/bin/jarvis-logs` вместе с логами — в `/root/jarvis-logs/wake/`.

**Виновник ищется по звуку, а не по тексту.** Запись прогоняется дважды: детектором
с той же грамматикой, что на ноуте (имя, соперники, `[unk]`) — так находится
момент срабатывания; и полным словарём модели с разметкой слов — так находится,
что на самом деле звучало в этот момент. Слово, на которое пришлось срабатывание,
и есть кандидат в соперники.

**В соперники идёт только прошедшее замер** (`--try`): ложных на записях должно
стать меньше, а обращения из `tools/wake_bench.py` — ловиться все до одного.
Кандидат не из словаря модели не годится вовсе: декодер молча выбросит его из
грамматики, и он не изменит ничего.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.core.audio.wakeword import RIVALS, UNKNOWN  # noqa: E402

RATE = 16000
FRAME = RATE * 30 // 1000 * 2
WAKE = Path("/root/jarvis-logs/wake")
LOGS = Path("/root/jarvis-logs")


@dataclass
class Case:
    """Одно ложное срабатывание: звук и что о нём известно."""

    pcm: bytes
    text: str
    reason: str
    source: str


def load_recorded(directory: Path) -> list[Case]:
    """Записи с ноута: строки журнала плюс звук рядом."""
    journal = directory / "false.jsonl"
    if not journal.exists():
        return []
    cases = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        audio = str(entry.get("audio") or "")
        path = directory / audio
        if not audio or not path.exists():
            continue
        with wave.open(str(path)) as file:
            pcm = file.readframes(file.getnframes())
        cases.append(Case(pcm, str(entry.get("text") or ""), str(entry.get("reason") or ""), audio))
    return cases


def logged_false(logs: Path) -> list[str]:
    """Фразы из логов, на которых детектор сработал, а имени в расшифровке не было."""
    import re

    heard = re.compile(r"Услышал имя \(детектор")
    said = re.compile(r"Распознано \(.*?\): '(.*)'")
    found: list[str] = []
    for path in sorted(logs.glob("jarvis-*.log")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not heard.search(line):
                continue
            for after in lines[index + 1 : index + 60]:
                if heard.search(after):
                    break
                match = said.search(after)
                if match:
                    text = match.group(1)
                    window = "\n".join(lines[index : index + 60])
                    if "Обращения по имени нет" in window and not re.search(r"(?i)джарв|jarv", text):
                        found.append(text)
                    break
    return list(dict.fromkeys(found))


class Grammar:
    """Детектор с заданным словарём — так же, как на ноуте, кадр за кадром."""

    def __init__(self, model: Any, words: list[str], hold_ms: float) -> None:
        from vosk import KaldiRecognizer

        grammar = json.dumps(words, ensure_ascii=False)
        self._make = lambda: KaldiRecognizer(model, float(RATE), grammar)
        self._hold = hold_ms

    def fires(self, pcm: bytes) -> list[float]:
        """Моменты срабатываний, секунды от начала."""
        recognizer, held, moments = self._make(), 0.0, []
        for start in range(0, len(pcm) - FRAME + 1, FRAME):
            recognizer.AcceptWaveform(pcm[start : start + FRAME])
            partial = json.loads(recognizer.PartialResult()).get("partial", "")
            if "джарвис" in partial.split():
                held += 30
                if held >= self._hold:
                    moments.append((start + FRAME) / 2 / RATE)
                    recognizer, held = self._make(), 0.0
            else:
                held = 0.0
        return moments


def words_of(model: Any, pcm: bytes) -> list[tuple[str, float, float]]:
    """Что звучало на самом деле: слова полного словаря с началом и концом."""
    from vosk import KaldiRecognizer

    recognizer = KaldiRecognizer(model, float(RATE))
    recognizer.SetWords(True)
    found: list[tuple[str, float, float]] = []
    for start in range(0, len(pcm), FRAME * 10):
        if recognizer.AcceptWaveform(pcm[start : start + FRAME * 10]):
            found += [(w["word"], w["start"], w["end"]) for w in json.loads(recognizer.Result()).get("result", [])]
    found += [(w["word"], w["start"], w["end"]) for w in json.loads(recognizer.FinalResult()).get("result", [])]
    return found


def culprit(words: list[tuple[str, float, float]], moment: float, hold_s: float) -> str:
    """Слово, на которое пришлось срабатывание.

    Детектор срабатывает, когда имя продержалось `hold_ms`, то есть ближе к концу
    слова: ищем слово, звучавшее за выдержку до срабатывания.
    """
    probe = moment - hold_s
    for word, start, end in words:
        if start - 0.1 <= probe <= end + 0.1:
            return word
    before = [item for item in words if item[2] <= moment]
    return before[-1][0] if before else ""


def in_vocabulary(model: Any, word: str) -> bool:
    """Знает ли модель слово: иначе в грамматике оно молча пропадёт."""
    return bool(model.vosk_model_find_word(word) >= 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wake", default=str(WAKE), help="куда jarvis-logs кладёт записи")
    parser.add_argument("--logs", default=str(LOGS))
    parser.add_argument("--synth", action="store_true", help="добавить старые случаи из логов синтезом Fish")
    parser.add_argument("--try", dest="extra", default="", help="слова через запятую — замерить с ними")
    parser.add_argument("--voice", default="3fc339d7c6474b11a221708e2a7b1c4b")
    args = parser.parse_args()

    from vosk import Model, SetLogLevel

    from jarvis.core.assets import ensure_wakeword_model
    from jarvis.core.config import load_config

    SetLogLevel(-1)
    config = load_config().audio.wake_word
    model = Model(str(config.model or ensure_wakeword_model(config.models_dir)))
    hold_s = config.hold_ms / 1000

    cases = load_recorded(Path(args.wake))
    if args.synth:
        import numpy as np

        from tools.wake_bench import _cached, _room

        rng = np.random.default_rng(3)
        for text in logged_false(Path(args.logs)):
            cases.append(Case(_room(_cached(text, args.voice), rng, None), text, "из лога", "синтез"))
    print(f"ложных срабатываний: {len(cases)}")
    if not cases:
        print("записей нет: включи audio.wake_word.false_log на ноуте и дождись первых")
        return

    now = Grammar(model, ["джарвис", *RIVALS, UNKNOWN], config.hold_ms)
    blame: Counter[str] = Counter()
    for case in cases:
        moments = now.fires(case.pcm)
        words = words_of(model, case.pcm)
        guilty = [culprit(words, moment, hold_s) for moment in moments] or ["(не повторилось)"]
        blame.update(guilty)
        print(f"  {case.source:32s} {', '.join(guilty):24s} {case.reason}: {case.text[:70]}")

    print("\nвиновники:")
    for word, count in blame.most_common():
        note = ""
        if word in RIVALS:
            note = "уже в соперниках"
        elif word and not word.startswith("("):
            note = "в словаре модели" if in_vocabulary(model, word) else "НЕТ в словаре модели — не годится"
        print(f"  {count:3d}  {word:20s} {note}")

    extra = [word.strip() for word in args.extra.split(",") if word.strip()]
    if not extra:
        return
    import numpy as np

    from tools.wake_bench import NAMED, _cached, _room

    rng = np.random.default_rng(3)
    named = [_room(_cached(text, args.voice), rng, None) for text in NAMED]
    tried = Grammar(model, ["джарвис", *RIVALS, *extra, UNKNOWN], config.hold_ms)
    for title, detector in (("сейчас", now), (f"+ {', '.join(extra)}", tried)):
        false = sum(len(detector.fires(case.pcm)) for case in cases)
        caught = sum(1 for piece in named if detector.fires(piece))
        print(f"{title:40s} ложных {false}/{len(cases)}; обращений поймано {caught}/{len(named)}")


if __name__ == "__main__":
    main()
