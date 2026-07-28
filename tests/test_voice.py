"""Голосовой конвейер: обращение по имени, VAD, тайминги захвата."""

from __future__ import annotations

import time

import pytest

from jarvis.core.audio import AudioFrame, EnergyVAD, frame_rms
from jarvis.core.bus import LocalEventBus
from jarvis.core.config import AudioConfig, VADConfig, WakeWordConfig
from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router import Dispatcher, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from jarvis.core.tts import NullTTS
from jarvis.core.voice import VoicePipeline


class Lights:
    """Скилл-заглушка для проверки маршрутизации."""

    @tool(phrases=["включи свет"])
    async def on(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")


def _pipeline(registry: ToolRegistry, events: LocalEventBus, **wake: object) -> VoicePipeline:
    """Собрать конвейер с заглушками вместо звука."""
    from jarvis.core.audio import NullAudioSink, NullAudioSource, PassthroughVAD
    from jarvis.core.audio.null import AlwaysActiveWakeWord
    from jarvis.core.stt import NullSTT

    config = AudioConfig(
        wake_word=WakeWordConfig(
            mode=str(wake.get("mode", "text")),
            phrase=str(wake.get("phrase", "джарвис")),
            aliases=tuple(wake.get("aliases", ("жарвис",))),
            similarity=float(wake.get("similarity", 0.7)),
            follow_up_s=float(wake.get("follow_up_s", 10.0)),
        )
    )
    router = Router([PhraseResolver(registry)], threshold=0.6)
    return VoicePipeline(
        source=NullAudioSource(),
        sink=NullAudioSink(),
        vad=PassthroughVAD(),
        wake_word=AlwaysActiveWakeWord("джарвис"),
        stt=NullSTT(),
        tts=NullTTS(),
        dispatcher=Dispatcher(router=router, registry=registry),
        events=events,
        config=config,
    )


@pytest.fixture
def pipeline(registry: ToolRegistry, events: LocalEventBus) -> VoicePipeline:
    """Конвейер с одним зарегистрированным скиллом."""
    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)
    return _pipeline(registry, events)


def test_name_is_stripped_from_command(pipeline: VoicePipeline) -> None:
    """Имя отделяется от команды."""
    called, command = pipeline._strip_wake("Джарвис, включи свет")
    assert called
    assert command == "включи свет"


def test_alias_counts_as_name(pipeline: VoicePipeline) -> None:
    """Известный вариант ослышки засчитывается как обращение."""
    called, command = pipeline._strip_wake("жарвис включи свет")
    assert called
    assert command == "включи свет"


def test_unrelated_phrase_is_not_a_call(pipeline: VoicePipeline) -> None:
    """Обычная реплика не считается обращением, текст не портится."""
    called, command = pipeline._strip_wake("передай отвёртку")
    assert not called
    assert command == "передай отвёртку"


def test_command_without_name_ignored(pipeline: VoicePipeline) -> None:
    """Без обращения по имени команда не выполняется."""
    assert pipeline._extract_command("включи свет") is None


def test_bare_name_returns_empty_command(pipeline: VoicePipeline) -> None:
    """Голое «Джарвис» — это обращение без команды."""
    assert pipeline._extract_command("Джарвис") == ""


def test_follow_up_window_accepts_command_without_name(pipeline: VoicePipeline) -> None:
    """В окне ответа команда принимается без повторного обращения."""
    pipeline._follow_up_until = time.time() + 10
    assert pipeline._extract_command("включи свет") == "включи свет"


def test_follow_up_window_still_strips_name(pipeline: VoicePipeline) -> None:
    """В окне ответа имя всё равно вырезается, а не уезжает в роутер.

    Иначе «Джарвис» → «Джарвис, включи свет» приводило к тому, что роутер
    видел фразу вместе с именем и не находил команду.
    """
    pipeline._follow_up_until = time.time() + 10
    assert pipeline._extract_command("Джарвис, включи свет") == "включи свет"


async def test_handle_strips_name_for_text_input(pipeline: VoicePipeline) -> None:
    """Текстовая команда с именем маршрутизируется так же, как голосовая."""
    result = await pipeline.handle(Utterance(text="Джарвис, включи свет", source="text"))
    assert result.ok
    assert result.tool == "lights.on"


async def test_mode_none_reacts_without_name(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """При mode=none обращение по имени не требуется."""
    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)
    pipeline = _pipeline(registry, events, mode="none")

    assert pipeline._extract_command("включи свет") == "включи свет"


# --- VAD --------------------------------------------------------------------


def _frame(amplitude: int, samples: int = 480) -> AudioFrame:
    """Кадр с постоянной амплитудой."""
    import array

    data = array.array("h", [amplitude] * samples)
    return AudioFrame(data=data.tobytes(), sample_rate=16000)


def test_rms_scales_with_amplitude() -> None:
    """Громкость считается и нормируется к диапазону 0..1."""
    assert frame_rms(_frame(0)) == 0.0
    assert 0.0 < frame_rms(_frame(1000)) < frame_rms(_frame(10000)) < 1.0


def test_fixed_threshold_separates_speech_from_silence() -> None:
    """С заданным порогом тишина отсекается, речь проходит."""
    vad = EnergyVAD(threshold=0.05)

    assert not vad.is_speech(_frame(100))
    assert vad.is_speech(_frame(8000))


def test_calibration_learns_room_noise() -> None:
    """При нулевом пороге VAD замеряет фон и выставляет порог сам."""
    vad = EnergyVAD(threshold=0.0, calibrate_frames=10)

    for _ in range(10):
        assert not vad.is_speech(_frame(300)), "во время калибровки речи нет"

    assert vad.threshold > frame_rms(_frame(300))
    assert not vad.is_speech(_frame(300)), "фон не должен считаться речью"
    assert vad.is_speech(_frame(9000)), "речь громче фона — должна пройти"


# --- тайминги ---------------------------------------------------------------


def test_audio_config_derives_frame_math() -> None:
    """Производные величины считаются из частоты и длины кадра."""
    config = AudioConfig(sample_rate=16000, frame_ms=30, silence_ms=900, max_utterance_s=10.0)

    assert config.frame_bytes == 960
    assert config.silence_frames == 30
    assert config.max_utterance_bytes == 320000
