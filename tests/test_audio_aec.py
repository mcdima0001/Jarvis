"""Проверка эхоподавления: на синтетическом эхе и на настоящей речи.

Урок Silero тут учтён с самого начала: **отрицательный пример не проверяет
ничего**. Сломанный AEC на тишине выдаёт тишину, а на белом шуме — шум, ровно
как исправный. Поэтому все проверки ниже устроены одинаково: берётся сигнал,
портится известным способом, и меряется, сколько порчи удалось убрать. Число в
децибелах либо есть, либо его нет.

Речь синтезируется формулой, а не читается из файла: тест обязан идти на любой
машине без сети. Формантная модель звучит нехорошо, но у неё есть всё, что
важно для проверки, — переменный спектр, паузы и структура во времени.
"""

from __future__ import annotations

import numpy
import pytest

from jarvis.core.audio.aec import (
    BLOCK,
    EchoCanceller,
    HighPass,
    estimate_delay,
    to_float,
    to_pcm,
)

RATE = 16000


def _rng() -> numpy.random.Generator:
    """Свой генератор с постоянным зерном: тест обязан быть повторяемым."""
    return numpy.random.default_rng(7)


def _speech(samples: int) -> numpy.ndarray:
    """Похоже на речь: меняющиеся форманты, слоги и паузы между словами."""
    rng = _rng()
    time = numpy.arange(samples) / RATE
    tone = 110.0 + 20.0 * numpy.sin(2.0 * numpy.pi * 1.7 * time)
    phase = 2.0 * numpy.pi * numpy.cumsum(tone) / RATE
    wave = numpy.zeros(samples)
    for number in (1, 2, 3, 5, 8, 13):
        wave += numpy.sin(number * phase) / number
    # Слоги: огибающая с провалами, плюс паузы между словами.
    syllables = numpy.clip(numpy.sin(2.0 * numpy.pi * 4.0 * time), 0.0, None)
    words = (numpy.sin(2.0 * numpy.pi * 0.4 * time) > -0.3).astype(float)
    return wave * syllables * words * 0.12 + rng.normal(0, 1e-4, samples)


def _music(samples: int) -> numpy.ndarray:
    """Похоже на музыку: бас с гармониками и удары."""
    rng = _rng()
    time = numpy.arange(samples) / RATE
    wave = numpy.zeros(samples)
    for hertz, level in ((55, 0.9), (110, 0.5), (220, 0.3), (440, 0.15), (1760, 0.05)):
        wave += level * numpy.sin(2.0 * numpy.pi * hertz * time + rng.uniform(0, 6))
    beats = (numpy.sin(2.0 * numpy.pi * 2.0 * time) > 0.9) * rng.normal(0, 1, samples) * 0.4
    return (wave + beats) / 4.0


def _room(delay_ms: float = 50.0, tail_ms: float = 90.0) -> numpy.ndarray:
    """Путь от колонки до микрофона: задержка плюс затухающие отражения."""
    rng = _rng()
    delay = int(delay_ms * RATE / 1000)
    tail = int(tail_ms * RATE / 1000)
    answer = numpy.zeros(delay + tail)
    answer[delay] = 1.0
    answer[delay:] += rng.normal(0, 1, tail) * numpy.exp(-numpy.arange(tail) / (0.02 * RATE)) * 0.5
    return answer * 0.5


def _run(mic: numpy.ndarray, reference: numpy.ndarray, **options) -> tuple[numpy.ndarray, EchoCanceller]:
    """Прогнать пару потоков через фильтр блок за блоком."""
    aec = EchoCanceller(sample_rate=RATE, **options)
    pieces = [
        aec.process(mic[at : at + BLOCK], reference[at : at + BLOCK])
        for at in range(0, len(mic) - BLOCK, BLOCK)
    ]
    # Задержка спектральной ступени снимается сразу: сравнивать выход со входом
    # иначе бессмысленно — сдвиг в блок рушит любое сравнение до неузнаваемости.
    return numpy.concatenate(pieces)[aec.latency :], aec


