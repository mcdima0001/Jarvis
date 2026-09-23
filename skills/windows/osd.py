"""Оверлей RivaTuner (OSD): включить и выключить без горячих клавиш.

Владелец спросил 23.09.2026, где назначать клавишу для «Show On-Screen
Display», — и оказалось, что назначать её незачем. У RTSS есть своя библиотека
`RTSSHooks64.dll`, и в ней ровно то, что нужно: `GetFlags` и `SetFlags`. Младший
бит флагов — та самая галочка в его окне.

Проверено на живой машине владельца (RTSS 7.3.7): флаги были `0x0` (оверлей
выключен), `SetFlags` включил, `GetFlags` подтвердил, обратная установка вернула
как было.

**Почему это лучше клавиши.** Горячая клавиша — это переключатель: одна
пропущенная нажатием осечка, и дальше всё наоборот, «включить» выключает. Здесь
состояние **читается**, поэтому «включи оверлей» включает его и тогда, когда он
уже включён, а протокол на выходе возвращает ровно то, что было до игры.

Библиотека грузится лениво и только на Windows; нет RTSS — скилл не ломается,
команда честно отвечает «не нашёл RivaTuner».
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Где RTSS лежит по умолчанию. Свой путь задаётся настройкой скилла.
RTSS_DLL = Path(r"C:\Program Files (x86)\RivaTuner Statistics Server\RTSSHooks64.dll")

#: Младший бит флагов RTSS — видимость оверлея.
OSD_VISIBLE = 0x1

#: Загруженная библиотека: грузим один раз на сеанс.
_LIBRARY: Any = None
_TRIED = False


def library(path: Path = RTSS_DLL) -> Any:
    """Библиотека RTSS или ``None``, если её нет."""
    global _LIBRARY, _TRIED
    if _TRIED:
        return _LIBRARY
    _TRIED = True
    if sys.platform != "win32" or not path.exists():
        return None
    import ctypes

    try:
        dll = ctypes.WinDLL(str(path))
        dll.GetFlags.restype = ctypes.c_uint32
        dll.SetFlags.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    except OSError as exc:
        logger.warning("RTSS есть, но не загрузился: %s", exc)
        return None
    _LIBRARY = dll
    return dll


def visible(path: Path = RTSS_DLL) -> bool | None:
    """Показан ли сейчас оверлей. ``None`` — RTSS не запущен или не найден."""
    dll = library(path)
    if dll is None:
        return None
    try:
        return bool(dll.GetFlags() & OSD_VISIBLE)
    except OSError as exc:  # noqa: BLE001 — RTSS могли закрыть между вызовами
        logger.debug("RTSS не ответил: %s", exc)
        return None


def show(on: bool, path: Path = RTSS_DLL) -> bool:
    """Включить или выключить оверлей. ``False`` — не вышло.

    Состояние задаётся, а не переключается: переключатель рассинхронизируется на
    первой же осечке, и дальше «включить» начинает выключать.
    """
    dll = library(path)
    if dll is None:
        return False
    try:
        # Первый аргумент — маска «что оставить», второй — «что перевернуть»:
        # так у RTSS задаётся один бит, не трогая остальные.
        dll.SetFlags(~OSD_VISIBLE & 0xFFFFFFFF, OSD_VISIBLE if on else 0)
        return visible(path) is on
    except OSError as exc:  # noqa: BLE001
        logger.warning("RTSS не принял команду: %s", exc)
        return False
