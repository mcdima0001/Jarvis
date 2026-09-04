"""Аудиотракт: протоколы, реализации и сборка по конфигу."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jarvis.core.config import AudioConfig
from jarvis.core.errors import AudioError

from .devices import SoundDeviceSink, SoundDeviceSource, list_devices
from .echo import EchoCancellingSource
from .null import AlwaysActiveWakeWord, NullAudioSink, NullAudioSource, PassthroughVAD
from .sound import load_sound, trim_silence
from .protocol import VAD, AudioFrame, AudioSink, AudioSource, WakeWord
from .silero import SileroVAD
from .vad import EnergyVAD, frame_rms

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioStack:
    """Собранный аудиотракт."""

    source: AudioSource
    sink: AudioSink
    vad: VAD
    wake_word: WakeWord

    @property
    def live(self) -> bool:
        """Работает ли реальный звук (а не заглушки)."""
        return not isinstance(self.source, NullAudioSource)


def _energy_vad(config: AudioConfig) -> EnergyVAD:
    """Энергетический детектор — он же запасной для всех остальных."""
    return EnergyVAD(
        threshold=config.vad.threshold,
        calibrate_frames=max(1, int(config.vad.calibrate_seconds * 1000 / config.frame_ms)),
    )


def _build_vad(config: AudioConfig) -> VAD:
    """Выбрать детектор речи по конфигу."""
    if config.vad.engine in ("", "none", "null"):
        return PassthroughVAD()
    if config.vad.engine == "energy":
        return _energy_vad(config)
    if config.vad.engine == "silero":
        try:
            from jarvis.core.assets import ensure_vad_model

            return SileroVAD(
                ensure_vad_model(config.vad.models_dir),
                sample_rate=config.sample_rate,
                threshold=config.vad.threshold,
            )
        except Exception as exc:  # noqa: BLE001 — нет модели, сети или onnxruntime
            # Остаться без детектора хуже, чем остаться с грубым: без него
            # Whisper молотит на любом шуме.
            logger.warning(
                "Silero VAD не поднялся (%s: %s) — беру энергетический",
                type(exc).__name__,
                exc,
            )
            return _energy_vad(config)
    logger.warning(
        "Неизвестный движок VAD %r — пропускаю весь звук. Доступны: energy, silero",
        config.vad.engine,
    )
    return PassthroughVAD()


def _build_wake_word(config: AudioConfig) -> WakeWord:
    """Выбрать детектор активационной фразы по конфигу.

    В режимах ``text`` и ``none`` детектора по звуку нет вовсе: имя ищется в
    расшифровке, и слот занимает пропускающая заглушка. Модель нужна только
    для ``acoustic`` — и её отсутствие не должно ломать запуск, поэтому откат
    на текстовый гейт молча предусмотрен.
    """
    if config.wake_word.mode != "acoustic":
        return AlwaysActiveWakeWord(config.wake_word.phrase)

    try:
        if config.wake_word.engine == "openwakeword":
            return _open_wake_word(config)
        if config.wake_word.engine == "vosk":
            return _vosk_wake_word(config)
        logger.warning(
            "Неизвестный движок активации %r — слушаю имя по тексту. "
            "Доступны: vosk, openwakeword",
            config.wake_word.engine,
        )
    except Exception as exc:  # noqa: BLE001 — нет модели, сети или пакета
        logger.warning(
            "Активация по звуку не поднялась (%s: %s) — слушаю имя по тексту",
            type(exc).__name__,
            exc,
        )
    return AlwaysActiveWakeWord(config.wake_word.phrase)


def _vosk_wake_word(config: AudioConfig) -> WakeWord:
    """Активация распознаванием со словарём из одного слова.

    Модель качается сама при первом запуске, как у Silero VAD: обучать нечего,
    а требовать ручного скачивания ради 45 МБ значит держать режим выключенным
    у всех, кто не дочитал документацию.
    """
    from jarvis.core.assets import ensure_wakeword_model

    from .wakeword import VoskWakeWord

    model = config.wake_word.model or ensure_wakeword_model(config.wake_word.models_dir)
    # Алиасы сюда **не идут**, и это измерено, а не решено из общих
    # соображений. Они придуманы для ошибок Whisper — «жаркость», «дживс», —
    # то есть описывают, как имя выглядит в расшифровке. Декодер же слышит
    # звук напрямую, и лишние похожие слова в словаре только расширяют ему
    # выбор: с алиасами «turn on the music» стало срабатывать как имя, а без
    # них тот же файл даёт чистое «[unk]».
    return VoskWakeWord(
        model,
        phrases=config.wake_word.phrases,
        sample_rate=config.sample_rate,
    )


def _open_wake_word(config: AudioConfig) -> WakeWord:
    """Активация своей обученной моделью.

    Готовой модели на «Джарвис» тут не бывает: `hey_jarvis` из набора
    openWakeWord на русское произношение не отзывается (замер — в докстринге
    `wakeword.py`). Поэтому без файла модели режим не поднимается вовсе.
    """
    from .wakeword import OpenWakeWord

    if config.wake_word.model is None:
        raise AudioError(
            "Для openwakeword нужна своя обученная модель (audio.wake_word.model). "
            "Как её обучить — docs/wakeword.md. Готовая под русское имя не подходит; "
            "проще оставить engine: vosk."
        )
    return OpenWakeWord(
        config.wake_word.model,
        phrase=config.wake_word.phrase,
        sample_rate=config.sample_rate,
        threshold=config.wake_word.threshold,
    )


def build_audio(config: AudioConfig) -> AudioStack:
    """Собрать аудиотракт по конфигу.

    Если звуковая подсистема недоступна (нет устройства, не установлен
    sounddevice), поднимаются заглушки: приложение должно стартовать и на
    сервере без звуковой карты.
    """
    vad = _build_vad(config)
    wake_word = _build_wake_word(config)

    if config.engine in ("", "none", "null"):
        logger.info("Звук отключён в конфиге (audio.engine)")
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    if config.engine != "sounddevice":
        logger.warning(
            "Неизвестный движок звука %r — использую заглушки. Доступен: sounddevice",
            config.engine,
        )
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    try:
        from .devices import _import_sounddevice

        _import_sounddevice()
    except AudioError as exc:
        logger.warning("%s Звук отключён, команды доступны через --say.", exc)
        return AudioStack(
            source=NullAudioSource(),
            sink=NullAudioSink(),
            vad=vad,
            wake_word=wake_word,
        )

    return AudioStack(
        source=_with_echo_cancelling(SoundDeviceSource(config), config),
        sink=SoundDeviceSink(config),
        vad=vad,
        wake_word=wake_word,
    )


def _with_echo_cancelling(source: AudioSource, config: AudioConfig) -> AudioSource:
    """Обернуть микрофон вычитанием собственного звука.

    Обёртка ставится **до** VAD и распознавания, потому что чинит она их обоих:
    музыка не доходит ни до детектора речи, ни до Whisper. Приглушение музыки
    этого не заменяет — оно случается уже после того, как ассистента позвали, а
    само слово «Джарвис» произносится в полный фон.

    Ничего не поднялось (нет `soundcard`, нет петлевого устройства) — обёртка
    остаётся, но опорный сигнал в ней тишина: срез низа работает, вычитать
    нечего. Так лучше, чем два разных пути на одном коде.
    """
    if not config.aec.enabled:
        return source

    wrapper = EchoCancellingSource(
        source,
        sample_rate=config.sample_rate,
        tail_ms=config.aec.tail_ms,
        residual=config.aec.residual,
        high_pass_hz=config.aec.high_pass_hz,
    )
    if config.aec.reference in ("", "off", "none"):
        logger.info("Опорный сигнал отключён (audio.aec.reference) — остаётся только срез низа")
        return wrapper

    from .loopback import LoopbackSource

    wrapper.attach(
        LoopbackSource(
            sample_rate=config.sample_rate,
            device=None if config.aec.reference == "auto" else config.aec.reference,
            on_audio=wrapper.push_reference,
        )
    )
    return wrapper


__all__ = [
    "load_sound",
    "trim_silence",
    "VAD",
    "AlwaysActiveWakeWord",
    "AudioFrame",
    "AudioSink",
    "AudioSource",
    "AudioStack",
    "EchoCancellingSource",
    "EnergyVAD",
    "NullAudioSink",
    "NullAudioSource",
    "PassthroughVAD",
    "SileroVAD",
    "SoundDeviceSink",
    "SoundDeviceSource",
    "WakeWord",
    "build_audio",
    "frame_rms",
    "list_devices",
]
