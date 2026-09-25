"""Где я: место владельца для всех, кому оно нужно, — погоды, самолётов, плана.

Просьба владельца 25.09.2026: погода отвечала про Москву из настроек, а он был
в Измире. Город, вписанный руками, врёт ровно тогда, когда нужнее всего, — в
поездке. Поэтому место берётся **по IP** (`ipwho.is`: без ключа, по HTTPS,
названия по-русски), а настройки остаются как переопределение: точные
координаты дома лучше городского центра, который знает IP.

Честные ограничения. IP знает город, а не улицу: для погоды этого хватает, для
«самолёта возле меня» — с запасом в десяток километров. Через VPN IP покажет
страну выхода, и тогда спасает только место, вписанное в настройки. Службу
геолокации Windows пробовали: на ноутбуке владельца она отвечает `NoData`.

Место держится `cache_minutes` и попутно кладётся в обстановку
(`situation.note`) — модели при разборе полезно знать, что «рядом» — это Измир.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Место по IP: без ключа, по HTTPS, `lang=ru` отдаёт названия по-русски.
IP_LOOKUP = "https://ipwho.is/"

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
        description="Где я: место владельца по IP или из настроек",
        version="0.1.0",
        spoken=("где я", "место", "whereabouts"),
    )

    async def on_setup(self) -> None:
        self._fixed = from_settings(self.context.setting("place", None))
        self._ttl = float(self.context.setting("cache_minutes", 30)) * 60
        self._timeout = float(self.context.setting("timeout", 8.0))
        self._client: httpx.AsyncClient | None = None
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def on_stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _lookup(self) -> dict[str, Any] | None:
        """Место по IP, с кешем: переезжают не каждую минуту, а запрос — секунды."""
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        try:
            response = await self._client.get(IP_LOOKUP, params={"lang": "ru"})
            response.raise_for_status()
            place = from_lookup(response.json())
        except _ERRORS as error:
            self.log.warning("Место по IP не узнал: %s", error)
            return self._cached  # старое место лучше никакого
        if place is not None:
            if self._cached is None or place["city"] != self._cached["city"]:
                self.log.info("Место по IP: %s, %s", place["city"], place["country"])
            self._cached, self._cached_at = place, now
        return place or self._cached

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
        city = place["city"] or "неизвестном месте"
        country = f", {place['country']}" if place["country"] else ""
        return ToolResult.success(
            place,
            speech={"ru": f"Вы в городе {city}{country}, сэр.", "en": f"You are in {city}{country}, sir."},
        )

    async def health(self) -> HealthStatus:
        if self._fixed is not None:
            return HealthStatus.healthy(f"место из настроек: {self._fixed['city'] or 'координаты'}")
        return HealthStatus.healthy("место по IP")
