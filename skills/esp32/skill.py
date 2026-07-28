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

    @tool(
        phrases=[
            "включи свет",
            "включи свет в студии",
            "зажги свет",
            "turn on the light",
            "lights on",
        ]
    )
    async def set_light(
        self, state: bool = True, zone: str = "studio", brightness: int = 100
    ) -> ToolResult:
        """Включить или выключить свет в зоне студии.

        :param state: True — включить, False — выключить.
        :param zone: зона освещения (studio, desk, ambient).
        :param brightness: яркость в процентах, от 0 до 100.
        """
        # TODO: POST http://{host}/light
        self.log.info("Свет %s: зона=%s яркость=%s%%", state, zone, brightness)
        return ToolResult.success(
            {"zone": zone, "state": state, "brightness": brightness},
            speech={
                "ru": f"Свет в зоне {zone} {'включён' if state else 'выключен'}.",
                "en": f"Light in {zone} is {'on' if state else 'off'}.",
            },
        )

    @tool(
        phrases=[
            "какая температура",
            "температура в студии",
            "сколько градусов",
            "what's the temperature",
            "how warm is it",
        ]
    )
    async def get_temperature(self, sensor: str = "studio") -> ToolResult:
        """Вернуть температуру с датчика в градусах Цельсия.

        :param sensor: идентификатор датчика.
        """
        # TODO: GET http://{host}/sensors/{sensor}
        value = round(random.uniform(21.0, 24.0), 1)
        return ToolResult.success(
            value,
            speech={
                "ru": f"В студии {value} градуса.",
                "en": f"It's {value} degrees in the studio.",
            },
        )

    @tool(
        phrases=[
            "включи {mode} режим",
            "переключись в режим {mode}",
            "switch to {mode} mode",
            "set {mode} mode",
        ]
    )
    async def set_mode(self, mode: str) -> ToolResult:
        """Переключить сценарий студии.

        :param mode: game, record, cinema, work или sleep.
        """
        known = ("game", "record", "cinema", "work", "sleep")
        aliases = {
            "игровой": "game", "игровый": "game", "гейминг": "game",
            "записи": "record", "запись": "record", "recording": "record",
            "кино": "cinema", "киношный": "cinema", "movie": "cinema",
            "рабочий": "work", "работы": "work",
            "сна": "sleep", "ночной": "sleep",
        }
        resolved = aliases.get(mode.strip().lower(), mode.strip().lower())
        if resolved not in known:
            return ToolResult.failure(
                f"Неизвестный режим {mode!r}",
                speech={
                    "ru": f"Не знаю режим {mode}. Есть игровой, записи, кино, рабочий и сна.",
                    "en": f"I don't know the {mode} mode. "
                    f"Available: game, record, cinema, work, sleep.",
                },
            )

        previous, self._mode = self._mode, resolved
        # TODO: POST http://{host}/mode
        self.events.emit(
            StudioModeChanged(source=self.meta.name, mode=resolved, previous=previous)
        )
        return ToolResult.success(
            {"mode": resolved, "previous": previous},
            speech={"ru": f"Включаю режим {mode}.", "en": f"Switching to {resolved} mode."},
        )

    @tool(phrases=["какая влажность", "what's the humidity"])
    async def get_humidity(self, sensor: str = "studio") -> ToolResult:
        """Вернуть влажность с датчика в процентах.

        :param sensor: идентификатор датчика.
        """
        # TODO: GET http://{host}/sensors/{sensor}
        value = round(random.uniform(40.0, 55.0), 1)
        return ToolResult.success(
            value,
            speech={
                "ru": f"Влажность {value} процентов.",
                "en": f"Humidity is {value} percent.",
            },
        )

    async def health(self) -> HealthStatus:
        """Проверить доступность контроллера."""
        # TODO: пинг ESP32
        return HealthStatus.healthy(f"заглушка, адрес {self._host}:{self._port}")