def _clarity(out: numpy.ndarray, speech: numpy.ndarray) -> float:
    """Насколько речь громче всего остального, в децибелах.

    Мера **не зависит от общей громкости**: из выхода вычитается наилучшая по
    методу наименьших квадратов копия чистой речи, и остаток сравнивается с
    ней. Иначе подавитель, который просто сделал тише всё сразу, выглядел бы
    испортившим сигнал, хотя для распознавания громкость не значит ничего.
    """
    size = min(len(out), len(speech))
    out, speech = out[:size], speech[:size]
    level = float(numpy.dot(out, speech)) / (float(numpy.dot(speech, speech)) + 1e-12)
    rest = out - level * speech
    kept = level**2 * float(numpy.dot(speech, speech))
    return 10.0 * numpy.log10((kept + 1e-12) / (float(numpy.dot(rest, rest)) + 1e-12))


def _erle(mic: numpy.ndarray, out: numpy.ndarray) -> float:
    """Насколько тише стало, в децибелах."""
    size = min(len(mic), len(out))
    return 10.0 * numpy.log10(
        (float(numpy.mean(mic[:size] ** 2)) + 1e-12) / (float(numpy.mean(out[:size] ** 2)) + 1e-12)
    )


@pytest.fixture(scope="module")
def scene() -> dict[str, numpy.ndarray]:
    """Двадцать секунд: играет музыка, человек говорит поверх неё."""
    samples = 20 * RATE
    music = _music(samples)
    speech = _speech(samples)
    echo = numpy.convolve(music, _room())[:samples]
    return {"music": music, "speech": speech, "echo": echo, "mic": echo + speech}


def test_speech_comes_out_from_under_the_music(scene) -> None:
    """Главное число: насколько лучше стало слышно человека.

    Речь в записи на два десятка децибел тише музыки — это и есть та громкость,
    при которой ассистент «вообще не откликается».
    """
    settle = slice(5 * RATE, None)
    out, _ = _run(scene["mic"], scene["music"])

    before = _clarity(scene["mic"][settle], scene["speech"][settle])
    after = _clarity(out[settle], scene["speech"][settle])

    assert before < -10.0, f"сцена слишком лёгкая: {before:.1f} дБ"
    assert after - before > 15.0, f"улучшение всего {after - before:.1f} дБ"


def test_nothing_playing_means_nothing_changed(scene) -> None:
    """Музыка не играет — микрофон обязан пройти насквозь без изменений.

    Проверка не формальная: фильтр работает **всегда**, и если в тишине он
    что-нибудь портит, то портит это каждую команду, сказанную в тишине, то
    есть большинство.
    """
    silence = numpy.zeros(len(scene["speech"]))
    out, aec = _run(scene["speech"], silence)

    assert numpy.allclose(out, scene["speech"][: len(out)], atol=2e-4)
    assert aec.stats().active == 0.0, "в тишине обучаться нечему"


def test_residual_stage_never_makes_it_worse(scene) -> None:
    """Спектральная ступень обязана быть не хуже своего отсутствия.

    Первая версия задавала утечку константой (0.4) и на чистом тракте резала
    полосы, где эха давно не было: −8 дБ из честно заработанных двадцати.
    Поэтому утечка измеряется, и проверка стоит тестом.
    """
    settle = slice(5 * RATE, None)
    plain, _ = _run(scene["mic"], scene["music"], residual=False)
    full, _ = _run(scene["mic"], scene["music"], residual=True)

    without = _clarity(plain[settle], scene["speech"][settle])
    with_it = _clarity(full[settle], scene["speech"][settle])

    assert with_it >= without - 0.5, f"со ступенью {with_it:.1f} дБ против {without:.1f} дБ"


