"""Датчики машины из MSI Afterburner: загрузка и температура процессора и видеокарты.

Свой счётчик (`Meter`) знает только сам Jarvis, и в долях одного ядра: на
главной это выглядело как «пик 140%» без ответа на вопрос, что происходит с
машиной. Afterburner у владельца стоит и так и уже опрашивает датчики — читать
его выходит дешевле и честнее, чем заводить свой опрос температур.

**Читается его общая память `MAHMSharedMemory`** — формат описан в SDK
(`SDK/Include/MAHMSharedMemory.h`), зависимостей ноль. Память **открывается, но
не создаётся**: `mmap` с именем создал бы её сам, и запущенный следом
Afterburner нашёл бы имя занятым пустышкой.

Afterburner не запущен — `None`, и график остаётся на своём счётчике.
"""

from __future__ import annotations

import ctypes
import math
import struct
import sys
from dataclasses import asdict, dataclass
from typing import Any

MAPPING = "MAHMSharedMemory"
#: 'MAHM' — память заполнена; 0xDEAD — Afterburner выгружается.
SIGNATURE = 0x4D41484D

#: Номера источников из SDK (`MONITORING_SOURCE_ID_*`).
GPU_TEMPERATURE = 0x00
GPU_USAGE = 0x30
CPU_TEMPERATURE = 0x80
CPU_USAGE = 0x90

_NAME = 260  # MAX_PATH
#: Где в записи начинаются числа: пять строк по MAX_PATH.
_NUMBERS = _NAME * 5
#: data, minLimit, maxLimit (float) и dwFlags, dwGpu, dwSrcId (DWORD).
_ENTRY_MIN = _NUMBERS + 24
#: Нет значения — Afterburner пишет FLT_MAX.
_MISSING = 1e30
#: Больше этого памяти у монитора не бывает; иначе заголовок испорчен.
_LIMIT = 16 << 20

_FILE_MAP_READ = 0x0004


@dataclass(frozen=True, slots=True)
class Machine:
    """Показания за один опрос; ``None`` — такого датчика у Afterburner нет."""

    cpu: float | None = None
    cpu_temp: float | None = None
    gpu: float | None = None
    gpu_temp: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


def parse(memory: bytes) -> Machine | None:
    """Разобрать содержимое общей памяти. ``None`` — данных нет или они не те."""
    if len(memory) < 20:
        return None
    signature, _, header_size, count, entry_size = struct.unpack_from("<5I", memory, 0)
    if signature != SIGNATURE or entry_size < _ENTRY_MIN:
        return None
    found: dict[int, list[tuple[str, int, float]]] = {}
    for index in range(count):
        offset = header_size + index * entry_size
        if offset + _ENTRY_MIN > len(memory):
            break
        (value,) = struct.unpack_from("<f", memory, offset + _NUMBERS)
        gpu, source = struct.unpack_from("<2I", memory, offset + _NUMBERS + 16)
        if not math.isfinite(value) or abs(value) >= _MISSING:
            continue
        name = memory[offset:offset + _NAME].split(b"\0", 1)[0].decode("ascii", "replace")
        found.setdefault(source, []).append((name, gpu, value))

    machine = Machine(
        cpu=_total(found.get(CPU_USAGE, []), "CPU usage", combine=_mean),
        cpu_temp=_total(found.get(CPU_TEMPERATURE, []), "CPU temperature", combine=max),
        gpu=_first_gpu(found.get(GPU_USAGE, [])),
        gpu_temp=_first_gpu(found.get(GPU_TEMPERATURE, [])),
    )
    if machine == Machine():
        return None
    return machine


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _total(entries: list[tuple[str, int, float]], name: str, *, combine: Any) -> float | None:
    """Общее значение, а без него — сведённое по ядрам («CPU1 usage», «CPU2 usage»…)."""
    if not entries:
        return None
    for entry_name, _, value in entries:
        if entry_name == name:
            return round(value, 1)
    return round(float(combine([value for _, _, value in entries])), 1)


def _first_gpu(entries: list[tuple[str, int, float]]) -> float | None:
    """Первая видеокарта: у ноутбука их бывает две, и основной считается нулевая."""
    if not entries:
        return None
    return round(min(entries, key=lambda entry: entry[1])[2], 1)


def read_machine() -> Machine | None:
    """Прочитать датчики сейчас. ``None`` — не Windows или Afterburner не запущен."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenFileMappingW.restype = ctypes.c_void_p
    kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.OpenFileMappingW(_FILE_MAP_READ, 0, MAPPING)
    if not handle:
        return None
    try:
        view = kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, 0)
        if not view:
            return None
        try:
            head = ctypes.string_at(view, 20)
            signature, _, header_size, count, entry_size = struct.unpack("<5I", head)
            if signature != SIGNATURE:
                return None
            size = header_size + count * entry_size
            if size > _LIMIT:
                return None
            return parse(ctypes.string_at(view, size))
        finally:
            kernel32.UnmapViewOfFile(view)
    finally:
        kernel32.CloseHandle(handle)
