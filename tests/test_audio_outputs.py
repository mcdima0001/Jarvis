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
from jarvis.core.audio.outputs import Output, find_output, is_default, usable_outputs
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


def _outputs() -> list[Output]:
    return usable_outputs(DEVICES, HOSTAPIS, NAMES)


# --- список -----------------------------------------------------------------


def test_one_interface_without_service_devices_and_repeats() -> None:
    outputs = _outputs()
    assert [output.name for output in outputs] == [
        "Динамики (JBL Flip 6)",
        "5.1 (VB-Audio Voicemeeter VAIO)",
        "Динамики (VB-Audio Voicemeeter VAIO)",
        "Onboard Speaker (Audio Device)",
    ]
    # DirectSound: номера его, а не обрезанного MME.
    assert outputs[0].index == 6


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
        ("колонку", "Динамики (JBL Flip 6)"),
        ("колонка", "Динамики (JBL Flip 6)"),
        ("JBL", "Динамики (JBL Flip 6)"),
        ("динамики ноутбука", "Onboard Speaker (Audio Device)"),
    ],
)
def test_named_output_is_found(said: str, expected: str) -> None:
    found = find_output(said, _outputs())
    assert found is not None and found.name == expected


def test_unknown_output_is_not_guessed() -> None:
    assert find_output("наушники", _outputs()) is None


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
    assert sink.device == 6
    # Номер после перезапуска другой, поэтому помнится имя.
    assert documents.data[OUTPUT_MEMORY] == "Динамики (JBL Flip 6)"
    assert "колонка" in result.speech_for("ru")


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
    sink.select(6)

    result = await core.set_output("по умолчанию")

    assert result.ok
    assert sink.device == "Onboard"
    assert documents.data[OUTPUT_MEMORY] is None


async def test_restore_picks_remembered_output_at_start(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments({OUTPUT_MEMORY: "Onboard Speaker (Audio Device)"}), monkeypatch)
    await core.restore_output()
    assert sink.device == 10


async def test_restore_keeps_config_when_remembered_output_is_gone(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments({OUTPUT_MEMORY: "Наушники (Bluetooth)"}), monkeypatch)
    await core.restore_output()
    assert sink.device is None


async def test_list_names_current_output(monkeypatch: Any) -> None:
    sink = NullAudioSink()
    core = _core(sink, FakeDocuments(), monkeypatch)
    sink.select(6)
    said = (await core.outputs()).speech_for("ru")
    assert "Сейчас — колонка" in said


async def test_without_selectable_sink_says_sound_is_off(monkeypatch: Any) -> None:
    core = _core(object(), FakeDocuments(), monkeypatch)
    result = await core.set_output("колонку")
    assert not result.ok
