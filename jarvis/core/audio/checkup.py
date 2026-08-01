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
from .echo import _KEEP_MS, _MIC_DELAY_MS


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

    # Прогон в тех же условиях, в каких работает конвейер: микрофон придержан,
    # опора идёт впереди. Иначе замер показал бы не то, что будет вживую.
    delay = int(rate * _MIC_DELAY_MS / 1000)
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
    if erle > 12:
        lines.append("Это хороший результат: музыка станет заметно тише ещё до распознавания.")
    elif erle > 5:
        lines.append(
            "Работает, но небогато. Обычно дело в громкости: на большой колонка "
            "искажает, и часть эха вычесть нельзя в принципе."
        )
    else:
        lines += [
            "Почти ничего. По порядку, что смотреть:",
            f"  * сдвиг {shift * 1000 // rate} мс — если он больше {_MIC_DELAY_MS:.0f} мс "
            f"или отрицательный, потоки разъезжаются;",
            "  * выключены ли «улучшения звука» у микрофона в параметрах Windows — "
            "автоусиление и шумодав драйвера меняют сигнал непредсказуемо, "
            "и вычитать после них нечего;",
            f"  * хватает ли длины фильтра: audio.aec.tail_ms, сейчас {config.aec.tail_ms:.0f} мс.",
        ]
    lines.append(
        f"Для справки: конвейер придерживает микрофон на {_MIC_DELAY_MS:.0f} мс "
        f"и держит опору впереди на {_KEEP_MS:.0f} мс."
    )
    return "\n".join(lines)
