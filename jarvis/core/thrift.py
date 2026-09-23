"""Тихий режим: отдать машине память и процессор на время игры.

Просьба владельца 23.09.2026: «надо сделать для него тихий режим, чтобы он ел
мало во время игры». Первым делом это померили, и замер сразу развернул задачу.

| что | сколько |
|---|---|
| процессор | **0.6%** машины |
| память самого ассистента | **1.97 ГБ** (11.7% машины) |
| память открытой панели (Edge) | ещё около гигабайта |

То есть экономить процессор незачем — его и не тратится. Мешает игре **память**,
и рычагов ровно три, по убыванию отдачи: закрыть панель, отпустить местную
модель распознавания, если её будили, и уйти в низкий приоритет, чтобы не
дёргать планировщик на кадрах игры.

**Ничего не выключается насовсем.** Уши, голос и команды работают как работали:
ассистент, который в игре перестаёт отзываться, — это не тихий режим, а
выключенный ассистент. Возвращается всё словами «как обычно» — тем же
выключателем, что и остальные режимы.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

#: Классы приоритета Windows: обычный и пониженный.
NORMAL_PRIORITY = 0x00000020
BELOW_NORMAL_PRIORITY = 0x00004000


def set_priority(*, low: bool) -> bool:
    """Сдвинуть приоритет собственного процесса.

    Не «сделать быстрее игру», а перестать конкурировать с ней за ядра в те
    миллисекунды, когда ассистент всё-таки работает. На других системах просто
    ничего не делаем: `nice` тут не эквивалент, а лишняя зависимость поведения.
    """
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wanted = BELOW_NORMAL_PRIORITY if low else NORMAL_PRIORITY
    done = bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), wanted))
    if not done:
        logger.debug("Приоритет сменить не удалось: ошибка %s", ctypes.get_last_error())
    return done


def own_memory() -> float:
    """Сколько памяти занимает ассистент, в гигабайтах. Ноль — не измерить.

    Нужно не ради отчёта: без числа «до» и «после» тихий режим превращается в
    обещание, а правило проекта требует замера.
    """
    if sys.platform != "win32":
        return 0.0

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not kernel32.K32GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return 0.0
    return counters.WorkingSetSize / 1e9


async def release_recognizer(stt: Any) -> bool:
    """Отпустить местную модель распознавания, если она была поднята.

    Whisper поднимается лениво — при первом отказе облака — и дальше висит в
    памяти до конца сеанса. В игре это полгигабайта впустую: облако работает, а
    он ждёт следующего обрыва. Отпустить его безопасно: понадобится — поднимется
    снова, той же ленью, ценой одной фразы.
    """
    release = getattr(stt, "release_backup", None)
    if release is None:
        return False
    try:
        return bool(await release())
    except Exception as exc:  # noqa: BLE001 — освобождение памяти не важнее работы
        logger.warning("Не смог отпустить местное распознавание: %s", exc)
        return False
