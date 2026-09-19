"""Блютуз: какое устройство имели в виду — наушники, колонку или по названию."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "skills" / "windows" / "bluetooth.py"
_spec = importlib.util.spec_from_file_location("bluetooth_under_test", _PATH)
assert _spec is not None and _spec.loader is not None
bt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bt
_spec.loader.exec_module(bt)

#: Классы настоящих устройств владельца (замер 19.09.2026).
QCY = bt.Device(name="QCY Melobuds ANC", address=1, connected=True, kind=0x248404)
JBL = bt.Device(name="JBL Flip 6", address=2, connected=False, kind=0x240414)
HK = bt.Device(name="HK GO + PLAY", address=3, connected=False, kind=0x240414)
K55 = bt.Device(name="K55", address=4, connected=False, kind=0x240404)
PHONE = bt.Device(name="A23 пользователь Дима", address=5, connected=False, kind=0x5A020C)


def test_kind_is_read_from_the_device_class() -> None:
    assert bt.is_headphones(QCY.kind) and bt.is_headphones(K55.kind)
    assert bt.is_speaker(JBL.kind) and bt.is_speaker(HK.kind)
    assert not bt.is_headphones(PHONE.kind) and not bt.is_speaker(PHONE.kind)


def test_spoken_kind_picks_devices_of_that_kind() -> None:
    assert bt.category("наушники") == "headphones" and bt.category("колонку") == "speaker"
    assert bt.category("JBL") == ""
    assert bt.by_category("speaker", [QCY, JBL, HK, PHONE]) == [JBL, HK]
    assert bt.by_category("headphones", [QCY, JBL, K55]) == [QCY, K55]


def test_service_guids_are_packed_right() -> None:
    guid = bt._GUID.of(bt.AUDIO_SERVICES[0])
    assert (guid.Data1, guid.Data2, guid.Data3) == (0x0000110B, 0x0000, 0x1000)
    assert bytes(guid.Data4) == bytes.fromhex("800000805f9b34fb")
