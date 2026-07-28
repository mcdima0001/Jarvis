"""Управление ESP32: свет, датчики, режимы студии.

Заглушка: инструменты, фразы и события настоящие, наружу пока ничего не ходит.
Чтобы оживить — замени тела методов HTTP-запросами к контроллеру.
"""

from __future__ import annotations

import asyncio
import random

from jarvis.core.contracts import SensorReadingChanged, StudioModeChanged, ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool


class ESP32Skill(Skill):
    """Свет, датчики и сценарии студии через ESP32."""

    meta = SkillMeta(
        name="esp32",
        description="Освещение, датчики и режимы студии",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать настройки и подписаться на смену режима."""
        self._host = self.context.setting("host", "192.168.1.50")
        self._port = int(self.context.setting("port", 80))
        self._mode = "work"
        self._poll_interval = float(self.context.setting("poll_interval", 0))

        # Пример подписки: реакция на факт, который создал кто-то другой.
        self.context.scope.subscribe("sensor.motion.detected", self._on_motion)
        self.log.info("ESP32 настроен на %s:%s", self._host, self._port)

    async def on_start(self) -> None:
        """Запустить опрос датчиков, если он включён в конфиге."""
        if self._poll_interval > 0:
            self.context.scope.spawn(self._poll_sensors(), name="esp32-poll")

    async def _poll_sensors(self) -> None:
        """Фоновый опрос датчиков; задачу отменит scope при выгрузке."""
        while True:
            await asyncio.sleep(self._poll_interval)
            # TODO: реальный запрос к ESP32 вместо случайного значения
            value = round(random.uniform(21.0, 24.0), 1)
            self.events.emit(
                SensorReadingChanged(
                    source=self.meta.name,
                    sensor="studio",
                    value=value,
                    unit="°C",
                )
            )

    async def _on_motion(self, event) -> None:
        """Включить свет, когда сработал датчик движения."""
        self.log.debug("Движение в зоне %s", getattr(event, "zone", "?"))

    # --- инструменты -------------------------------------------------------

    @tool(phrases=["включи свет", "включи свет в студии", "зажги свет"])
    async def set_light(self, state: bool = True, zone: str = "studio", brightness: int = 100) -> ToolResult:
        """Включить или выключить свет в зоне студии.

        :param state: True — включить, False — выключить.
        :param zone: зона освещения (studio, desk, ambient).
        :param brightness: яркость в процентах, от 0 до 100.
        """
        # TODO: POST http://{host}/light
        action = "включён" if state else "выключен"
        self.log.info("Свет %s: зона=%s яркость=%s%%", action, zone, brightness)
        return ToolResult.success(
            {"zone": zone, "state": state, "brightness": brightness},
            speech=f"Свет в зоне {zone} {action}.",
        )

    @tool(phrases=["какая температура", "температура в студии", "сколько градусов"])
    async def get_temperature(self, sensor: str = "studio") -> ToolResult:
        """Вернуть температуру с датчика в градусах Цельсия.

        :param sensor: идентификатор датчика.
        """
        # TODO: GET http://{host}/sensors/{sensor}
        value = round(random.uniform(21.0, 24.0), 1)
        return ToolResult.success(value, speech=f"В студии {value} градуса.")

    @tool(phrases=["включи {mode} режим", "переключись в режим {mode}"])
    async def set_mode(self, mode: str) -> ToolResult:
        """Переключить сценарий студии.

        :param mode: game, record, cinema, work или sleep.
        """
        known = ("game", "record", "cinema", "work", "sleep")
        aliases = {
            "игровой": "game",
            "записи": "record",
            "кино": "cinema",
            "рабочий": "work",
            "сна": "sleep",
        }
        resolved = aliases.get(mode.strip().lower(), mode.strip().lower())
        if resolved not in known:
            return ToolResult.failure(
                f"Неизвестный режим {mode!r}",
                speech=f"Не знаю режим {mode}. Есть игровой, записи, кино, рабочий и сна.",
            )

        previous, self._mode = self._mode, resolved
        # TODO: POST http://{host}/mode
        self.events.emit(
            StudioModeChanged(source=self.meta.name, mode=resolved, previous=previous)
        )
        return ToolResult.success(
            {"mode": resolved, "previous": previous},
            speech=f"Включаю режим {mode}.",
        )

    @tool()
    async def get_humidity(self, sensor: str = "studio") -> ToolResult:
        """Вернуть влажность с датчика в процентах.

        :param sensor: идентификатор датчика.
        """
        # TODO: GET http://{host}/sensors/{sensor}
        value = round(random.uniform(40.0, 55.0), 1)
        return ToolResult.success(value, speech=f"Влажность {value} процентов.")

    async def health(self) -> HealthStatus:
        """Проверить доступность контроллера."""
        # TODO: пинг ESP32
        return HealthStatus.healthy(f"заглушка, адрес {self._host}:{self._port}")