def test_distortion_costs_but_does_not_break(scene) -> None:
    """На громкости колонка искажает, и часть эха вычесть нельзя в принципе.

    Проверяем не «сколько убрали», а что выигрыш остаётся заметным и **не
    уходит в минус** даже на дикой перегрузке: линейным преобразованием такое
    эхо не описывается, и ждать тут прежних двадцати децибел не приходится.
    Шкала на стенде вышла такая: перегрузка вдвое — плюс 15 дБ, втрое — 11,
    впятеро — 6, ввосьмеро — 3.5.
    """
    settle = slice(5 * RATE, None)
    for drive, least in ((3.0, 8.0), (8.0, 2.0)):
        driven = numpy.tanh(scene["music"] * drive) / drive
        mic = numpy.convolve(driven, _room())[: len(scene["music"])] + scene["speech"]
        out, _ = _run(mic, scene["music"])

        before = _clarity(mic[settle], scene["speech"][settle])
        after = _clarity(out[settle], scene["speech"][settle])

        assert after - before > least, f"перегрузка {drive}: всего {after - before:.1f} дБ"


def test_our_own_ducking_does_not_disturb_the_filter(scene) -> None:
    """Приглушение по сессиям Windows попадает и в опорный сигнал.

    Поэтому путь от колонки до микрофона не меняется, и фильтру всё равно.
    Это важно знать точно: приглушение случается на **каждую** команду.
    """
    samples = len(scene["music"])
    quieter = numpy.ones(samples)
    quieter[10 * RATE :] = 0.1
    out, aec = _run(scene["echo"] * quieter, scene["music"] * quieter)

    after = slice(12 * RATE, None)
    assert _erle(scene["echo"][after] * 0.1, out[after]) > 15.0
    assert aec.stats().rescales == 0, "пересобирать фильтр тут не из-за чего"


def test_recovers_when_the_speaker_knob_moves(scene) -> None:
    """Громкость крутят на самой колонке — опора прежняя, эхо тише.

    Тогда фильтр вычитает больше, чем есть, и делает **хуже, чем если бы его
    не было**. Без поправки усиления на стенде это −7 дБ и десяток секунд на
    переучивание; с поправкой — одна секунда.
    """
    samples = len(scene["music"])
    quieter = numpy.ones(samples)
    quieter[10 * RATE :] = 0.1
    out, aec = _run(scene["echo"] * quieter, scene["music"])

    recovered = slice(12 * RATE, None)
    assert aec.stats().rescales > 0, "поломка не замечена"
    assert _erle(scene["echo"][recovered] * 0.1, out[recovered]) > 8.0


def test_delay_between_the_streams_is_found(scene) -> None:
    """Опорный поток и микрофон не совпадают по времени, и это надо видеть.

    Сравнение по огибающим на ровном ритме ошибалось на 60 мс — у музыки вся
    мощность в басу, и корреляция даёт холм по периоду бита. С выравниванием
    спектра пик приходится ровно на задержку.
    """
    shift, sure = estimate_delay(scene["echo"], scene["music"], sample_rate=RATE)

    assert abs(shift - int(0.05 * RATE)) < BLOCK, f"нашлось {shift} сэмплов"
    assert sure > 0.5, f"уверенность всего {sure:.2f}"


def test_delay_of_silence_is_not_invented() -> None:
    """Сравнивать нечего — уверенность обязана быть нулевой, а не случайной."""
    quiet = numpy.zeros(RATE)

    assert estimate_delay(quiet, quiet, sample_rate=RATE) == (0, 0.0)


