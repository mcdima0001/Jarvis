"""Проверка эхоподавления на живой машине: `python -m jarvis --check-aec`.

Разбирать AEC по логу нельзя: в лог попадает итог, а вопросов на пути четыре, и
каждый способен обнулить остальные. Тот ли сигнал взят за опорный? Доходит ли он
вообще? Попадает ли по времени? И сколько в итоге удаётся убрать? Эта команда
отвечает на все четыре подряд, за десять секунд и без единой модели.

Пользоваться так: включить музыку погромче, **молчать**, запустить. Молчать
важно — меряется, насколько тише стала музыка, и собственный голос в этот
подсчёт войдёт как «неубранное».
"""

from __future__ import annotations

import time
from typing import Any

import numpy

from jarvis.core.config import AudioConfig

from .aec import BLOCK, EchoCanceller, estimate_delay, to_float
from .echo import _KEEP_MS, _MAX_MIC_DELAY_MS


def _mono(block: Any) -> numpy.ndarray:
    """Свести кадр PortAudio к одному каналу в числах."""
    return to_float(bytes(block))


def check_aec(config: AudioConfig, *, seconds: float = 10.0) -> str:
    """Снять микрофон и опорный сигнал одновременно и отчитаться.

    Возвращается готовый текст: команда служебная, её читают глазами.
    """
    lines = ["Проверка эхоподавления", "=" * 22, ""]
    rate = config.sample_rate

    from .devices import _import_sounddevice
    from .loopback import LoopbackSource, describe_outputs

    lines.append("Устройства вывода, с которых можно снять копию звука:")
    for name in describe_outputs():
        lines.append(f"  {name}")
    lines.append("")

    reference: list[numpy.ndarray] = []
    capture = LoopbackSource(
        sample_rate=rate,
        device=None if config.aec.reference in ("auto", "", "off", "none") else config.aec.reference,
        on_audio=reference.append,
    )
    if not capture.start():
        lines += [
            f"Опорный сигнал не открылся: {capture.failure}",
            "",
            "Это и есть причина, по которой AEC ничего не делает. Что проверить:",
            "  * установлен ли пакет:  pip install -e \".[aec]\"",
            "  * тот ли выход стоит в системе по умолчанию — колонки, а не наушники;",
            "  * можно ли назвать устройство прямо: audio.aec.reference в конфиге.",
        ]
        return "\n".join(lines)

    lines.append(f"Опорный сигнал снимается с: {capture.name}")

    sd = _import_sounddevice()
    heard: list[numpy.ndarray] = []
    stream = sd.RawInputStream(
        samplerate=rate,
        blocksize=BLOCK,
        device=config.input_device,
        channels=1,
        dtype="int16",
        callback=lambda data, frames, info, status: heard.append(_mono(data)),
    )
    lines.append(f"Слушаю {seconds:.0f} с. Пусть играет музыка, и лучше помолчать.")
    stream.start()
    time.sleep(seconds)
    stream.stop()
    stream.close()
    capture.stop()

    mic = numpy.concatenate(heard) if heard else numpy.zeros(0)
    played = numpy.concatenate(reference) if reference else numpy.zeros(0)
    lines += ["", f"Записано: микрофон {len(mic) / rate:.1f} с, опора {len(played) / rate:.1f} с"]

    loud = numpy.sqrt(numpy.mean(played**2)) if len(played) else 0.0
    lines.append(f"Громкость опорного сигнала: {loud:.4f}")
    if loud < 1e-4:
        lines += [
            "",
            "Опорный сигнал пустой — значит, снимается не с того выхода.",
            "Проверь, что музыка играет именно на устройстве по умолчанию,",
            "либо назови нужное в audio.aec.reference.",
        ]
        return "\n".join(lines)

    size = min(len(mic), len(played))
    shift, sure = estimate_delay(mic[:size], played[:size], sample_rate=rate)
    lines.append(
        f"Сдвиг между потоками: {shift * 1000 // rate} мс (уверенность {sure:.2f})"
    )
    if sure < 0.5:
        lines.append("  Уверенности мало: похоже, в микрофон эта музыка не попадает вовсе.")

    lines += _drift_report(mic, played, rate)

    # Прогон в тех же условиях, в каких работает конвейер: микрофон придержан
    # ровно на столько, чтобы опора шла впереди на `_KEEP_MS`. Именно это и
    # подбирает `_realign` на живом запуске, и брать тут какое-то своё число
    # значило бы мерить не то, что будет работать.
    delay = min(int(rate * (_KEEP_MS / 1000)) - shift, int(rate * _MAX_MIC_DELAY_MS / 1000))
    delay = max(delay, 0)
    aligned_mic = numpy.concatenate((numpy.zeros(delay), mic))[:size]
    aec = EchoCanceller(sample_rate=rate, tail_ms=config.aec.tail_ms, residual=config.aec.residual)
    out = [
        aec.process(aligned_mic[at : at + BLOCK], played[at : at + BLOCK])
        for at in range(0, size - BLOCK, BLOCK)
    ]
    cleaned = numpy.concatenate(out) if out else numpy.zeros(0)

    # Первую половину фильтр только подбирает тракт — считаем по второй.
    half = len(cleaned) // 2
    was = float(numpy.mean(aligned_mic[half : len(cleaned)] ** 2))
    left = float(numpy.mean(cleaned[half:] ** 2))
    erle = 10.0 * numpy.log10((was + 1e-12) / (left + 1e-12))
    stats = aec.stats()

    lines += [
        "",
        f"Убрано: {erle:.1f} дБ",
        f"Задержка тракта по фильтру: {stats.delay_ms:.0f} мс "
        f"(запас по длине фильтра — {config.aec.tail_ms:.0f} мс)",
        f"Музыка звучала: {stats.active * 100:.0f}% времени",
        "",
    ]
    lines.append(
        f"Микрофон придержан на {delay * 1000 // rate} мс — столько нужно, чтобы опора "
        f"шла впереди на {_KEEP_MS:.0f} мс. Столько же подберёт и живой запуск."
    )
    if erle > 12:
        lines.append("Это хороший результат: музыка станет заметно тише ещё до распознавания.")
    elif erle > 5:
        lines.append(
            "Работает, но небогато. Обычно дело в громкости: на большой колонка "
            "искажает, и часть эха вычесть нельзя в принципе."
        )
    else:
        lines += [
            "Почти ничего. Выравнивание при этом учтено, так что дело не в нём. "
            "Что смотреть дальше:",
            "  * выключены ли «улучшения звука» у микрофона в параметрах Windows — "
            "автоусиление и шумодав драйвера меняют сигнал непредсказуемо и "
            "нелинейно, а после такой обработки вычитать уже нечего. "
            "Это самая частая причина;",
            "  * не слишком ли громко: на большой громкости колонка искажает, и "
            "часть эха линейным вычитанием не описывается вовсе;",
            f"  * хватает ли длины фильтра: audio.aec.tail_ms, сейчас {config.aec.tail_ms:.0f} мс.",
        ]
    return "\n".join(lines)


