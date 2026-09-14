"""Выбор аудиовыхода голосом: список устройств, поиск названного, память.

Список устройств здесь — слепок настоящего с ноутбука владельца (14.09.2026):
одна колонка видна несколькими интерфейсами, у MME имена обрезаны, выходы
Voicemeeter повторяются под одним именем. На этом слепке и проверяется, что
голосом выбирается то, что назвали, а не сосед по списку.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.core import builtin
from jarvis.core.audio.devices import SoundDeviceSink
from jarvis.core.audio.null import NullAudioSink
from jarvis.core.audio.outputs import (
    Output,
    config_value,
    find_output,
    is_default,
    usable_inputs,
    usable_outputs,
)
from jarvis.core.audio.protocol import SelectableSink
from jarvis.core.builtin import OUTPUT_MEMORY, CoreTools
from jarvis.core.config import AudioConfig

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]


def _device(name: str, api: int, outputs: int = 2) -> dict[str, Any]:
    return {"name": name, "hostapi": api, "max_output_channels": outputs}


DEVICES = [
    _device("Переназначение звуковых устр. - Input", 0, outputs=0),
    _device("Переназначение звуковых устр. - Output", 0),
    _device("Динамики (JBL Flip 6)", 0),
    _device("5.1 (VB-Audio Voicemeeter VAIO)", 0),
    # MME обрезает имя до 31 знака — ровно так оно и приходит.
    _device("Динамики (VB-Audio Voicemeeter ", 0),
    _device("Onboard Speaker (Audio Device)", 0),
    _device("Первичный звуковой драйвер", 1),
    _device("Динамики (JBL Flip 6)", 1),
    _device("5.1 (VB-Audio Voicemeeter VAIO)", 1),
    _device("Динамики (VB-Audio Voicemeeter VAIO)", 1),
    _device("Динамики (VB-Audio Voicemeeter VAIO)", 1),
    _device("Onboard Speaker (Audio Device)", 1),
    _device("Микрофон (Audio Device)", 1, outputs=0),
    _device("Динамики (JBL Flip 6)", 2),
]
NAMES = {"JBL Flip 6": "колонка", "Onboard Speaker": "динамики ноутбука"}
JBL, ONBOARD = 2, 5


def _outputs() -> list[Output]:
    return usable_outputs(DEVICES, HOSTAPIS, NAMES)


# --- список -----------------------------------------------------------------


def test_plays_through_mme_with_full_names() -> None:
    """MME — путь голоса по умолчанию. DirectSound на ноутбуке владельца молчал:
    петлевой захват динамиков дал ноль, а MME на том же сигнале — 0.076."""
    outputs = _outputs()
    assert [output.name for output in outputs] == [
        "Динамики (JBL Flip 6)",
        "5.1 (VB-Audio Voicemeeter VAIO)",
        "Динамики (VB-Audio Voicemeeter VAIO)",
        "Onboard Speaker (Audio Device)",
    ]
    assert [output.index for output in outputs] == [JBL, 3, 4, ONBOARD]


def test_short_mme_name_is_not_stretched_to_a_longer_one() -> None:
    devices = [
        _device("Динамики", 0),
        _device("Динамики", 1),
        _device("Динамики (VB-Audio Voicemeeter VAIO)", 1),
    ]
    assert [output.name for output in usable_outputs(devices, HOSTAPIS)] == ["Динамики"]


def test_microphones_follow_the_same_rules_and_carry_config_value() -> None:
    devices = [
        _device("Переназначение звуковых устр. - Input", 0, outputs=0) | {"max_input_channels": 2},
        # Ровно 31 знак: столько оставляет MME.
        _device("Микрофон (Realtek High Definiti", 0, outputs=0) | {"max_input_channels": 2},
        _device("Первичный драйвер записи звука", 1, outputs=0) | {"max_input_channels": 2},
        _device("Микрофон (Realtek High Definition Audio)", 1, outputs=0) | {"max_input_channels": 2},
        _device("Динамики (JBL Flip 6)", 0),
    ]
    inputs = usable_inputs(devices, HOSTAPIS)
    assert [item.name for item in inputs] == ["Микрофон (Realtek High Definition Audio)"]
    # В конфиг — обрезанное имя MME вместе с интерфейсом: так sounddevice найдёт одно.
    assert config_value(inputs[0]) == "Микрофон (Realtek High Definiti, MME"


def test_spoken_names_come_from_config_or_the_name_itself() -> None:
    spoken = [output.spoken for output in _outputs()]
    assert spoken[0] == "колонка"
    assert spoken[3] == "динамики ноутбука"
    assert spoken[1] == "5.1 VB-Audio Voicemeeter VAIO"


def test_without_known_interfaces_everything_is_offered() -> None:
    outputs = usable_outputs([_device("Speakers", 0)], [{"name": "ALSA"}])
    assert [output.name for output in outputs] == ["Speakers"]


# --- поиск названного -------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("колонку", JBL),
        ("колонка", JBL),
        ("JBL", JBL),
        ("динамики ноутбука", ONBOARD),
        # Живой запуск 14.09.2026: строкой целиком это совпадало с «колонка».
        ("колонки ноутбука", ONBOARD),
        ("ноутбук", ONBOARD),
        ("onboard speaker", ONBOARD),
    ],
)
def test_named_output_is_found(said: str, expected: int) -> None:
    found = find_output(said, _outputs())
    assert found is not None and found.index == expected


def test_unknown_output_is_not_guessed() -> None:
    assert find_output("наушники", _outputs()) is None


def test_ambiguous_name_is_not_guessed() -> None:
    # «Динамики» — и JBL, и Voicemeeter: лучше перечислить, чем угадать.
    assert find_output("динамики", _outputs()) is None


def test_default_request_is_recognised() -> None:
    assert is_default("выход по умолчанию")
    assert is_default("как обычно")
    assert not is_default("колонку")


# --- вывод ------------------------------------------------------------------


def test_sinks_can_switch_device() -> None:
    sink = SoundDeviceSink(AudioConfig(output_device=3))
    assert isinstance(sink, SelectableSink)
    assert sink.device == 3
    sink.select(6)
    assert sink.device == 6
    assert isinstance(NullAudioSink(), SelectableSink)


# --- инструменты ------------------------------------------------------------


class FakeDocuments:
    def __init__(self, data: dict[tuple[str, str], Any] | None = None) -> None:
        self.data = dict(data or {})

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        return self.data.get((namespace, key), default)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        self.data[(namespace, key)] = value


def _core(sink: Any, documents: FakeDocuments, monkeypatch: Any, *, configured: Any = None) -> CoreTools:
    monkeypatch.setattr(builtin, "query_outputs", lambda names: usable_outputs(DEVICES, HOSTAPIS, names))
    return CoreTools(
        llm=None,  # type: ignore[arg-type]
        memory=SimpleNamespace(documents=documents),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        sink=sink,
        output_device=configured,
        output_names=NAMES,
    )


async def test_switch_by_voice_and_remember_by_name(monkeypatch: Any) -> None:
    sink, documents = NullAudioSink(), FakeDocuments()
    core = _core(sink, documents, monkeypatch)

    result = await core.set_output("колонку")

    assert result.ok
    assert sink.device == JBL
    # Номер после перезапуска другой, поэтому помнится имя.
    assert documents.data[OUTPUT_MEMORY] == "Динамики (JBL Flip 6)"
    # Название стоит после двоеточия: «через колонка» звучит безграмотно.
    assert result.speech_for("ru") == "Голос переключён: колонка."


async def test_unknown_output_lists_what_exists(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments(), monkeypatch)
    result = await core.set_output("наушники")
    assert not result.ok
    assert sink.device is None
    assert "динамики ноутбука" in result.speech_for("ru")


async def test_default_returns_to_configured_output(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    documents = FakeDocuments({OUTPUT_MEMORY: "Динамики (JBL Flip 6)"})
    core = _core(sink, documents, monkeypatch, configured="Onboard")
    sink.select(JBL)

    result = await core.set_output("по умолчанию")

    assert result.ok
    assert sink.device == "Onboard"
    assert documents.data[OUTPUT_MEMORY] is None


async def test_restore_picks_remembered_output_at_start(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments({OUTPUT_MEMORY: "Onboard Speaker (Audio Device)"}), monkeypatch)
    await core.restore_output()
    assert sink.device == ONBOARD


async def test_restore_keeps_config_when_remembered_output_is_gone(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments({OUTPUT_MEMORY: "Наушники (Bluetooth)"}), monkeypatch)
    await core.restore_output()
    assert sink.device is None


async def test_list_names_current_output(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments(), monkeypatch)
    sink.select(JBL)
    said = (await core.outputs()).speech_for("ru")
    assert "Сейчас — колонка" in said


async def test_without_selectable_sink_says_sound_is_off(monkeypatch: Any) -> None:
    core = _core(object(), FakeDocuments(), monkeypatch)
    result = await core.set_output("колонку")
    assert not result.ok