def test_high_pass_keeps_speech_and_drops_rumble() -> None:
    """Саб на полу приходит в корпус ноутбука через стол, а не по воздуху.

    Такой путь вычесть вместе с остальным эхом нельзя, зато он весь внизу —
    там, где речи нет.
    """
    time = numpy.arange(RATE) / RATE
    settled = slice(RATE // 4, None)
    # Тона проверяются по отдельности: у фильтра со свёрткой выход сдвинут во
    # времени, и вычитать из него неcдвинутый эталон бессмысленно.
    rumble = HighPass(150.0, RATE).process(numpy.sin(2.0 * numpy.pi * 45.0 * time))
    voice = HighPass(150.0, RATE).process(numpy.sin(2.0 * numpy.pi * 900.0 * time))

    assert _erle(numpy.sin(2.0 * numpy.pi * 45.0 * time)[settled], rumble[settled]) > 20.0
    assert abs(_erle(numpy.sin(2.0 * numpy.pi * 900.0 * time)[settled], voice[settled])) < 1.0


def test_high_pass_survives_the_seam_between_blocks() -> None:
    """Кусками и целиком должно получаться одно и то же."""
    wave = _speech(4 * BLOCK)
    whole = HighPass(150.0, RATE).process(wave)
    parted = HighPass(150.0, RATE)
    pieces = numpy.concatenate([parted.process(wave[at : at + BLOCK]) for at in range(0, len(wave), BLOCK)])

    assert numpy.allclose(whole, pieces)


def test_pcm_survives_the_round_trip() -> None:
    """Перевод в числа и обратно не должен ничего терять сверх шага сетки."""
    wave = _speech(BLOCK)

    assert numpy.allclose(to_float(to_pcm(wave)), wave, atol=1.0 / 32767)


def test_muted_microphone_does_not_unlearn_the_filter() -> None:
    """Выключенный кнопкой микрофон не должен стирать подобранный тракт.

    Он отдаёт **цифровую тишину** — ровные нули, а не тихий шум комнаты. Учиться
    на таком блоке нельзя: наилучшее приближение к нулю есть нулевой фильтр, то
    есть за минуту с выключенным микрофоном тракт разучивается начисто, а после
    включения его приходится собирать заново. Владелец глушит микрофон
    регулярно, так что случай рядовой, а не экзотика.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE * 2)
    room = numpy.array([0.0] * 32 + [0.7, -0.3, 0.1])
    echo = numpy.convolve(music, room)[: len(music)]
    for at in range(0, len(music) - BLOCK, BLOCK):
        canceller.process(echo[at : at + BLOCK], music[at : at + BLOCK])

    learned = float(numpy.abs(canceller._weights).sum())
    assert learned > 0

    silence = numpy.zeros(BLOCK)
    for at in range(0, len(music) - BLOCK, BLOCK):
        canceller.process(silence, music[at : at + BLOCK])

    assert float(numpy.abs(canceller._weights).sum()) == pytest.approx(learned, rel=1e-9)
    assert canceller.stats().muted > 0.4, "доля молчания обязана попасть в отчёт"


def test_quiet_room_is_not_mistaken_for_a_muted_microphone() -> None:
    """Тихая комната — не выключенный микрофон, и учиться там как раз полезнее.

    Порог берётся ниже младшего бита 16-битной записи, поэтому настоящий, пусть
    и очень тихий, звук под него не попадает.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE)
    # Настоящий микрофон тише собственного шума не бывает: у 16-битной записи
    # младший бит это 3e-5, и он всегда шевелится. Ровные нули приходят только
    # от выключенного микрофона — на этом различие и держится.
    quiet = _speech(RATE) * 0.02 + _rng().normal(0, 1e-4, RATE)
    for at in range(0, len(music) - BLOCK, BLOCK):
        canceller.process(quiet[at : at + BLOCK], music[at : at + BLOCK])

    assert canceller.stats().muted == 0.0
    assert float(numpy.abs(canceller._weights).sum()) > 0, "в тихой комнате учимся"


