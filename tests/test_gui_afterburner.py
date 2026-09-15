"""Разбор общей памяти MSI Afterburner — на синтетической памяти по формату из SDK."""

from __future__ import annotations

import struct

from jarvis.core.gui.afterburner import (
    CPU_TEMPERATURE,
    CPU_USAGE,
    GPU_TEMPERATURE,
    GPU_USAGE,
    SIGNATURE,
    Machine,
    parse,
    read_machine,
)

ENTRY = 1324
FLT_MAX = 3.4028234663852886e38


def _entry(name: str, source: int, value: float, gpu: int = 0) -> bytes:
    raw = bytearray(ENTRY)
    raw[: len(name)] = name.encode()
    struct.pack_into("<3f3I", raw, 1300, value, 0.0, 100.0, 0, gpu, source)
    return bytes(raw)


def _memory(*entries: bytes, signature: int = SIGNATURE) -> bytes:
    # signature, version, headerSize, numEntries, entrySize, time32, numGpuEntries, gpuEntrySize
    header = struct.pack("<5Ii2I", signature, 0x20000, 32, len(entries), ENTRY, 0, 0, 0)
    return header + b"".join(entries)


def test_reads_total_cpu_and_first_gpu() -> None:
    memory = _memory(
        _entry("CPU1 usage", CPU_USAGE, 90.0),
        _entry("CPU usage", CPU_USAGE, 23.4),
        _entry("CPU temperature", CPU_TEMPERATURE, 61.0),
        _entry("GPU usage", GPU_USAGE, 40.0, gpu=1),
        _entry("GPU usage", GPU_USAGE, 12.0, gpu=0),
        _entry("GPU temperature", GPU_TEMPERATURE, 48.0),
    )
    assert parse(memory) == Machine(cpu=23.4, cpu_temp=61.0, gpu=12.0, gpu_temp=48.0)


def test_without_totals_cores_are_combined() -> None:
    memory = _memory(
        _entry("CPU1 usage", CPU_USAGE, 10.0),
        _entry("CPU2 usage", CPU_USAGE, 30.0),
        _entry("CPU1 temperature", CPU_TEMPERATURE, 55.0),
        _entry("CPU2 temperature", CPU_TEMPERATURE, 70.0),
    )
    assert parse(memory) == Machine(cpu=20.0, cpu_temp=70.0)


def test_missing_values_are_skipped() -> None:
    memory = _memory(_entry("GPU temperature", GPU_TEMPERATURE, FLT_MAX), _entry("CPU usage", CPU_USAGE, 5.0))
    assert parse(memory) == Machine(cpu=5.0)


def test_unloading_or_empty_memory_is_no_data() -> None:
    assert parse(_memory(_entry("CPU usage", CPU_USAGE, 5.0), signature=0xDEAD)) is None
    assert parse(_memory()) is None
    assert parse(b"") is None


def test_reading_without_afterburner_does_not_fail() -> None:
    assert read_machine() is None or isinstance(read_machine(), Machine)
