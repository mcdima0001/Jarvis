"""Блютуз голосом: включить и выключить радио, подключить и отключить устройство.

Зависимостей ноль. Устройства — через `bthprops.cpl` (Bluetooth API Windows,
ctypes): список сопряжённых, а подключение и отключение — включением и
выключением их служб. Так делает и сама Windows по кнопке «Подключить»: у
наушников и колонок подключение и есть включённая служба «приёмник звука»
(A2DP) и «гарнитура» (HFP). Выключили службы — устройство отключилось;
включили — подключилось снова. Сопряжение при этом не теряется.

Радио — через WinRT (`Windows.Devices.Radios`) из Windows PowerShell: у
классического API выключателя радио нет вовсе. Секунда-две на процесс — для
команды, которую дают раз в день, это приемлемо.

Новые устройства тут не сопрягаются: для этого нужен код с экрана устройства
или подтверждение на нём, голосом этого не сделать. Сопряжённое один раз —
подключается и отключается голосом сколько угодно.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Device:
    """Сопряжённое устройство."""

    name: str
    address: int
    connected: bool
    #: Класс устройства (CoD): по нему видно, звуковое ли оно.
    kind: int = 0


#: Службы, которые делают звуковое устройство подключённым: приёмник звука
#: (A2DP), гарнитура с громкой связью (HFP), гарнитура (HSP), пульт (AVRCP).
AUDIO_SERVICES = (
    "0000110b-0000-1000-8000-00805f9b34fb",
    "0000111e-0000-1000-8000-00805f9b34fb",
    "00001108-0000-1000-8000-00805f9b34fb",
    "0000110e-0000-1000-8000-00805f9b34fb",
)
#: Служба «устройство ввода» — мыши, клавиатуры, геймпады.
HID_SERVICE = "00001124-0000-1000-8000-00805f9b34fb"
_ENABLE, _DISABLE = 1, 0


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, wintypes.WORD) for name in (
        "wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds",
    )]


class _DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", _SYSTEMTIME),
        ("stLastUsed", _SYSTEMTIME),
        ("szName", ctypes.c_wchar * 248),
    ]


class _SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueInquiry", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", ctypes.c_void_p),
    ]


class _RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD)]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        value = uuid.UUID(text)
        return cls(value.time_low, value.time_mid, value.time_hi_version, (ctypes.c_ubyte * 8)(*value.bytes[8:]))


class BluetoothError(Exception):
    """Блютуз недоступен или отказал — текст для человека."""


def _api() -> Any:
    if sys.platform != "win32":
        raise BluetoothError("Блютуз умею только в Windows.")
    try:
        api = ctypes.WinDLL("bthprops.cpl", use_last_error=True)
    except OSError as exc:
        raise BluetoothError("В системе нет библиотеки Bluetooth.") from exc
    api.BluetoothFindFirstRadio.restype = ctypes.c_void_p
    api.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(_RADIO_PARAMS), ctypes.POINTER(ctypes.c_void_p)]
    api.BluetoothFindRadioClose.argtypes = [ctypes.c_void_p]
    api.BluetoothFindFirstDevice.restype = ctypes.c_void_p
    api.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(_SEARCH_PARAMS), ctypes.POINTER(_DEVICE_INFO)]
    api.BluetoothFindNextDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DEVICE_INFO)]
    api.BluetoothFindDeviceClose.argtypes = [ctypes.c_void_p]
    api.BluetoothSetServiceState.restype = wintypes.DWORD
    api.BluetoothSetServiceState.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DEVICE_INFO), ctypes.POINTER(_GUID), wintypes.DWORD,
    ]
    return api


def _radio(api: Any) -> int:
    """Дескриптор первого радиомодуля; его нужно закрыть `CloseHandle`."""
    params = _RADIO_PARAMS(ctypes.sizeof(_RADIO_PARAMS))
    radio = ctypes.c_void_p()
    finder = api.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not finder:
        raise BluetoothError("Блютуз выключен или его нет.")
    api.BluetoothFindRadioClose(finder)
    return int(radio.value or 0)


def _close(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(handle)


def _infos(api: Any, radio: int) -> list[_DEVICE_INFO]:
    params = _SEARCH_PARAMS(
        ctypes.sizeof(_SEARCH_PARAMS), True, True, False, True, False, 0, radio,
    )
    info = _DEVICE_INFO()
    info.dwSize = ctypes.sizeof(_DEVICE_INFO)
    found: list[_DEVICE_INFO] = []
    finder = api.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not finder:
        return found
    try:
        while True:
            copy = _DEVICE_INFO()
            ctypes.pointer(copy)[0] = info
            found.append(copy)
            info = _DEVICE_INFO()
            info.dwSize = ctypes.sizeof(_DEVICE_INFO)
            if not api.BluetoothFindNextDevice(finder, ctypes.byref(info)):
                break
    finally:
        api.BluetoothFindDeviceClose(finder)
    return found


def devices() -> list[Device]:
    """Сопряжённые устройства и подключены ли они сейчас."""
    api = _api()
    radio = _radio(api)
    try:
        return [
            Device(name=info.szName, address=info.Address, connected=bool(info.fConnected), kind=info.ulClassofDevice)
            for info in _infos(api, radio)
            if info.fRemembered or info.fAuthenticated
        ]
    finally:
        _close(radio)


def set_connected(address: int, connected: bool) -> int:
    """Подключить или отключить устройство службами. Возвращает, сколько служб приняли."""
    api = _api()
    radio = _radio(api)
    try:
        target = next((info for info in _infos(api, radio) if info.Address == address), None)
        if target is None:
            raise BluetoothError("Такого устройства среди сопряжённых нет.")
        accepted = 0
        for service in (*AUDIO_SERVICES, HID_SERVICE):
            guid = _GUID.of(service)
            code = api.BluetoothSetServiceState(
                radio, ctypes.byref(target), ctypes.byref(guid), _ENABLE if connected else _DISABLE
            )
            if code == 0:
                accepted += 1
        return accepted
    finally:
        _close(radio)


_RADIO_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bt = $radios | Where-Object { $_.Kind -eq 'Bluetooth' } | Select-Object -First 1
if (-not $bt) { 'none'; exit }
__ACTION__
"""


