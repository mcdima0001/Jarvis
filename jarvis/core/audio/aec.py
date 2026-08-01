"""Вычитание собственного звука из микрофона: AEC и подавление остатка.

Задача узкая и оттого решаемая. Шумодав вообще (RNNoise, WebRTC NS) убирает
**стационарный** шум — гул, вентилятор, шипение, — а музыку и чужую речь не
трогает by design. Здесь же шум не какой-нибудь: он наш собственный, мы знаем
его сэмпл в сэмпл, потому что сами его и играем. Значит, его можно не давить
на слух, а **вычесть**.

Схема классическая и состоит из двух ступеней:

1. **Линейная** (`EchoCanceller`) — адаптивный фильтр подбирает, как звук из
   колонок доходит до микрофона (задержка, отражения, окраска), и вычитает
   предсказанное эхо из записи. Там, где путь линеен, это даёт 20-30 дБ и
   **не портит речь вообще**: вычитается ровно то, что играло.
2. **Спектральная** (`_Residual`) — то, что линейный вычесть не смог. На
   громкости колонка искажает, корпус ноутбука вибрирует от саба, и эта часть
   к опорному сигналу линейным преобразованием не сводится. Она давится по
   спектру, оценкой «сколько эха осталось в этой полосе». Речь при этом слегка
   страдает, поэтому есть предел `floor_db`: тише него не давим никогда.

Реализация — обычный numpy, без scipy и без внешних библиотек. Причин две.
Готовых сборок WebRTC APM и speexdsp под Windows и Python 3.14 нет, собирать
их у владельца — отдельная беда; а стоит всё это на удивление дёшево: фильтр
живёт в частотной области одним массивом `(разделов, бинов)`, и весь расчёт
сводится к паре БПФ на блок в 16 мс. На двух слабых ядрах это проценты одного.

Главное, чего здесь нет и быть не может: **оценка выигрыша на живой машине**.
Всё, что ниже, проверено на синтетическом эхе и настоящей речи (тесты меряют
ERLE в децибелах), но комната, колонка с сабом и микрофон в корпусе ноутбука
проверяются только у владельца.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy

#: Длина блока обработки в сэмплах. При 16 кГц это 16 мс.
#:
#: Блок и определяет всё остальное: БПФ вдвое длиннее (метод перекрытия с
#: сохранением), задержка спектральной ступени равна одному блоку, а длина
#: фильтра округляется до целого числа блоков. Меньше — точнее по времени, но
#: дороже; больше — дешевле, но хуже ловит короткие отражения.
BLOCK = 256

#: Насколько плавно усредняется мощность опорного сигнала по бинам.
#: Ближе к единице — спокойнее шаг адаптации, но медленнее отклик на смену
#: трека.
_POWER_SMOOTH = 0.9

#: Ниже этой мощности опорный сигнал считается тишиной, и фильтр **не
#: обучается**. Иначе он подстраивается под собственный шум микрофона и
#: медленно разъезжается в паузах между треками.
_QUIET_REFERENCE = 1e-7

#: Чтобы нигде не делить на ноль.
_EPS = 1e-10

#: Сколько блоков смотрим, прежде чем решить, что фильтр разъехался (0.5 с).
_WATCH_BLOCKS = 32

#: Во сколько раз выход должен быть громче входа, чтобы это считалось поломкой,
#: а не неудачной секундой.
_DIVERGED = 1.4

#: Пределы поправки усиления при пересборке. Шире — и одна случайная секунда
#: двойного разговора уводила бы фильтр куда угодно.
_RESCALE = (0.05, 2.0)


def _hann(size: int) -> numpy.ndarray:
    """Периодическое окно Ханна.

    Именно периодическое, а не симметричное (`numpy.hanning`): при перекрытии
    вполовину сумма сдвинутых копий периодического окна равна единице, и
    сигнал собирается обратно без ряби. У симметричного — не равна.
    """
    return 0.5 - 0.5 * numpy.cos(2.0 * numpy.pi * numpy.arange(size) / size)


def high_pass_taps(cutoff_hz: float, sample_rate: int, *, taps: int = 63) -> numpy.ndarray:
    """Коэффициенты фильтра, срезающего низ.

    Зачем он вообще нужен: саб стоит на полу, микрофон — в корпусе ноутбука, и
    часть баса приходит в него **через стол и корпус**, а не по воздуху. Этот
    путь не имеет отношения к акустическому, вычесть его вместе с остальным
    эхом нельзя, а мощности в нём много. Речь при этом начинается от 300 Гц,
    и всё, что ниже полутора сотен, для распознавания — чистая помеха.

    Фильтр сделан с конечной характеристикой (свёртка), а не рекурсивным: его
    можно применить одним вызовом numpy, тогда как рекурсивный требует цикла
    по сэмплам на чистом Python.
    """
    if taps % 2 == 0:
        taps += 1  # симметричное ядро требует нечётной длины
    middle = taps // 2
    steps = numpy.arange(taps) - middle
    ratio = 2.0 * cutoff_hz / sample_rate
    # Фильтр нижних частот (оконный sinc), затем обращение в верхние: единица
    # в середине минус то, что фильтр нижних частот пропускает.
    low = ratio * numpy.sinc(ratio * steps) * numpy.hamming(taps)
    low /= numpy.sum(low)
    high = -low
    high[middle] += 1.0
    return high.astype(numpy.float64)


class HighPass:
    """Срез низких частот с памятью между блоками."""

    def __init__(self, cutoff_hz: float, sample_rate: int) -> None:
        self._taps = high_pass_taps(cutoff_hz, sample_rate)
        self._tail = numpy.zeros(len(self._taps) - 1)

    def process(self, wave: numpy.ndarray) -> numpy.ndarray:
        """Отфильтровать очередной кусок, ничего не потеряв на стыке."""
        joined = numpy.concatenate((self._tail, wave))
        self._tail = joined[-(len(self._taps) - 1) :] if len(self._taps) > 1 else joined[:0]
        return numpy.convolve(joined, self._taps, mode="valid")[: len(wave)]


def estimate_delay(
    mic: numpy.ndarray, reference: numpy.ndarray, *, sample_rate: int, max_ms: float = 500.0
) -> tuple[int, float]:
    """На сколько сэмплов запись отстаёт от того, что играло.

    Нужно это **для диагностики, а не для работы**: сам фильтр задержку
    поглощает своей длиной, ему подсказки не требуется. Но когда AEC не даёт
    ничего, первый вопрос всегда один — тот ли сигнал взят за опорный и
    попадает ли он по времени. Возвращается сдвиг в сэмплах и насколько
    уверенно он найден (от нуля до единицы).

    Считается взаимной корреляцией с выравниванием спектра (GCC-PHAT): перед
    обратным преобразованием у произведения спектров оставляется только фаза.
    Смысл в том, что задержка живёт именно в фазе, а амплитуда только мешает —
    у музыки вся мощность в басу, и обычная корреляция даёт широкий холм по
    периоду бита вместо пика на задержке. Проверено: на музыке с ровным ритмом
    сравнение по огибающим ошибалось на 60 мс, PHAT попадает в сэмплы.
    """
    size = min(len(mic), len(reference))
    if size < BLOCK * 8:
        return 0, 0.0
    first = mic[:size] - numpy.mean(mic[:size])
    second = reference[:size] - numpy.mean(reference[:size])
    if float(numpy.sum(first**2)) < _EPS or float(numpy.sum(second**2)) < _EPS:
        return 0, 0.0

    length = 1 << int(numpy.ceil(numpy.log2(2 * size)))
    spectrum = numpy.fft.rfft(first, length) * numpy.conj(numpy.fft.rfft(second, length))
    spectrum /= numpy.abs(spectrum) + _EPS
    correlation = numpy.fft.irfft(spectrum, length)

    # Ищем в **обе** стороны, и это не педантизм. Физически запись отстаёт от
    # того, что её вызвало, — но опорный поток идёт отдельным устройством и
    # своим потоком, и запаздывать может он сам. Тогда эхо в микрофоне звучит
    # **раньше** своей причины, а такое не описывается фильтром вообще: он
    # смотрит только назад. Отрицательный сдвиг — единственный способ это
    # заметить, иначе AEC просто молча ничего не делает.
    reach = max(1, int(max_ms * sample_rate / 1000))
    window = numpy.concatenate((correlation[-reach:], correlation[: reach + 1]))
    best = int(numpy.argmax(window))
    height = float(window[best])
    # Насколько пик выделяется на фоне остального — это и есть мера доверия.
    noise = float(numpy.mean(numpy.abs(correlation)))
    return best - reach, float(numpy.clip(height / (height + 8.0 * noise + _EPS), 0.0, 1.0))


@dataclass(frozen=True, slots=True, kw_only=True)
class EchoStats:
    """Сколько эха удалось убрать — для лога и для диагностики."""

    #: Насколько тише стал звук там, где играла музыка, в децибелах.
    erle_db: float
    #: Куда сошёлся фильтр: задержка до самого сильного отражения, в мс.
    delay_ms: float
    #: Доля блоков, на которых опорный сигнал был не тишиной.
    active: float
    #: Сколько раз фильтр пришлось пересобирать по усилению.
    rescales: int = 0


class EchoCanceller:
    """Адаптивный фильтр: вычитает из микрофона то, что играют колонки.

    Устройство — блочный частотный NLMS с разбиением на разделы (PBFDAF).
    По-человечески: фильтр длиной в несколько сотен миллисекунд разрезан на
    куски по одному блоку, каждый живёт своей строкой в матрице, и свёртка
    превращается в поэлементное умножение спектров. Отсюда и дешевизна.

    Три вещи, без которых оно не работает и которые легко упустить:

    * **Шаг нормируется мощностью опоры.** Без этого громкая музыка рвёт
      фильтр в клочья, а тихая не обучает его вовсе.
    * **В тишине не обучаемся.** Нечему: опорного сигнала нет, и фильтр начнёт
      подгоняться под шум микрофона.
    * **Когда говорит человек, обучение притормаживает само** (`_pace`). Это
      «двойной разговор»: в записи есть и эхо, и речь, а фильтр считает всё
      расхождение своей ошибкой и уезжает. Отдельного детектора нет намеренно —
      шаг просто умножается на долю эха в записи, и чем больше постороннего,
      тем осторожнее шаг.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        tail_ms: float = 400.0,
        step: float = 0.4,
        residual: bool = True,
        floor_db: float = -18.0,
    ) -> None:
        self._rate = sample_rate
        self._block = BLOCK
        self._size = 2 * BLOCK
        self._bins = BLOCK + 1
        self._parts = max(1, int(math.ceil(tail_ms * sample_rate / 1000.0 / BLOCK)))
        self._step = float(step)

        self._weights = numpy.zeros((self._parts, self._bins), dtype=numpy.complex128)
        self._history = numpy.zeros((self._parts, self._bins), dtype=numpy.complex128)
        self._power = numpy.zeros(self._bins)
        self._previous = numpy.zeros(self._block)
        self._turn = 0

        self._residual = _Residual(size=self._size, floor_db=floor_db) if residual else None
        self._heard = 0.0
        self._left = 0.0
        self._blocks = 0
        self._active = 0
        self._watch = numpy.zeros(4)
        self._watched = 0
        self._rescales = 0
        self._bad = 0

    @property
    def block(self) -> int:
        """Сколько сэмплов принимает и отдаёт один вызов `process`."""
        return self._block

    @property
    def latency(self) -> int:
        """На сколько сэмплов выход отстаёт от входа.

        Линейная ступень работает без задержки вовсе, спектральная — с одним
        блоком: ей нужен следующий кусок, чтобы окно перекрылось. Наружу это
        отдаётся числом, а не прячется, потому что сравнивать выход со входом
        придётся и в тестах, и при разборе живой записи.
        """
        return self._block if self._residual is not None else 0

    def forget(self) -> None:
        """Забыть подобранный тракт: он больше не описывает происходящее.

        Зовётся, когда опорный поток подвинули по времени. Всё, чему фильтр
        научился, было привязано к прежнему выравниванию, и оставлять это —
        значит вычитать эхо не оттуда, где оно есть.
        """
        self._weights.fill(0)
        self._power.fill(0)

    def process(self, mic: numpy.ndarray, reference: numpy.ndarray) -> numpy.ndarray:
        """Убрать из блока микрофона то, что в это время играло.

        Оба массива — ровно `block` сэмплов, значения в пределах ±1.
        """
        window = numpy.concatenate((self._previous, reference))
        self._previous = reference.copy()
        spectrum = numpy.fft.rfft(window)

        # Сдвигаем историю: свежий спектр встаёт первым, самый старый уходит.
        self._history = numpy.roll(self._history, 1, axis=0)
        self._history[0] = spectrum

        echo_spectrum = numpy.sum(self._weights * self._history, axis=0)
        echo = numpy.fft.irfft(echo_spectrum, self._size)[self._block :]
        error = mic - echo

        loud = float(numpy.mean(numpy.abs(spectrum) ** 2))
        if loud > _QUIET_REFERENCE:
            self._adapt(spectrum, error)
            self._active += 1
        self._blocks += 1
        self._heard += float(numpy.sum(mic**2))
        self._left += float(numpy.sum(error**2))
        self._watch += (
            float(numpy.dot(mic, mic)),
            float(numpy.dot(error, error)),
            float(numpy.dot(mic, echo)),
            float(numpy.dot(echo, echo)),
        )
        self._watched += 1
        if self._watched >= _WATCH_BLOCKS:
            self._recover()

        if self._residual is None:
            return error
        return self._residual.process(error, echo)

    def _adapt(self, spectrum: numpy.ndarray, error: numpy.ndarray) -> None:
        """Подвинуть фильтр в сторону меньшей ошибки."""
        self._power = _POWER_SMOOTH * self._power + (1.0 - _POWER_SMOOTH) * numpy.abs(spectrum) ** 2
        # Ошибка дополняется нулями спереди: в методе перекрытия с сохранением
        # значимая половина всегда вторая.
        padded = numpy.concatenate((numpy.zeros(self._block), error))
        gradient = numpy.conj(self._history) * numpy.fft.rfft(padded)

        # Двойной разговор. Доля эха в том, что услышали, и есть мера доверия к
        # ошибке: говорит только колонка — доверяем полностью, вмешался
        # человек — почти нет.
        pace = self._pace(error)
        self._weights += (self._step * pace) * gradient / (self._parts * self._power + _EPS)

        # Проекция обратно во время: у настоящего фильтра отклик короче блока,
        # и «хвост», который набегает в частотной области, — чистый мусор.
        # Чинится он по одному разделу за блок: полная проекция стоила бы по
        # два БПФ на каждый раздел, а сходимость от этого почти не меняется.
        self._turn = (self._turn + 1) % self._parts
        taps = numpy.fft.irfft(self._weights[self._turn], self._size)
        taps[self._block :] = 0.0
        self._weights[self._turn] = numpy.fft.rfft(taps)

    def _recover(self) -> None:
        """Заметить, что фильтр стал вредить, и поправить его усиление.

        Случай не выдуманный: громкость крутят **на самой колонке**, а не в
        Windows. Тогда опорный сигнал прежний, а эхо стало вдвое тише — фильтр
        продолжает вычитать столько же, сколько раньше, и делает **хуже, чем
        если бы его не было вовсе**. На стенде это −7 дБ и десяток секунд на
        переучивание.

        Своё же приглушение музыки такой беды не вызывает: оно идёт по звуковым
        сессиям Windows, то есть попадает и в опорный сигнал тоже. Путь от
        колонки до микрофона при этом не меняется, и фильтру всё равно —
        проверено, одна плохая секунда на переходе и сразу прежние 29 дБ.

        Чинится не сбросом, а **поправкой усиления**: если тракт изменился
        только громкостью, наилучший множитель считается сразу, по методу
        наименьших квадратов. Сброс в ноль означал бы те же десять секунд с
        нуля, а тут фильтр возвращается к работе за один шаг.
        """
        heard, left, cross, echo = self._watch
        self._watch = numpy.zeros(4)
        self._watched = 0
        if left <= _DIVERGED * heard or echo <= _EPS:
            self._bad = 0
            return
        # Одного плохого полусекундного окна мало. Пока фильтр только сходится,
        # его выход законно бывает громче входа, и поправка усиления в этот
        # момент отбрасывает уже проделанную работу назад — на стенде это
        # видно провалом посреди ровного подъёма.
        self._bad += 1
        if self._bad < 2:
            return
        self._bad = 0
        scale = float(numpy.clip(cross / echo, *_RESCALE))
        self._weights *= scale
        self._rescales += 1

    def _pace(self, error: numpy.ndarray) -> float:
        """Насколько доверять ошибке: 1 — только эхо, 0 — говорит человек."""
        left = float(numpy.mean(error**2))
        if left <= _EPS:
            return 1.0
        # Чем больше осталось после вычитания, тем вероятнее, что осталась
        # речь, а не недоубранное эхо. Мера грубая, но устойчивая: точный
        # детектор двойного разговора сам по себе сложнее фильтра.
        share = float(numpy.mean(self._power)) / (float(numpy.mean(self._power)) + left + _EPS)
        return float(numpy.clip(share, 0.05, 1.0))

    def stats(self) -> EchoStats:
        """Сколько всего убрали с момента запуска."""
        erle = 10.0 * math.log10((self._heard + _EPS) / (self._left + _EPS))
        taps = numpy.fft.irfft(self._weights, self._size, axis=1)[:, : self._block]
        peak = int(numpy.argmax(numpy.abs(taps))) if taps.size else 0
        return EchoStats(
            erle_db=erle,
            delay_ms=(peak % self._block + (peak // self._block) * self._block) * 1000.0 / self._rate,
            active=self._active / self._blocks if self._blocks else 0.0,
            rescales=self._rescales,
        )


class _Residual:
    """Спектральное подавление того, что линейный фильтр вычесть не смог.

    Работает по взвешенному перекрытию с окном: блок анализируется вместе с
    предыдущим, каждая полоса умножается на свой коэффициент, и куски
    складываются обратно. Окно корень-из-Ханна стоит и на входе, и на выходе —
    при перекрытии вполовину их произведение суммируется точно в единицу, то
    есть при коэффициенте 1 сигнал проходит **без изменений вообще**.

    Главное здесь — **утечка измеряется, а не задаётся**. Сперва она была
    константой (0.4, «линейный фильтр берёт не всё»), и на стенде это вышло
    боком: там, где линейная ступень отработала на 26 дБ, спектральная резала
    полосы, в которых никакого эха уже не было, и **отнимала у речи 8 дБ из
    честно заработанных двадцати**. Утечка — не свойство алгоритма, а свойство
    комнаты и громкости, и меняется она вместе с ними.

    Меряется по минимуму: отношение «осталось / было» падает мгновенно и растёт
    медленно. Причина в двойном разговоре — когда человек говорит, в остатке
    появляется его голос, отношение взлетает, и приняв это за утечку, фильтр
    придавил бы ровно то, ради чего всё затевалось. Минимум за последние
    секунды такого всплеска не замечает.

    Цена ступени — один блок задержки, 16 мс.
    """

    #: Насколько быстро оценка утечки может расти обратно (за блок).
    _RISE = 1.02
    #: Пределы, за которые оценке уходить незачем: −40 дБ и «не убрали ничего».
    _RANGE = (1e-4, 1.0)

    def __init__(self, *, size: int, floor_db: float) -> None:
        self._size = size
        self._half = size // 2
        self._window = numpy.sqrt(_hann(size))
        self._floor = 10.0 ** (floor_db / 10.0)
        self._leak = numpy.full(size // 2 + 1, 0.1)
        self._error_tail = numpy.zeros(self._half)
        self._echo_tail = numpy.zeros(self._half)
        self._overlap = numpy.zeros(self._half)

    def process(self, error: numpy.ndarray, echo: numpy.ndarray) -> numpy.ndarray:
        """Придавить полосы, в которых эха осталось больше, чем полезного."""
        joined = numpy.concatenate((self._error_tail, error))
        echoes = numpy.concatenate((self._echo_tail, echo))
        self._error_tail = error.copy()
        self._echo_tail = echo.copy()

        spectrum = numpy.fft.rfft(joined * self._window)
        left = numpy.abs(spectrum) ** 2
        was = numpy.abs(numpy.fft.rfft(echoes * self._window)) ** 2

        loud = was > _EPS
        if numpy.any(loud):
            share = numpy.where(loud, left / (was + _EPS), self._leak)
            self._leak = numpy.minimum(share, self._leak * self._RISE)
            numpy.clip(self._leak, *self._RANGE, out=self._leak)

        # Вычитание по мощности: из того, что осталось, убираем оценку эха.
        gain = (left - self._leak * was) / (left + _EPS)
        numpy.clip(gain, self._floor, 1.0, out=gain)

        piece = numpy.fft.irfft(spectrum * numpy.sqrt(gain), self._size) * self._window
        out = piece[: self._half] + self._overlap
        self._overlap = piece[self._half :]
        return out


def to_float(pcm: bytes) -> numpy.ndarray:
    """PCM 16 бит -> значения в пределах ±1."""
    return numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float64) / 32768.0


def to_pcm(wave: numpy.ndarray) -> bytes:
    """Обратно в PCM 16 бит, с ограничением по краям.

    Округление обязательно: `astype` отбрасывает дробную часть, то есть тянет
    все значения к нулю. На одном проходе это полшага сетки, но проходов у
    кадра несколько, и такая ошибка **накапливается в одну сторону**.
    """
    return numpy.round(numpy.clip(wave, -1.0, 1.0) * 32767.0).astype(numpy.int16).tobytes()
