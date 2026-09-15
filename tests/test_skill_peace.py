"""Эквалайзер Peace Nexus: что меняется, на сколько и что слышно вслух.

Настоящий Peace тут не нужен: модуль `peace_api` подменяется поддельным, который
помнит полосы и вызовы. Проверяется то, что ломается по-настоящему: басы
сдвигаются только в басах и не выходят за предел, пресет узнаётся на слух, а
отказ Peace («закройте окно настроек») звучит как есть.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "peace" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_peace", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


peace = _load()


class PeaceError(RuntimeError):
    pass


class FakeApi:
    PeaceError = PeaceError

    def __init__(self) -> None:
        self.bands_state = [
            {"band": 1, "frequency_hz": 60, "gain_db": 0.0},
            {"band": 2, "frequency_hz": 200, "gain_db": 11.0},
            {"band": 3, "frequency_hz": 1000, "gain_db": 0.0},
            {"band": 4, "frequency_hz": 8000, "gain_db": -1.5},
        ]
        self.calls: list[tuple[str, Any]] = []
        self.on = True
        self.fail: str | None = None

    def _check(self) -> None:
        if self.fail:
            raise PeaceError(self.fail)

    def bands(self) -> list[dict[str, Any]]:
        self._check()
        return [dict(item) for item in self.bands_state]

    def set_band_gain(self, number: int, db: float) -> dict[str, Any]:
        self.calls.append(("gain", (number, db)))
        self.bands_state[number - 1]["gain_db"] = db
        return self.bands_state[number - 1]

    def presets(self) -> list[str]:
        self._check()
        return ["BassBoost", "ULTRA BASS", "Вечер", "Вечеринка"]

    def load_preset(self, name: str) -> str:
        self.calls.append(("load", name))
        return name

    def set_equalizer(self, on: bool) -> bool:
        self.on = on
        return on

    def status(self) -> dict[str, Any]:
        self._check()
        return {"equalizer_on": self.on, "preset": "BassBoost", "preamp_db": -3.0, "pan": 0, "bands": self.bands()}

    def set_all_gains(self, db: float) -> None:
        self.calls.append(("all", db))

    def set_pan(self, value: int) -> int:
        return value

    def _window(self) -> int:
        self._check()
        return 1


async def _skill() -> tuple[Any, FakeApi]:
    skill = peace.PeaceSkill()
    skill._context = SimpleNamespace(setting=lambda key, default=None: default, logger=logging.getLogger("test.peace"))
    await skill.on_setup()
    api = FakeApi()
    skill._api = api
    return skill, api


# --- чистые функции ---------------------------------------------------------


@pytest.mark.parametrize(("value", "spoken"), [
    (3, "3 децибела"), (-3, "минус 3 децибела"), (5, "5 децибел"), (1, "1 децибел"), (2.5, "2.5 децибела"),
])
def test_decibels_are_spoken_in_full(value: float, spoken: str) -> None:
    assert peace.decibels(value) == spoken


@pytest.mark.parametrize(("heard", "preset"), [
    ("ультра бас", "ULTRA BASS"), ("бас буст", "BassBoost"), ("вечер", "Вечер"), ("вечеринка", "Вечеринка"),
])
def test_preset_is_found_by_ear(heard: str, preset: str) -> None:
    assert peace.pick_preset(heard, FakeApi().presets()) == preset


def test_unknown_preset_is_not_guessed() -> None:
    assert peace.pick_preset("джаз", FakeApi().presets()) is None


def test_bass_shift_touches_only_bass_and_respects_the_limit() -> None:
    changes = peace.shifted_gains(FakeApi().bands(), lambda hz: hz < 250, 2.0, 12.0)
    # Полоса 2 упирается в предел 12, полосы 3 и 4 — не басы.
    assert changes == [(1, 2.0), (2, 12.0)]


def test_status_is_one_spoken_sentence() -> None:
    said = peace.describe_status({"equalizer_on": True, "preset": "BassBoost", "preamp_db": -3.0, "pan": -10})
    assert said == "Эквалайзер включён, пресет BassBoost, предусиление минус 3 децибела, баланс влево на 10."
    assert peace.describe_status({"equalizer_on": False}) == "Эквалайзер выключен."
    # Так Peace помечает несохранённые правки (живое состояние 14.09.2026).
    assert peace.describe_status({"equalizer_on": True, "preset": "BassBoost*"}) == (
        "Эквалайзер включён, пресет BassBoost, с изменениями."
    )


# --- команды ----------------------------------------------------------------


async def test_more_bass_changes_bass_bands() -> None:
    skill, api = await _skill()
    result = await skill.more_bass()
    assert result.ok and api.calls == [("gain", (1, 2.0)), ("gain", (2, 12.0))]
    assert result.speech_for("ru") == "Добавил басов на 2 децибела."


async def test_less_treble_lowers_only_high_bands() -> None:
    skill, api = await _skill()
    await skill.less_treble()
    assert api.calls == [("gain", (4, -3.5))]


async def test_preset_is_loaded_by_spoken_name() -> None:
    skill, api = await _skill()
    result = await skill.load_preset("ультра бас")
    assert result.ok and api.calls == [("load", "ULTRA BASS")]


async def test_unknown_preset_lists_what_exists_and_loads_nothing() -> None:
    skill, api = await _skill()
    result = await skill.load_preset("джаз")
    assert not result.ok and api.calls == []
    assert "BassBoost" in result.speech_for("ru")


async def test_peace_refusal_is_spoken_as_is() -> None:
    skill, api = await _skill()
    api.fail = "в Peace открыто окно настроек, закройте его"
    result = await skill.status()
    assert not result.ok
    assert "закройте его" in result.speech_for("ru")


async def test_missing_api_folder_is_a_clear_refusal(tmp_path: Path) -> None:
    skill = peace.PeaceSkill()
    skill._context = SimpleNamespace(
        setting=lambda key, default=None: str(tmp_path) if key == "api_dir" else default,
        logger=logging.getLogger("test.peace"),
    )
    await skill.on_setup()
    result = await skill.status()
    assert not result.ok and "peace_api.py" in result.error
    assert not (await skill.health()).ok


async def test_equalizer_switches_and_status_reads() -> None:
    skill, api = await _skill()
    assert (await skill.equalizer_off()).speech_for("ru") in ("Эквалайзер выключен.", "Выключил эквалайзер.")
    assert api.on is False
    assert (await skill.status()).speech_for("ru") == "Эквалайзер выключен."


def test_only_four_tools_go_to_the_model_catalog() -> None:
    from jarvis.core.tools import collect_tools

    routable = sorted(item.spec.name for item in collect_tools(peace.PeaceSkill(), namespace="peace") if item.spec.routable)
    assert routable == ["peace.equalizer", "peace.load_preset", "peace.shift", "peace.status"]


@pytest.mark.parametrize(
    ("spoken", "part"),
    [("басы", "bass"), ("Низы", "bass"), ("середину", "mid"), ("средние частоты", "mid"), ("вокал", "mid"),
     ("верха", "treble"), ("высокие", "treble")],
)
def test_spoken_part_of_the_spectrum(spoken: str, part: str) -> None:
    assert peace.normalize_part(spoken) == part


def test_middle_takes_only_what_lies_strictly_between_bass_and_treble() -> None:
    """Края 250 Гц и 4 кГц у владельца — басы и верха, а не середина."""
    mid = peace.band_chooser("mid", 250, 4000)
    bass = peace.band_chooser("bass", 250, 4000)
    treble = peace.band_chooser("treble", 250, 4000)
    assert [mid(hz) for hz in (100, 250, 1000, 4000, 8000)] == [False, False, True, False, False]
    assert bass(250) and not bass(251)
    assert treble(4000) and not treble(3999)


def test_long_preset_list_is_shortened_for_speech() -> None:
    """Десяток названий латиницей подряд вслух не дослушать (разбор 14.09.2026)."""
    assert peace.few_names(["A", "B", "C", "D", "E"]) == "A, B, C и ещё 2"
    assert peace.few_names(["A", "B"]) == "A, B"
    assert peace.few_names([]) == "ни одного"


async def test_missing_module_is_spoken_without_the_path(tmp_path: Path) -> None:
    """Путь к peace_api.py вслух не произнести — он только в тексте ошибки."""
    skill, _ = await _skill()
    skill._api = None
    skill._api_dir = tmp_path / "нет-такой-папки"
    result = await skill.presets()
    assert not result.ok
    assert result.speech_for("ru") == "Не нашёл модуль эквалайзера."
    assert "peace_api.py" in (result.error or "")
