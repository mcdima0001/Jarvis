"""Реальный ввод-вывод звука через sounddevice (PortAudio).

Захват идёт в потоке PortAudio, а не в event loop: колбэк отдаёт кадры в
очередь через `call_soon_threadsafe`, и уже оттуда их читает конвейер. Если
обработка отстаёт, очередь роняет самые старые кадры — лучше потерять кусок
шума, чем копить память и отвечать с задержкой в полминуты.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from jarvis.core.config import AudioConfig
from jarvis.core.errors import AudioError

from .protocol import AudioFrame

logger = logging.getLogger(__name__)

#: Сколько кадров держать в очереди (при 30 мс это около 6 секунд).
_QUEUE_SIZE = 200


def _import_sounddevice() -> Any:
    """Импортировать sounddevice с внятной ошибкой.

    Пакет инициализирует PortAudio прямо при импорте, поэтому падает не только
    `ImportError`, но и ошибка звуковой подсистемы — на машине без звука это
    `PortAudioError`. Ловим широко.
    """
    try:
        import sounddevice
    except Exception as exc:  # noqa: BLE001 — импорт может бросить что угодно
        raise AudioError(
            f"sounddevice недоступен ({type(exc).__name__}: {exc}). "
            f"Установи: pip install 'jarvis-core[audio]'"
        ) from exc
    return sounddevice


def list_devices() -> str:
    """Вернуть список звуковых устройств для настройки."""
    sd = _import_sounddevice()
    lines = ["Звуковые устройства (номер — имя, входов/выходов):", ""]
    try:
        default_in, default_out = sd.default.device
    except Exception:  # noqa: BLE001
        default_in = default_out = None

    for index, device in enumerate(sd.query_devices()):
        marks = []
        if index == default_in:
            marks.append("вход по умолчанию")
        if index == default_out:
            marks.append("выход по умолчанию")
        suffix = f"  <- {', '.join(marks)}" if marks else ""
        lines.append(
            f"  {index:>2}  {device['name']}"
            f"  [{device['max_input_channels']} вх / {device['max_output_channels']} вых]"
            f"{suffix}"
        )
    lines += [
        "",
        "Номер или часть имени впиши в config.yaml:",
        "  audio.input_device / audio.output_device",
    ]
    return "\n".join(lines)


class SoundDeviceSource:
    """Захват звука с микрофона."""

    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._stream: Any = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "audio-in"

    @property
    def _frame_samples(self) -> int:
        """Сколько сэмплов в одном кадре."""
        return int(self._config.sample_rate * self._config.frame_ms / 1000)

    async def start(self) -> None:
        """Открыть входной поток."""
        if self._stream is not None:
            return
        sd = _import_sounddevice()
        self._loop = asyncio.get_running_loop()

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._config.sample_rate,
                blocksize=self._frame_samples,
                device=self._config.input_device,
                channels=1,
                dtype="int16",
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise AudioError(
                f"Не удалось открыть микрофон ({type(exc).__name__}: {exc}). "
                f"Проверь список устройств: python -m jarvis --devices"
            ) from exc

        device_name = self._config.input_device if self._config.input_device is not None else "по умолчанию"
        logger.info(
            "Микрофон открыт: устройство=%s частота=%d Гц кадр=%d мс",
            device_name,
            self._config.sample_rate,
            self._config.frame_ms,
        )

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Колбэк PortAudio. Выполняется в чужом потоке — только передача данных."""
        if status:
            logger.debug("Статус аудиопотока: %s", status)
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._enqueue, bytes(indata))

    def _enqueue(self, data: bytes) -> None:
        """Положить кадр в очередь, вытеснив самый старый при переполнении."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning(
                    "Очередь аудио переполнена, кадры отбрасываются (всего %d)",
                    self._dropped,
                )
        self._queue.put_nowait(data)

    async def stop(self) -> None:
        """Закрыть входной поток."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.debug("Микрофон закрыт")

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Асинхронный поток кадров с микрофона."""
        while True:
            data = await self._queue.get()
            yield AudioFrame(
                data=data,
                sample_rate=self._config.sample_rate,
                timestamp=time.time(),
            )


class SoundDeviceSink:
    """Воспроизведение звука."""

    def __init__(self, config: AudioConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "audio-out"

    async def start(self) -> None:
        """Проверить, что звуковая подсистема доступна."""
        _import_sounddevice()
        device = self._config.output_device if self._config.output_device is not None else "по умолчанию"
        logger.info("Аудиовыход готов: устройство=%s", device)

    async def stop(self) -> None:
        """Поток открывается на время каждой реплики — закрывать нечего."""

    async def play(self, audio: bytes, *, sample_rate: int) -> None:
        """Воспроизвести моно-PCM 16 бит, не блокируя event loop."""
        if not audio:
            return
        # Реплики не должны накладываться друг на друга.
        async with self._lock:
            await asyncio.to_thread(self._play_sync, audio, sample_rate)

    def _play_sync(self, audio: bytes, sample_rate: int) -> None:
        """Синхронное воспроизведение — выполняется в отдельном потоке."""
        sd = _import_sounddevice()
        try:
            with sd.RawOutputStream(
                samplerate=sample_rate,
                device=self._config.output_device,
                channels=1,
                dtype="int16",
            ) as stream:
                stream.write(audio)
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось воспроизвести звук: %s: %s", type(exc).__name__, exc)
