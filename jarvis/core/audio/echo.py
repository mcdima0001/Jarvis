"""Сборка эхоподавления: микрофон плюс опорный сигнал на входе конвейера.

Здесь решается вторая половина задачи. Первая — математика — лежит в `aec.py`
и проверена на стенде; вторая, ничуть не проще, — **свести два независимых
потока по времени**. Микрофон и петлевой захват идут своими устройствами, со
своими буферами и своим ходом часов, и общего времени у них нет.

Как это устроено:

* сэмплы опорного сигнала копятся в `ReferenceTrack` — простой очереди с
  верхней границей. Переполнилась — самое старое выбрасывается, и это пишется
  в лог: значит, потоки разъезжаются;
* микрофонные кадры и опорные сэмплы **берутся по порядку**, пара к паре;
* сдвиг между ними **измеряется** (`estimate_delay`) и правится — в обе
  стороны. Опора опережает микрофон сильнее, чем нужно, — задерживаем опору.
  Отстаёт — придерживаем микрофон, потому что двигать опору вперёд бесполезно:
  тех сэмплов ещё не существует. Второй случай не гипотетический: на живой
  машине опора отставала на 205 мс, и без правки AEC не убирал ничего.
  Проверка повторяется: у петлевого захвата молчание отдаётся нулями по часам,
  а не по счётчику сэмплов, и после долгой тишины поток может уехать.

Отдельно стоит сказать, чего тут **не** делается. Эхоподавление не заменяет
приглушение музыки, а дополняет его: приглушение убирает два-три десятка
децибел мгновенно и без нагрузки, но случается **после** того, как ассистента
позвали. AEC работает всегда, в том числе на самом слове «Джарвис», — то есть
чинит ровно ту дыру, ради которой затевался акустический wake word.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

import numpy

from .aec import BLOCK, EchoCanceller, HighPass, estimate_delay, to_float, to_pcm
from .protocol import AudioFrame, AudioSource

logger = logging.getLogger(__name__)

#: Сколько секунд копить, прежде чем первый раз измерить сдвиг между потоками.
#: Меньше — измерение неуверенное, больше — фильтр дольше живёт вслепую.
_WARMUP_S = 5.0

#: Как часто перепроверять сдвиг, пока он ещё не сошёлся, и когда сошёлся.
#: Первый замер почти всегда требует правки, и ждать минуты до второго значит
#: минуту работать вслепую.
_RECHECK_S = 8.0
_SETTLED_RECHECK_S = 60.0

#: Насколько уверенным должно быть измерение, чтобы его применить.
_SURE_ENOUGH = 0.5

#: Мельче этого сдвиг не правим: фильтр съедает его без потерь.
_WORTH_FIXING = 2 * BLOCK

#: С какой задержки микрофона начинаем. Дальше она **измеряется**.
#:
#: Фильтр умеет смотреть **только назад**: эхо в кадре микрофона он ищет среди
#: уже полученных кусков опоры. Значит, опорный сигнал обязан быть **свежее**
#: микрофонного. Казалось бы, он таким и приходит — петлевой захват снимает
#: звук до того, как тот вышел из колонки, — но на живой машине оказалось
#: наоборот: опора отставала на **205 мс**. Столько набегает в WASAPI по дороге
#: (пересчёт частоты, буфер конечной точки, свой буфер захвата), и цифра эта у
#: каждого своя.
#:
#: Поэтому задержку микрофона нельзя выбрать заранее. Здесь только начальное
#: значение, а настоящее подбирается по измерению (`_realign`) в первые секунды
#: работы: слишком мало — придерживаем микрофон сильнее, слишком много —
#: задерживаем опору. Для голоса эта задержка почти ничего не стоит: конец
#: фразы всё равно ждём 800 мс.
_MIC_DELAY_MS = 200.0

#: Насколько опора должна опережать микрофон. Это цель подстройки — но только
#: тогда, когда подстраивать вообще приходится.
_KEEP_MS = 150.0

#: Меньше этого опережения уже опасно: замер плавает, и опора рискует уйти за
#: микрофон, откуда её не достать. Выше — не трогаем, сколько бы ни было.
_SAFE_LEAD_MS = 60.0

#: Больше этого микрофон не придерживаем. Предохранитель на случай, когда сдвиг
#: измерился неверно: лишняя задержка бьёт по отзывчивости, а бесконечная
#: означала бы, что ассистент перестал слышать вовсе.
_MAX_MIC_DELAY_MS = 900.0

#: Сколько стухшей опоры терпим в очереди. Много копить нельзя: старая опора
#: означает, что эхо мы ищем раньше, чем оно прозвучало.
_MAX_LAG_S = 0.064


class ReferenceTrack:
    """Очередь опорных сэмплов между потоком захвата и конвейером.

    Пишет в неё чужой поток, читает конвейер, поэтому всё общение — через два
    коротких метода. Внутри обычный список кусков: собирать их в один массив на
    каждом такте дороже, чем склеить один раз при выдаче.
    """

    def __init__(self, *, sample_rate: int, max_lag_s: float = _MAX_LAG_S) -> None:
        self._limit = int(sample_rate * max_lag_s)
        self._pieces: list[numpy.ndarray] = []
        self._size = 0
        self._dropped = 0
        self._skip = 0
        self._starved = 0

    @property
    def waiting(self) -> int:
        """Сколько сэмплов лежит в очереди."""
        return self._size

    @property
    def dropped(self) -> int:
        """Сколько сэмплов пришлось выбросить из-за расхождения потоков."""
        return self._dropped

    @property
    def starved(self) -> int:
        """Сколько сэмплов пришлось отдать тишиной: опора не поспевала."""
        return self._starved

    def push(self, wave: numpy.ndarray) -> None:
        """Принять кусок от захвата."""
        # Долг по пропуску: опорный поток отстал, и его двигали вперёд дальше,
        # чем лежало в очереди. Остаток снимается с того, что придёт потом.
        if self._skip:
            taken = min(self._skip, len(wave))
            self._skip -= taken
            self._dropped += taken
            wave = wave[taken:]
            if not len(wave):
                return
        self._pieces.append(wave)
        self._size += len(wave)
        # Очередь длиннее предела означает, что опорный поток обгоняет
        # микрофонный. Держать всё — значит копить задержку, а с ней фильтр
        # промахивается мимо своего же хвоста.
        while self._size > self._limit and self._pieces:
            extra = self._pieces.pop(0)
            self._size -= len(extra)
            self._dropped += len(extra)

    def take(self, count: int) -> numpy.ndarray:
        """Выдать ровно `count` сэмплов; не хватило — тишину, **не трогая очередь**.

        Вторая половина фразы и есть самое важное. Первая версия дополняла
        недостачу нулями в конце — и тем самым **растягивала опорный поток**:
        реальные сэмплы уходили в дело, а следом за ними в тот же кадр падала
        тишина, которой в звуке не было. Выравнивание съезжало на каждой такой
        нехватке и уже не восстанавливалось: на стенде AEC не убирал ничего
        вовсе, при том что все части по отдельности работали.

        Правильный ответ — отдать тишину и не расходовать накопленное. Тогда
        опорный сигнал просто копится и становится «старше» микрофонного, а это
        ровно та сторона, в которую он и должен отставать: эхо в записи звучит
        позже своей причины. Постоянный сдвиг потом измеряется и снимается.
        """
        if self._size < count:
            self._starved += count
            return numpy.zeros(count)
        taken: list[numpy.ndarray] = []
        need = count
        while need > 0 and self._pieces:
            piece = self._pieces[0]
            if len(piece) <= need:
                taken.append(piece)
                need -= len(piece)
                self._pieces.pop(0)
            else:
                taken.append(piece[:need])
                self._pieces[0] = piece[need:]
                need = 0
        self._size -= count - need
        return numpy.concatenate(taken) if taken else numpy.zeros(count)

    def hold(self, samples: int) -> None:
        """Задержать опорный поток на столько сэмплов (или подвинуть вперёд).

        Положительное число вставляет тишину в начало — опорный сигнал начинает
        приходить позже, ближе к тому, когда его слышит микрофон. Отрицательное
        выбрасывает сэмплы, то есть двигает поток вперёд.
        """
        if samples > 0:
            self._pieces.insert(0, numpy.zeros(samples))
            self._size += samples
        elif samples < 0:
            # Двигать вперёд можно и дальше, чем накоплено: остаток снимется с
            # того, что придёт следом. Без этого правка молча делается лишь
            # наполовину — в очереди обычно лежат считаные миллисекунды.
            ready = min(-samples, self._size)
            self.take(ready)
            self._skip += -samples - ready


class EchoCancellingSource:
    """Микрофон, из которого вычтено то, что играют колонки.

    Снаружи — обычный `AudioSource`: конвейер не знает, что перед ним обёртка,
    и всё остальное — VAD, wake word, распознавание — не меняется ни на строку.
    """

    def __init__(
        self,
        source: AudioSource,
        *,
        sample_rate: int,
        tail_ms: float = 400.0,
        residual: bool = True,
        high_pass_hz: float = 0.0,
    ) -> None:
        self._source = source
        self._rate = sample_rate
        self._aec = EchoCanceller(sample_rate=sample_rate, tail_ms=tail_ms, residual=residual)
        self._track = ReferenceTrack(sample_rate=sample_rate)
        self._high_pass = HighPass(high_pass_hz, sample_rate) if high_pass_hz > 0 else None
        # Линия задержки микрофона. Она **удерживает** сэмплы, а не подсыпает
        # тишину: в буфере всегда остаётся `_delay` штук, и в фильтр уходит то,
        # что пришло на столько же раньше. Первая версия вставляла нули в
        # начало — а буфер тут же вычерпывался до конца в том же цикле, так что
        # в поток подмешивалась тишина, но звук не задерживался ни на сэмпл.
        # На живом запуске это выглядело как бесконечный рост задержки: правка
        # применялась, замер не менялся, и через минуту упёрлись в предел.
        self._mic: numpy.ndarray = numpy.zeros(0)
        self._ready: numpy.ndarray = numpy.zeros(0)
        # История для измерения сдвига: держим ровно столько, сколько нужно.
        self._seen_mic: list[numpy.ndarray] = []
        self._seen_reference: list[numpy.ndarray] = []
        self._history = 0
        self._checked = 0.0
        self._started = 0.0
        self._told = False
        self._reference: Any = None
        self._keep = int(sample_rate * _KEEP_MS / 1000)
        self._max_delay = int(sample_rate * _MAX_MIC_DELAY_MS / 1000)
        self._safe = int(sample_rate * _SAFE_LEAD_MS / 1000)
        # Дальше половины хвоста опору отпускать незачем: фильтру нужно место и
        # под саму комнату, а не только под дорогу между потоками.
        self._roomy = int(sample_rate * tail_ms / 2000)
        self._delay = int(sample_rate * _MIC_DELAY_MS / 1000)
        self._settled = False
        self._rescales = 0

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return getattr(self._source, "service_name", "audio-in")

    def push_reference(self, wave: numpy.ndarray) -> None:
        """Принять кусок опорного сигнала. Зовётся из потока захвата."""
        self._track.push(wave)

    def attach(self, reference: Any) -> None:
        """Привязать захват опорного сигнала: он поднимается вместе с микрофоном.

        Объект тут нужен только с `start()` и `stop()` — обёртка не знает, чем
        именно снимается копия звука, и знать не должна. Без него всё
        продолжает работать: опорный сигнал остаётся тишиной, фильтр ничего не
        вычитает, а срез низа делается по-прежнему.
        """
        self._reference = reference

    async def start(self) -> None:
        """Открыть микрофон и захват того, что играет."""
        await self._source.start()
        if self._reference is not None:
            self._reference.start()
        self._started = time.monotonic()

    async def stop(self) -> None:
        """Закрыть микрофон и отчитаться, что получилось."""
        if self._reference is not None:
            self._reference.stop()
        await self._source.stop()
        stats = self._aec.stats()
        logger.info(
            "Эхоподавление: убрано %.1f дБ, задержка тракта %.0f мс, "
            "музыка звучала %.0f%% времени, пересборок %d, потеряно опоры %d сэмплов",
            stats.erle_db,
            stats.delay_ms,
            stats.active * 100,
            stats.rescales,
            self._track.dropped,
        )

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Кадры микрофона, очищенные от собственного эха."""
        async for frame in self._source.frames():
            wave = to_float(frame.data)
            if self._high_pass is not None:
                wave = self._high_pass.process(wave)
            self._mic = numpy.concatenate((self._mic, wave))

            while len(self._mic) >= self._delay + BLOCK:
                block, self._mic = self._mic[:BLOCK], self._mic[BLOCK:]
                reference = self._track.take(BLOCK)
                self._remember(block, reference)
                self._ready = numpy.concatenate((self._ready, self._aec.process(block, reference)))

            self._realign()
            size = len(wave)
            while len(self._ready) >= size:
                piece, self._ready = self._ready[:size], self._ready[size:]
                # Отметка времени поправляется на всю задержку тракта: наружу
                # уходит звук, прозвучавший раньше, а по этой отметке считается
                # окно ответа. Без поправки оно молча укорачивалось бы.
                yield AudioFrame(
                    data=to_pcm(piece),
                    sample_rate=frame.sample_rate,
                    timestamp=frame.timestamp - self._lag_s,
                )

    def _remember(self, mic: numpy.ndarray, reference: numpy.ndarray) -> None:
        """Сохранить кусок обоих потоков для измерения сдвига."""
        self._seen_mic.append(mic)
        self._seen_reference.append(reference)
        self._history += len(mic)
        keep = int(self._rate * _WARMUP_S)
        while self._history - len(self._seen_mic[0]) >= keep:
            self._history -= len(self._seen_mic.pop(0))
            self._seen_reference.pop(0)

    @property
    def _lag_s(self) -> float:
        """На сколько секунд выход отстаёт от того, что прозвучало."""
        return (self._delay + self._aec.latency) / self._rate

    def _realign(self) -> None:
        """Измерить сдвиг между потоками и подвинуть тот из них, который нужно.

        Делается редко и только когда музыка звучала: по тишине измерять нечего,
        а неуверенное измерение хуже, чем никакого — оно сдвинет фильтр туда,
        где эха нет вовсе.

        Правка идёт **в обе стороны**, и это не симметрия ради красоты. Опора
        опережает микрофон слишком сильно — задерживаем опору, тут всё просто.
        А вот если она отстаёт, двигать её вперёд бесполезно: тех сэмплов ещё
        не существует. Единственный выход — придержать микрофон, и ровно этот
        случай встретился на живой машине: опора отставала на 205 мс, начальной
        задержки в 200 мс не хватало впритык, и фильтр убирал 0.8 дБ вместо
        двадцати.
        """
        now = time.monotonic()
        if now - self._started < _WARMUP_S:
            return
        # Пересборка фильтра по усилению означает, что тракт заметно изменился,
        # — а рядом с петлевым захватом самая частая причина этого пропуск в
        # записи, то есть потерянные сэмплы и съехавшее выравнивание. Значит,
        # сдвиг стоит перемерить скоро, а не через минуту.
        rescales = self._aec.stats().rescales
        if rescales != self._rescales:
            self._rescales = rescales
            self._settled = False
        wait = _SETTLED_RECHECK_S if self._settled else _RECHECK_S
        if self._checked and now - self._checked < wait:
            return
        if self._history < int(self._rate * _WARMUP_S * 0.8):
            return
        self._checked = now

        mic = numpy.concatenate(self._seen_mic)
        reference = numpy.concatenate(self._seen_reference)
        if float(numpy.mean(reference**2)) < 1e-7:
            self._checked = 0.0  # тишина: попробуем в следующий раз, а не потом
            return

        shift, sure = estimate_delay(mic, reference, sample_rate=self._rate)
        if sure < _SURE_ENOUGH:
            self._tell_once(shift, sure)
            return
        # Опора должна опережать микрофон, но **сколько именно — не важно**,
        # лишь бы в пределах хвоста фильтра. Гнаться за точным числом нельзя:
        # каждая правка сбрасывает подобранный тракт, а замер плавает на
        # десятки миллисекунд, и на живом запуске это вылилось в правку каждые
        # восемь секунд подряд. Поэтому есть полоса, внутри которой не трогаем.
        if self._safe <= shift <= self._roomy:
            self._settled = True
            self._tell_once(shift, sure)
            return
        off = shift - self._keep

        if off > 0:
            self._track.hold(off)
            logger.info(
                "Опора опережала микрофон на %d мс — многовато, задержал её (уверенность %.2f)",
                shift * 1000 // self._rate,
                sure,
            )
        else:
            added = min(-off, self._max_delay - self._delay)
            if added <= 0:
                logger.warning(
                    "Опора опережает микрофон лишь на %d мс, а придерживать его дальше "
                    "некуда. Эхоподавление работать не будет: проверь audio.aec.reference",
                    shift * 1000 // self._rate,
                )
                self._settled = True
                return
            self._delay += added
            logger.info(
                "Опора опережала микрофон всего на %d мс — придержал микрофон, "
                "теперь задержка %d мс (уверенность %.2f)",
                shift * 1000 // self._rate,
                self._delay * 1000 // self._rate,
                sure,
            )
        # Подобранный тракт был привязан к прежнему выравниванию — теперь он
        # вычитал бы эхо не оттуда, где оно есть.
        self._aec.forget()
        self._forget_history()

    def _forget_history(self) -> None:
        """Выбросить историю: она снята при прежнем выравнивании."""
        self._seen_mic.clear()
        self._seen_reference.clear()
        self._history = 0

    def _tell_once(self, shift: int, sure: float) -> None:
        """Сказать один раз, что сдвиг искать не понадобилось."""
        if self._told:
            return
        self._told = True
        logger.debug(
            "Сдвиг между потоками %d мс при уверенности %.2f — правка не нужна",
            shift * 1000 // self._rate,
            sure,
        )