#: На сколько окон резать запись, чтобы увидеть дрейф. Восемь — чтобы отличить
#: равномерный уезд от разового скачка и увидеть, **где** тот случился: по двум
#: точкам прямую проводит что угодно, включая случайный промах корреляции.
_DRIFT_WINDOWS = 8


def _drift_report(mic: numpy.ndarray, played: numpy.ndarray, rate: int) -> list[str]:
    """Проверить, уезжают ли потоки друг от друга по ходу записи.

    **Главный вопрос всего AEC, и мерить его надо именно так.** Прежний замер
    (`_tell_rates` в `echo.py`) делил число сэмплов на время по часам — и дал
    28.08.2026 «микрофон 16277 Гц» при номинале 16000, то есть +1.7%. Столько
    не врёт ни один кварц: делились разъехавшиеся моменты старта и остановки
    двух потоков, а не частоты.

    Здесь сдвиг ищется в нескольких окнах подряд. Если тракт правда уезжает,
    сдвиг растёт **равномерно**, и наклон этой прямой и есть дрейф; окно
    записи при этом общее, и когда какой поток открылся, роли не играет.
    """
    seconds = len(mic) / rate
    if seconds < 8.0:
        return ["", "Записи мало, чтобы говорить о дрейфе — нужно секунд десять."]

    span = int(len(mic) / _DRIFT_WINDOWS)
    points: list[tuple[float, float, float]] = []
    for index in range(_DRIFT_WINDOWS):
        at = index * span
        piece_mic = mic[at : at + span]
        piece_played = played[at : at + span]
        if min(len(piece_mic), len(piece_played)) < span // 2:
            continue
        # Предел шире обычного: если дрейф есть, к концу записи сдвиг уедет
        # дальше, чем рабочие 500 мс, и упёрся бы в потолок поиска.
        shift, sure = estimate_delay(
            piece_mic, piece_played, sample_rate=rate, max_ms=2000.0
        )
        points.append(((at + span / 2) / rate, shift * 1000 / rate, sure))

    lines = ["", "Сдвиг по ходу записи (так виден дрейф):"]
    for moment, shift_ms, sure in points:
        mark = "" if sure >= 0.3 else "   ← уверенности мало, точка не в счёт"
        lines.append(
            f"  {moment:5.1f} с   {shift_ms:+8.1f} мс   (уверенность {sure:.2f}){mark}"
        )

    solid = [(moment, shift_ms) for moment, shift_ms, sure in points if sure >= 0.3]
    if len(solid) < 3:
        lines.append("")
        lines.append(
            "Точек с уверенным сдвигом мало — про дрейф сказать нечего. Обычно "
            "это значит, что музыка в микрофон почти не попадает."
        )
        return lines

    times = numpy.array([moment for moment, _ in solid])
    shifts = numpy.array([shift_ms for _, shift_ms in solid])

    # Наклон берётся по медиане приращений, а не прямой через все точки.
    # Первый же живой замер (07.09.2026) показал, почему: четыре окна подряд
    # дали -477 мс с разбросом в одну миллисекунду, пятое -17 мс, и прямая
    # через всё это объявила дрейф +15 мс/с — которого нет. Один выброс
    # утаскивает прямую целиком, а медиана его не замечает.
    steps = numpy.diff(shifts) / numpy.diff(times)
    slope = float(numpy.median(steps))
    percent = slope / 10.0  # мс за секунду -> проценты
    spread = float(numpy.max(shifts) - numpy.min(shifts))
    jumps = [
        (times[index + 1], float(steps[index] * (times[index + 1] - times[index])))
        for index in range(len(steps))
        if abs(steps[index] - slope) > 5.0
    ]

    lines += [
        "",
        f"Дрейф: {slope:+.2f} мс/с ({percent:+.3f}%), разброс за запись {spread:.0f} мс",
    ]
    for moment, size in jumps:
        lines.append(
            f"  Скачок к {moment:.0f}-й секунде: {size:+.0f} мс разом — это не дрейф, "
            f"а разрыв в одном из потоков"
        )
    if abs(slope) < 0.05:
        lines.append(
            "Потоки идут в такт — с эхоподавлением дрейф не воюет. Если оно всё "
            "равно ничего не убирает, причина не здесь."
        )
    elif abs(slope) < 0.5:
        lines.append(
            "Дрейф есть, но небольшой: выравнивание съезжает за минуты, а не за "
            "секунды. `_realign` такое догоняет."
        )
    else:
        lines.append(
            "Дрейф большой: тракт уезжает быстрее, чем фильтр успевает подстроиться, "
            "и подтверждение замеров по двум подряд (`_AGREE_MS`) не сработает "
            "никогда. Лечится либо подрезкой опоры по частоте, либо съёмом копии "
            "с другого звена — виртуальные устройства (Voicemeeter) идут по своему "
            "тактовому генератору."
        )
    return lines


