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
import warnings
from typing import Any, Callable

import numpy

logger = logging.getLogger(__name__)

#: Сколько сэмплов просить за один раз. При 16 кГц это 32 мс — вдвое больше
#: блока обработки, чтобы поток захвата не дёргался на каждый чих.
_CHUNK = 512

#: Сколько ждать открытия записи, прежде чем ответить вызывающему.
_OPEN_TIMEOUT_S = 3.0


def _prepare_com() -> None:
    """Разрешить потоку работать с COM.

    В Windows это делается **на каждый поток**, а `soundcard` вызывает
    инициализацию один раз при импорте — то есть в том потоке, который его
    импортировал. Наш поток захвата про это ничего не знает.

    Лезем во внутренности пакета намеренно: своей обёртки он не даёт, а тянуть
    ради одного вызова ещё одну библиотеку незачем. Не получилось — не беда: на
    не-Windows этого не нужно вовсе, а на Windows COM обычно уже поднят.
    """
    try:
        from soundcard.mediafoundation import _ffi, _ole32

        _ole32.CoInitializeEx(_ffi.NULL, 0)
    except Exception:  # noqa: BLE001 — не Windows либо уже поднят
        pass


def _quiet_discontinuity_warnings() -> None:
    """Убрать из консоли «data discontinuity in recording».

    `soundcard` печатает это через `warnings.warn` на каждый пропуск в записи, а
    пропуски у петлевого захвата — дело обычное: WASAPI отдаёт буфер по своему
    расписанию, и любая заминка планировщика помечается этим флагом. За минуту
    работы набегают десятки строк, и за ними перестаёт быть видно происходящее
    (жалоба владельца от 01.08.2026: «засирает лог»).

    Молча терять это всё же неправильно: пропуск означает потерянные сэмплы, то
    есть съехавшее выравнивание. Поэтому предупреждение гасится, а следит за
    последствиями `_realign` — он замечает сдвиг по самому звуку, что надёжнее
    любого флага.
    """
    try:
        from soundcard.mediafoundation import SoundcardRuntimeWarning

        warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
    except Exception:  # noqa: BLE001 — не Windows либо пакета нет
        pass


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
        self._opened = threading.Event()
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
        self._stop.clear()
        self._opened.clear()
        self._failed = ""
        self._thread = threading.Thread(target=self._loop, daemon=True, name="aec-loopback")
        self._thread.start()
        # Ждём недолго и только ради внятного ответа: и в логе при запуске, и в
        # отчёте `--check-aec` разница между «опоры нет» и «опора есть» должна
        # быть видна сразу, а не выясняться по отсутствию эффекта.
        self._opened.wait(timeout=_OPEN_TIMEOUT_S)
        if self._failed:
            logger.warning("Опорный сигнал недоступен (%s) — AEC работать не будет", self._failed)
            return False
        logger.info("Опорный сигнал: %s", self._name or "устройство вывода по умолчанию")
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

    def _loop(self) -> None:
        """Поток захвата: пишет в обработчик, пока не попросят остановиться.

        Устройство ищется **здесь же**, а не в вызывающем потоке, и это не
        придирка: в Windows COM инициализируется на каждый поток отдельно, а
        объекты WASAPI создаются и используются через него. Открыть запись в
        одном потоке и читать её из другого — верный способ получить
        `CO_E_NOTINITIALIZED` в самом неудобном месте.
        """
        try:
            _prepare_com()
            _quiet_discontinuity_warnings()
            microphone = self._find()
            # Два канала, а не один: микрофон слышит стерео сведённым по воздуху,
            # и среднее ближе к правде, чем один левый канал.
            with microphone.recorder(samplerate=self._rate, channels=2, blocksize=_CHUNK) as recorder:
                self._opened.set()
                while not self._stop.is_set():
                    block = recorder.record(numframes=_CHUNK)
                    self._on_audio(numpy.mean(numpy.asarray(block, dtype=numpy.float64), axis=1))
        except Exception as exc:  # noqa: BLE001
            self._failed = f"{type(exc).__name__}: {exc}"
            logger.warning("Захват опорного сигнала прервался (%s) — AEC выключен", self._failed)
        finally:
            # Разбудить ожидающего в любом случае: молчание тут читалось бы как
            # «ещё открывается» и стоило бы полной паузы на пустом месте.
            self._opened.set()
