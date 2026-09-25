"""Где я: место владельца для всех, кому оно нужно, — погоды, самолётов, плана.

Просьба владельца 25.09.2026: погода отвечала про Москву из настроек. Город,
вписанный руками, врёт ровно тогда, когда нужнее всего, — в поездке.

**Основа — служба геолокации Windows**, то есть место по окружающим сетям
Wi-Fi: замер 25.09.2026 — 4 с, точность 116 м. IP остался запасным, и не зря
вторым: мобильный IP владельца показал Измир, а Wi-Fi — Анталью, в четырёхстах
километрах. Через VPN (владелец им пользуется часто) IP показал бы страну
выхода, а Wi-Fi от VPN не зависит. Первый пробник на старом интерфейсе .NET
(`GeoCoordinateWatcher`) ответил `NoData` — спрашивать надо WinRT `Geolocator`,
через PowerShell, как радио блютуза: новых зависимостей не нужно.

Координаты превращаются в город обратным геокодером OSM (Nominatim, по-русски,
0.8 с) — для речи нужно «Анталья», а не числа. Вписанное в настройки место
важнее всего: Wi-Fi и IP тогда не спрашиваются вовсе.

Место держится `cache_minutes` и попутно кладётся в обстановку
(`situation.note`) — модели при разборе полезно знать, где «рядом».
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from typing import Any

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Место по IP: без ключа, по HTTPS, `lang=ru` отдаёт названия по-русски.
IP_LOOKUP = "https://ipwho.is/"
#: Город по координатам: OSM просит представляться, как и в `photo_place`.
REVERSE = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Jarvis voice assistant (github.com/mcdima0001/Jarvis)"

#: Служба геолокации Windows (WinRT) из PowerShell. Числа — в инвариантной
#: культуре: русская записала бы широту через запятую.
_WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $null = $task.Wait(15000); $task.Result }
$access = Await ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus])
if ("$access" -ne 'Allowed') { "denied;$access"; exit }
$g = New-Object Windows.Devices.Geolocation.Geolocator
$g.DesiredAccuracy = 'High'
$c = (Await ($g.GetGeopositionAsync()) ([Windows.Devices.Geolocation.Geoposition])).Coordinate
$inv = [Globalization.CultureInfo]::InvariantCulture
"ok;" + $c.Point.Position.Latitude.ToString($inv) + ";" + $c.Point.Position.Longitude.ToString($inv) + ";" + $c.Accuracy.ToString($inv) + ";" + $c.PositionSource
"""

_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


def from_settings(place: Any) -> dict[str, Any] | None:
    """Место из настроек, если оно задано целиком: без координат город бесполезен.

    Чистая функция — её проверяют тесты.
    """
    if not isinstance(place, dict):
        return None
    try:
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "city": str(place.get("city") or ""),
        "country": str(place.get("country") or ""),
        "latitude": latitude,
        "longitude": longitude,
        "source": "настройки",
    }


def parse_windows(output: str) -> dict[str, Any] | None:
    """Разобрать строку пробника: «ok;широта;долгота;точность;источник». Чистая функция."""
    parts = output.strip().splitlines()[-1].split(";") if output.strip() else []
    if len(parts) < 5 or parts[0] != "ok":
        return None
    try:
        latitude, longitude, accuracy = float(parts[1]), float(parts[2]), float(parts[3])
    except ValueError:
        return None
    return {
        "city": "",
        "country": "",
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": round(accuracy),
        "source": f"Windows, {parts[4].strip()}",
    }


def windows_position(timeout: float = 25.0) -> dict[str, Any] | None:
    """Место от службы геолокации Windows; нет её, запрещена или молчит — `None`."""
    if sys.platform != "win32":
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT],
            capture_output=True, timeout=timeout, creationflags=flags, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_windows(done.stdout.decode("utf-8", "replace"))


def city_of(address: dict[str, Any]) -> str:
    """Город из адреса Nominatim: у деревни и посёлка он зовётся по-своему."""
    for key in ("city", "town", "village", "municipality", "province", "state"):
        if address.get(key):
            return str(address[key])
    return ""


def from_lookup(data: dict[str, Any]) -> dict[str, Any] | None:
    """Разобрать ответ `ipwho.is`. Отказ сервиса — `None`, а не выдуманное место."""
    if not data.get("success", True):
        return None
    return {
        "city": str(data.get("city") or ""),
        "country": str(data.get("country") or ""),
        "latitude": float(data["latitude"]),
        "longitude": float(data["longitude"]),
        "source": "IP",
    }