def test_step_is_normalised_by_what_is_in_the_filter(caplog) -> None:
    """Опора то громкая, то тихая — фильтр обязан это переживать молча.

    Живой запуск 01.08.2026: **184 обнуления за восемнадцать секунд**, лог
    состоял из одной строки. Причина была в знаменателе шага: он считался как
    «мощность текущего блока × число разделов», а это верно, только пока
    громкость опоры ровная. Стоит ей упасть — выключили звук, пропал буфер,
    пауза между треками, — как в истории остаются громкие куски, а знаменатель
    берётся по тихому блоку, и шаг вырастает во столько же раз.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE * 8)
    # Опора моргает: две секунды играет, полсекунды почти нет. Так выглядит и
    # выключение звука кнопкой, и пропуск в петлевом захвате.
    reference = music.copy()
    for start in range(RATE * 2, len(music), RATE * 2):
        reference[start : start + RATE // 2] *= 1e-3
    mic = numpy.convolve(reference, _room())[: len(music)] + _speech(len(music)) * 0.1

    with caplog.at_level("WARNING"):
        out = numpy.concatenate([
            canceller.process(mic[at : at + BLOCK], reference[at : at + BLOCK])
            for at in range(0, len(mic) - BLOCK, BLOCK)
        ])

    assert numpy.isfinite(out).all()
    assert canceller.stats().blowups == 0, (
        f"фильтр разлетается на провалах опоры: {canceller.stats().blowups} раз"
    )
    # И при этом он всё-таки работает, а не просто не мешает.
    half = len(out) // 2
    erle = _erle(mic[half : len(out)], out[half:])
    assert erle > 10.0, f"убрано всего {erle:.1f} дБ"


def test_divergence_is_reported_once_not_every_block(caplog) -> None:
    """Строка о поломке, повторённая двести раз, о поломке уже не сообщает.

    Владелец выключил Jarvis через полминуты со словами «начался дикий спам» —
    и был прав: за предупреждениями перестало быть видно всё остальное.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE)
    canceller._weights.fill(1e6)  # заведомо разлетевшийся фильтр

    with caplog.at_level("WARNING", logger="jarvis.core.audio.aec"):
        for at in range(0, len(music) - BLOCK, BLOCK):
            canceller.process(music[at : at + BLOCK] * 0.01, music[at : at + BLOCK])

    told = caplog.text.count("разошёлся")
    assert told <= 2, f"жалоб за секунду: {told}"


def test_quiet_reference_does_not_blow_the_filter_up(caplog) -> None:
    """Тихая опора при громком микрофоне не должна рвать фильтр.

    Живой лог 01.08.2026: «убрано −369.1 дБ». Число не плохое, а бессмысленное:
    фильтр улетел в бесконечность. Механика — шаг NLMS делится на мощность
    опоры, и на почти нулевой мощности вырастает на порядки. А почти нулевой
    она бывает постоянно: владелец выключает звук, петлевой захват пропускает
    буфер, между треками пауза. Микрофон при этом громкий — в него дует
    вентилятор.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE)
    room = _room()
    echo = numpy.convolve(music, room)[: len(music)]
    for at in range(0, len(music) - BLOCK, BLOCK):
        canceller.process(echo[at : at + BLOCK], music[at : at + BLOCK])

    # Опора почти пропала, а комната шумит как прежде: ровно тот случай.
    faint = music * 1e-3
    noise = _speech(RATE) + _rng().normal(0, 0.02, RATE)
    with caplog.at_level("WARNING"):
        out = numpy.concatenate([
            canceller.process(noise[at : at + BLOCK], faint[at : at + BLOCK])
            for at in range(0, len(noise) - BLOCK, BLOCK)
        ])

    assert numpy.isfinite(out).all(), "выход обязан оставаться числом"
    loudest = float(numpy.max(numpy.abs(out)))
    assert loudest < 100.0, f"выход разлетелся: пик {loudest}"


def test_runaway_filter_is_zeroed_not_left_to_grow() -> None:
    """Оценка эха на порядки громче микрофона — это разлёт, а не громкий трек.

    Настоящее эхо состоит из того, что услышал микрофон, и вдесятеро громче
    него не бывает. Поправка усиления такое не лечит: она сработала бы через
    полсекунды, а к тому времени следующие шаги уже умножат беду.
    """
    canceller = EchoCanceller(sample_rate=RATE, tail_ms=200, residual=False)
    music = _music(RATE // 2)
    for at in range(0, len(music) - BLOCK, BLOCK):
        canceller.process(music[at : at + BLOCK] * 0.5, music[at : at + BLOCK])

    # Подделываем разлетевшийся фильтр: коэффициенты в тысячу раз больше.
    canceller._weights *= 1000.0
    quiet = numpy.full(BLOCK, 1e-3)
    canceller.process(quiet, music[:BLOCK])

    assert canceller.stats().blowups == 1
    assert float(numpy.abs(canceller._weights).sum()) == 0.0
