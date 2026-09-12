"""Контракты аудиотракта: источник, вывод, VAD и активационная фраза.

VAD и wake word не были в исходном ТЗ, но без них ассистент гоняет Whisper на
любом шуме студии — это постоянная нагрузка на CPU и ложные срабатывания.
Место под них заложено сразу: сейчас стоят пропускающие заглушки, позже
Silero VAD или openWakeWord встают одним классом, без правок конвейера.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioFrame:
    """Кадр звука: моно-PCM 16 бит."""

    data: bytes
    sample_rate: int
    timestamp: float = 0.0

    @property
    def duration(self) -> float:
        """Длительность кадра в секундах."""
        return len(self.data) / 2 / self.sample_rate if self.sample_rate else 0.0


@runtime_checkable
class AudioSource(Protocol):
    """Источник звука — микрофон, файл, сетевой поток."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        ...

    async def start(self) -> None:
        """Открыть устройство."""
        ...

    async def stop(self) -> None:
        """Закрыть устройство."""
        ...

    def frames(self) -> AsyncIterator[AudioFrame]:
        """Асинхронный поток кадров."""
        ...


@runtime_checkable
class AudioSink(Protocol):
    """Вывод звука."""

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        ...

    async def start(self) -> None:
        """Открыть устройство."""
        ...

    async def stop(self) -> None:
        """Закрыть устройство."""
        ...

    async def play(self, audio: bytes, *, sample_rate: int) -> None:
        """Воспроизвести моно-PCM 16 бит."""
        ...


@runtime_checkable
class StreamingAudioSink(Protocol):
    """Вывод, умеющий начать звучать до того, как реплика синтезирована вся.

    Отдельный протокол, а не метод в `AudioSink`, потому что это **умение, а не
    обязанность**: играть по кускам может далеко не всякий вывод, а требовать
    этого от всех значило бы чинить заглушки ради возможности, которой они не
    пользуются. Кто умеет — того и спрашивают (`isinstance`), остальные молча
    идут обычным путём.

    Зачем: облачный синтез отдаёт реплику потоком, и первый кусок приходит
    втрое раньше последнего (замер 12.09.2026: 0.97 с против 2.45 с). Ждать
    конца — значит дарить человеку полторы секунды тишины на каждой свежей
    реплике.
    """

    async def play_stream(
        self, chunks: AsyncIterator[bytes], *, sample_rate: int
    ) -> None:
        """Воспроизвести моно-PCM 16 бит, принимая его по кускам."""
        ...


@runtime_checkable
class VAD(Protocol):
    """Детектор речи: отделяет речь от тишины и шума."""

    def is_speech(self, frame: AudioFrame) -> bool:
        """Есть ли речь в кадре."""
        ...

    def reset(self) -> None:
        """Сбросить внутреннее состояние между фразами."""
        ...


@runtime_checkable
class WakeWord(Protocol):
    """Детектор активационной фразы."""

    @property
    def phrase(self) -> str:
        """Фраза активации."""
        ...

    def detect(self, frame: AudioFrame) -> bool:
        """Прозвучала ли активационная фраза."""
        ...

    def reset(self) -> None:
        """Сбросить состояние после срабатывания."""
        ...