class WhereaboutsSkill(Skill):
    """Знает, где сейчас владелец, и отдаёт это другим скиллам инструментом."""

    meta = SkillMeta(
        name="whereabouts",
        description="Где я: место владельца по Wi-Fi (Windows), IP или из настроек",
        version="0.2.0",
        spoken=("где я", "место", "whereabouts"),
    )

    async def on_setup(self) -> None:
        self._fixed = from_settings(self.context.setting("place", None))
        self._ttl = float(self.context.setting("cache_minutes", 30)) * 60
        #: Спрашивать ли службу геолокации Windows (Wi-Fi) раньше IP.
        self._use_windows = bool(self.context.setting("windows_location", True))
        self._timeout = float(self.context.setting("timeout", 8.0))
        self._client: httpx.AsyncClient | None = None
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._refreshing = False

    async def on_start(self) -> None:
        """Узнать место заранее: служба Windows из PowerShell идёт до 12 с на холодную
        (замер 25.09.2026), и ждать их на первой же «какая погода» незачем."""
        if self._fixed is None:
            self.context.scope.spawn(self._refresh(), name="whereabouts-warm")

    async def on_stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def _lookup(self) -> dict[str, Any] | None:
        """Где владелец. Устаревшее место отдаётся сразу, а свежее ищется в фоне:
        за полчаса дальше соседнего района не уезжают, а ждать двенадцать секунд
        на каждом вопросе о погоде — заметно."""
        if self._cached is None:
            return await self._refresh()
        if time.monotonic() - self._cached_at >= self._ttl and not self._refreshing:
            self.context.scope.spawn(self._refresh(), name="whereabouts-refresh")
        return self._cached

    async def _refresh(self) -> dict[str, Any] | None:
        """Спросить заново: Wi-Fi через Windows, иначе IP. Два вопроса разом не задаются."""
        if self._refreshing:
            return self._cached
        self._refreshing = True
        try:
            return await self._ask()
        finally:
            self._refreshing = False

    async def _ask(self) -> dict[str, Any] | None:
        now = time.monotonic()
        place = await asyncio.to_thread(windows_position) if self._use_windows else None
        if place is not None:
            await self._name(place)
        else:
            place = await self._by_ip()
        if place is None:
            return self._cached  # старое место лучше никакого
        if self._cached is None or place["city"] != self._cached["city"]:
            self.log.info("Место (%s): %s, %s", place["source"], place["city"] or "?", place["country"] or "?")
        self._cached, self._cached_at = place, now
        return place

    async def _name(self, place: dict[str, Any]) -> None:
        """Подписать координаты городом: вслух нужно «Анталья», а не числа."""
        try:
            response = await self._http().get(
                REVERSE,
                params={
                    "format": "jsonv2", "lat": place["latitude"], "lon": place["longitude"],
                    "zoom": 10, "accept-language": "ru",
                },
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            address = response.json().get("address") or {}
        except _ERRORS as error:
            self.log.debug("Город по координатам не узнал: %s", error)
            return
        place["city"] = city_of(address)
        place["country"] = str(address.get("country") or "")

    async def _by_ip(self) -> dict[str, Any] | None:
        try:
            response = await self._http().get(IP_LOOKUP, params={"lang": "ru"})
            response.raise_for_status()
            return from_lookup(response.json())
        except _ERRORS as error:
            self.log.warning("Место по IP не узнал: %s", error)
            return None

    @tool(
        phrases=["где я", "где я нахожусь", "в каком я городе", "в каком я сейчас городе", "where am i"],
        reversible=True,
    )
    async def here(self) -> ToolResult:
        """Где сейчас владелец: город, страна и координаты."""
        place = self._fixed or await self._lookup()
        if place is None:
            return ToolResult.failure(
                "место не определено",
                speech={"ru": "Не могу понять, где вы, сэр.", "en": "I can't tell where you are, sir."},
            )
        if place["city"]:
            self.context.situation.note("владелец сейчас в городе", place["city"], minutes=self._ttl / 60)
        if not place["city"]:
            return ToolResult.success(
                place,
                speech={"ru": "Координаты знаю, а город не узнал, сэр.", "en": "I have coordinates but no town name, sir."},
            )
        country = f", {place['country']}" if place["country"] else ""
        return ToolResult.success(
            place,
            speech={
                "ru": f"Вы в городе {place['city']}{country}, сэр.",
                "en": f"You are in {place['city']}{country}, sir.",
            },
        )

    async def health(self) -> HealthStatus:
        if self._fixed is not None:
            return HealthStatus.healthy(f"место из настроек: {self._fixed['city'] or 'координаты'}")
        return HealthStatus.healthy("место по Wi-Fi через Windows, запасное — IP" if self._use_windows else "место по IP")