def radio(state: str | None = None) -> str:
    """Состояние радио («On», «Off», «none» — нет модуля); с `state` — переключить.

    :param state: ``"On"`` или ``"Off"``; ``None`` — только узнать.
    """
    if sys.platform != "win32":
        raise BluetoothError("Блютуз умею только в Windows.")
    action = (
        f"$r = Await ($bt.SetStateAsync('{state}')) ([Windows.Devices.Radios.RadioAccessStatus]); \"$r\""
        if state in ("On", "Off")
        else "\"$($bt.State)\""
    )
    script = _RADIO_SCRIPT.replace("__ACTION__", action)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20, creationflags=flags, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BluetoothError(f"PowerShell не ответил: {exc}") from exc
    output = done.stdout.decode("utf-8", "replace").strip().splitlines()
    if done.returncode != 0 or not output:
        error = done.stderr.decode("cp866", "replace").strip()
        raise BluetoothError(f"Радио не ответило: {error[-200:] or done.returncode}")
    return output[-1].strip()


# --- выбор устройства по услышанному (чистые функции) -------------------------

HEADPHONE_WORDS = ("наушник", "гарнитур", "уши", "headphone", "earbud", "headset")
SPEAKER_WORDS = ("колонк", "колонок", "динамик", "speaker")


def is_headphones(kind: int) -> bool:
    """Наушники или гарнитура по классу устройства (аудио, младший класс 1, 2 или 6)."""
    return (kind >> 8) & 0x1F == 4 and (kind >> 2) & 0x3F in (1, 2, 6)


def is_speaker(kind: int) -> bool:
    """Колонка по классу устройства (аудио: громкоговоритель, переносное, Hi-Fi)."""
    return (kind >> 8) & 0x1F == 4 and (kind >> 2) & 0x3F in (5, 7, 10)


def category(query: str) -> str:
    """«наушники», «колонку» — не название, а род устройства: ``headphones``/``speaker``/пусто."""
    low = query.lower()
    if any(word in low for word in HEADPHONE_WORDS):
        return "headphones"
    if any(word in low for word in SPEAKER_WORDS):
        return "speaker"
    return ""


def by_category(kind: str, found: list[Device]) -> list[Device]:
    """Устройства нужного рода."""
    test = is_headphones if kind == "headphones" else is_speaker
    return [device for device in found if test(device.kind)]