def _read_wav(path: Any) -> tuple[numpy.ndarray, int]:
    """Прочитать wav 16 бит. Ничего сложнее нам тут и не нужно."""
    import wave

    with wave.open(str(path)) as source:
        rate = source.getframerate()
        data = source.readframes(source.getnframes())
    wave_ = numpy.frombuffer(data, dtype=numpy.int16)
    return wave_, rate


def check_wakeword(config: Any, spoken: Any, background: Any = None) -> str:
    """Проверить активацию по имени на своих записях.

    Проверяется **собранный по конфигу детектор**, а не отдельно взятая модель:
    в бою работать будет именно он — со своим движком, словарём и выдержкой.
    Поэтому и режим, и движок берутся из `config.yaml`, как при живом запуске.

    Урок Silero тут главный: **отрицательный пример не доказывает ничего**.
    Детектор, который молчит всегда, на тишине и на музыке ведёт себя ровно как
    исправный — и выглядит прекрасно, пока не позовёшь. Поэтому меряется прежде
    всего попадание на записях своего голоса, а ложные срабатывания — вторым.

    :param config: секция ``audio`` из конфига.
    :param spoken: каталог с записями, где имя **произнесено**.
    :param background: каталог с тем, где имени нет: своя музыка, разговоры.
    """
    from pathlib import Path

    from . import _build_wake_word
    from .null import AlwaysActiveWakeWord

    detector = _build_wake_word(config)
    if isinstance(detector, AlwaysActiveWakeWord):
        return (
            "Активация по звуку не поднялась — проверять нечего. Причина в "
            "предупреждении выше: либо audio.wake_word.mode не acoustic, либо "
            "не встал движок. Подробности — docs/wakeword.md."
        )

    lines = [
        "Проверка активации по имени",
        "=" * 27,
        "",
        f"Движок: {type(detector).__name__}, имя: {detector.phrase}",
    ]

    said = sorted(Path(str(spoken)).glob("*.wav")) if spoken else []
    if not said:
        return f"Нет записей в {spoken} — проверять нечего. Их делает record_samples.py"

    heard: list[tuple[str, float]] = []
    missed: list[str] = []
    for path in said:
        at = _first_hit(detector, path, config)
        if at is None:
            missed.append(path.name)
        else:
            heard.append((path.name, at))

    share = len(heard) / len(said) * 100
    lines += ["", f"Записей с именем: {len(said)}, узнано {len(heard)} ({share:.0f}%)"]
    if heard:
        delays = [at for _, at in heard]
        lines.append(
            f"Срабатывает за {min(delays):.2f}–{max(delays):.2f} с от начала записи"
        )
    for name in missed:
        lines.append(f"  не услышал: {name}")

    if background:
        others = sorted(Path(str(background)).glob("*.wav"))
        hours = 0.0
        false = 0
        for path in others:
            wave_, rate = _read_wav(path)
            hours += len(wave_) / rate / 3600
            if _first_hit(detector, path, config) is not None:
                false += 1
        lines += ["", f"Записей без имени: {len(others)}, ложных срабатываний {false}"]
        if hours > 0.01:
            lines.append(f"Это {false / hours:.1f} в час на {hours:.1f} ч фона")

    lines += [
        "",
        "Попадание ниже 80% означает, что звать придётся дважды, и это хуже",
        "ложных: одно ложное срабатывание в пять часов терпимо, одно в десять",
        "минут — нет. Мало попаданий у vosk — проверь, что имя есть в словаре",
        "модели; много ложных — смотри HOLD_MS в wakeword.py.",
    ]
    return "\n".join(lines)


def _first_hit(detector: Any, path: Any, config: Any) -> float | None:
    """На какой секунде записи детектор услышал имя. ``None`` — не услышал.

    Кадры нарезаются той же длины, что приходит с микрофона: выдержка считается
    в миллисекундах, но кадр всё равно неделим, и мерить надо на живой длине.
    """
    from .protocol import AudioFrame

    wave_, rate = _read_wav(path)
    if rate != config.sample_rate:
        wave_, rate = _resampled(path, config.sample_rate)

    detector.reset()
    step = max(1, int(rate * config.frame_ms / 1000))
    for at in range(0, len(wave_) - step, step):
        frame = AudioFrame(data=wave_[at : at + step].tobytes(), sample_rate=rate)
        if detector.detect(frame):
            return (at + step) / rate
    return None


def _resampled(path: Any, rate: int) -> tuple[numpy.ndarray, int]:
    """Привести запись к нужной частоте.

    Отбрасывать сэмплы нельзя — алиасинг рушит и распознавание, и активацию, —
    поэтому берётся тот же честный ресемплер, что и у Whisper.
    """
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(path), sampling_rate=rate)
    return (numpy.clip(audio, -1.0, 1.0) * 32767).astype(numpy.int16), rate
