"""Активация по имени: словарь декодера и подтверждение гипотезы.

Настоящей модели тут нет и не нужно: она весит 45 МБ, качается из сети и в CI
её не будет. Проверяется то, что решает сам детектор, — какие написания вообще
попадут в словарь и когда промежуточной гипотезе можно верить.

Декодер подделан списком гипотез: он отдаёт по одной на кадр, ровно как
настоящий отдавал бы `PartialResult`. Так проверка идёт на той самой картине,
которая снята с живого декодера покадрово (см. `HOLD_MS`), и не зависит ни от
сети, ни от установленных пакетов.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from jarvis.core.audio.protocol import AudioFrame
from jarvis.core.audio.wakeword import HOLD_MS, known_words
from jarvis.core.errors import AudioError

RATE = 16000
FRAME_MS = 30
#: Кадр 30 мс: столько же приходит с микрофона при настройках по умолчанию.
FRAME = AudioFrame(data=b"\0" * (RATE * FRAME_MS // 1000 * 2), sample_rate=RATE)


# --- какие написания попадут в словарь --------------------------------------


def test_latin_spellings_are_dropped() -> None:
    """`jarvis` латиницей русская модель не знает, и молчать об этом нельзя.

    Декодер отбрасывает такое слово сам, с предупреждением в свой лог — а лог
    его мы гасим. Написание, которое выглядит рабочим и не срабатывает ни разу,
    хуже, чем отсутствующее.
    """
    assert known_words(["джарвис", "jarvis"]) == ["джарвис"]


def test_spellings_are_normalised() -> None:
    """Регистр и лишние пробелы к делу не относятся."""
    assert known_words([" Джарвис ", "ДЖАРВИС"]) == ["джарвис", "джарвис"]


def test_empty_spellings_are_skipped() -> None:
    """Пустая строка в конфиге — не написание имени."""
    assert known_words(["", "   ", "джарвис"]) == ["джарвис"]


# --- подтверждение гипотезы -------------------------------------------------


class _FakeRecognizer:
    """Декодер, который отдаёт заранее заданные гипотезы по одной на кадр."""

    def __init__(self, guesses: list[str]) -> None:
        self._guesses = list(guesses)
        self._said = ""

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 — имя из vosk
        self._said = self._guesses.pop(0) if self._guesses else ""
        return False

    def PartialResult(self) -> str:  # noqa: N802 — имя из vosk
        return json.dumps({"partial": self._said}, ensure_ascii=False)


def _detector(guesses: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Собрать детектор на поддельном декодере."""
    made: list[_FakeRecognizer] = []

    def recognizer(model: object, rate: float, grammar: str) -> _FakeRecognizer:
        # Каждый новый декодер продолжает ту же ленту гипотез: сброс создаёт
        # его заново, а проверять мы хотим поведение, а не устройство.
        fake = _FakeRecognizer(guesses)
        made.append(fake)
        return fake

    module = types.ModuleType("vosk")
    module.KaldiRecognizer = recognizer
    module.Model = lambda path: object()
    module.SetLogLevel = lambda level: None
    monkeypatch.setitem(sys.modules, "vosk", module)

    from jarvis.core.audio.wakeword import VoskWakeWord

    (tmp_path / "model").mkdir(exist_ok=True)
    return VoskWakeWord(tmp_path / "model", phrases=("джарвис",), sample_rate=RATE)


def _run(detector, frames: int) -> list[int]:
    """На каких кадрах детектор сказал «да»."""
    return [index for index in range(frames) if detector.detect(FRAME)]


#: Столько кадров по 30 мс нужно, чтобы набрать выдержку.
HOLD_FRAMES = int(HOLD_MS / FRAME_MS)


def test_short_guess_is_not_believed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Гипотеза, которая отменилась, именем не была.

    Так ведут себя ложные срабатывания: на живом декодере «turn on the music»
    и «поставь на паузу пожалуйста» на восемь кадров становились именем, а
    потом исправлялись на «[unk]».
    """
    guesses = ["джарвис"] * (HOLD_FRAMES - 1) + ["[unk]"] * 20
    assert _run(_detector(guesses, tmp_path, monkeypatch), len(guesses)) == []


def test_held_guess_fires_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Продержавшееся имя срабатывает — и ровно один раз.

    Настоящее имя остаётся в гипотезе до конца фразы. Без запрета на повтор
    «Джарвис, включи музыку» сработало бы полсотни раз подряд.
    """
    guesses = ["джарвис"] * (HOLD_FRAMES + 40)
    assert _run(_detector(guesses, tmp_path, monkeypatch), len(guesses)) == [HOLD_FRAMES - 1]


def test_reset_lets_the_next_call_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После сброса детектор снова готов услышать имя."""
    detector = _detector(["джарвис"] * (HOLD_FRAMES * 3), tmp_path, monkeypatch)

    assert _run(detector, HOLD_FRAMES) == [HOLD_FRAMES - 1]
    detector.reset()
    assert _run(detector, HOLD_FRAMES) == [HOLD_FRAMES - 1]


def test_broken_hold_starts_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Прерванная выдержка копится заново, а не продолжается.

    Иначе имя, дважды мелькнувшее в разных фразах, сложилось бы в одно
    срабатывание.
    """
    half = HOLD_FRAMES // 2
    guesses = ["джарвис"] * half + ["[unk]"] + ["джарвис"] * HOLD_FRAMES
    fired = _run(_detector(guesses, tmp_path, monkeypatch), len(guesses))

    assert fired == [half + HOLD_FRAMES]


def test_unusable_spellings_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Все написания латиницей — это не «тихо не работает», а отказ.

    Молчаливый детектор неотличим от исправного, пока ассистента не позовут;
    отказ же виден в логе сразу и откатывает на текстовый гейт.
    """
    module = types.ModuleType("vosk")
    module.KaldiRecognizer = lambda *args: None
    module.Model = lambda path: object()
    module.SetLogLevel = lambda level: None
    monkeypatch.setitem(sys.modules, "vosk", module)

    from jarvis.core.audio.wakeword import VoskWakeWord

    (tmp_path / "model").mkdir(exist_ok=True)
    with pytest.raises(AudioError, match="русской модели"):
        VoskWakeWord(tmp_path / "model", phrases=("jarvis", "hey jarvis"))


def test_missing_model_is_refused(tmp_path: Path) -> None:
    """Нет каталога модели — отказ с причиной, а не падение при первом кадре."""
    from jarvis.core.audio.wakeword import VoskWakeWord

    with pytest.raises(AudioError, match="Нет модели"):
        VoskWakeWord(tmp_path / "нет-такого", phrases=("джарвис",))
