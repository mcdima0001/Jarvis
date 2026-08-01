"""Захват того, что играет: опорный сигнал для эхоподавления.

Чтобы вычесть музыку из микрофона, её надо сперва услышать в чистом виде — не
из комнаты, а прямо из системы, до колонок. В Windows это называется loopback:
звуковая подсистема отдаёт копию того, что уходит на устройство вывода.

**Почему не sounddevice, на котором построено всё остальное.** PortAudio умеет
loopback только начиная со сборок новее той, что приезжает с колесом: у
владельца стоит `PortAudio V19.7.0-devel`, и петлевых устройств в списке нет
вовсе. Так что либо просить его собирать PortAudio руками, либо взять пакет,
который ходит в WASAPI напрямую. `soundcard` — второе: чистый Python поверх
ctypes, колесо без сборки, работает на 3.14.

Заодно он снимает две задачи, которые пришлось бы решать самим: Windows по
дороге **сам пересчитывает частоту** (48 кГц у вывода против 16 кГц у нас) и
**сам сводит каналы**, потому что при открытии выставляются флаги
автоконвертации. А когда не играет ничего, поток отдаёт нули по часам — молчание
приходит молчанием, а не зависанием.

Запасной путь на случай, когда `soundcard` не встал или отказал: обычное входное
устройство, названное в конфиге. Годится «Стерео микшер», если он есть в
драйвере, или петлевой вход Voicemeeter — у владельца тот стоит и без нас.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import numpy

logger = logging.getLogger(__name__)

#: Сколько сэмплов просить за один раз. При 16 кГц это 32 мс — вдвое больше
#: блока обработки, чтобы поток захвата не дёргался на каждый чих.
_CHUNK = 512


def _import_soundcard() -> Any:
    """Импортировать soundcard с внятной ошибкой."""
    try:
        import soundcard
    except Exception as exc:  # noqa: BLE001 — пакет лезет в COM прямо при импорте
        raise RuntimeError(
            f"soundcard недоступен ({type(exc).__name__}: {exc}). "
            f"Установи: pip install 'jarvis-core[aec]'"
        ) from exc
    return soundcard


def describe_outputs() -> list[str]:
    """Что можно взять за опорный сигнал — для настройки и для отчёта."""
    try:
        soundcard = _import_soundcard()
        return [f"{item.name}" for item in soundcard.all_speakers()]
    except Exception as exc:  # noqa: BLE001
        return [f"(список недоступен: {type(exc).__name__}: {exc})"]


class LoopbackSource:
    """Копия того, что уходит на колонки, кусками по `_CHUNK` сэмплов.

    Захват идёт в своём потоке и **никогда не роняет систему**: любая ошибка
    гасит опорный сигнал, а не ассистента. Без AEC он работает хуже, но
    работает, а вот упавший из-за звуковой библиотеки ассистент бесполезен
    целиком.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        device: str | None = None,
        on_audio: Callable[[numpy.ndarray], None],
    ) -> None:
        self._rate = sample_rate
        self._device = device
        self._on_audio = on_audio
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._name = ""
        self._failed = ""

    @property
    def name(self) -> str:
        """С какого устройства слушаем."""
        return self._name

    @property
    def failure(self) -> str:
        """Почему не получилось, если не получилось."""
        return self._failed

    def start(self) -> bool:
        """Открыть поток. `False` — не вышло, причина в `failure`."""
        if self._thread is not None:
            return True
        try:
            microphone = self._find()
        except Exception as exc:  # noqa: BLE001
            self._failed = f"{type(exc).__name__}: {exc}"
            logger.warning("Опорный сигнал недоступен (%s) — AEC работать не будет", self._failed)
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(microphone,), daemon=True, name="aec-loopback")
        self._thread.start()
        logger.info("Опорный сигнал: %s", self._name)
        return True

    def stop(self) -> None:
        """Закрыть поток."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _find(self) -> Any:
        """Найти петлевое устройство: названное в конфиге либо вывод по умолчанию."""
        soundcard = _import_soundcard()
        if self._device:
            microphone = soundcard.get_microphone(self._device, include_loopback=True)
        else:
            # Тот выход, который система считает основным. Именно он и звучит.
            microphone = soundcard.get_microphone(soundcard.default_speaker().name, include_loopback=True)
        self._name = getattr(microphone, "name", str(microphone))
        return microphone

    def _loop(self, microphone: Any) -> None:
        """Поток захвата: пишет в обработчик, пока не попросят остановиться."""
        try:
            # Два канала, а не один: микрофон слышит стерео сведённым по воздуху,
            # и среднее ближе к правде, чем один левый канал.
            with microphone.recorder(samplerate=self._rate, channels=2, blocksize=_CHUNK) as recorder:
                while not self._stop.is_set():
                    block = recorder.record(numframes=_CHUNK)
                    self._on_audio(numpy.mean(numpy.asarray(block, dtype=numpy.float64), axis=1))
        except Exception as exc:  # noqa: BLE001
            self._failed = f"{type(exc).__name__}: {exc}"
            logger.warning("Захват опорного сигнала прервался (%s) — AEC выключен", self._failed)
