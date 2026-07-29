"""Жизненный цикл сервисов.

Сервис — это всё, что нужно запустить при старте и корректно погасить при
остановке: шина, worker, голосовой конвейер, менеджер скиллов. Запуск идёт
по порядку, остановка — в обратном, и сбой одного сервиса не мешает погасить
остальные.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Service(Protocol):
    """Компонент с управляемым временем жизни."""

    @property
    def service_name(self) -> str:
        """Имя для логов."""
        ...

    async def start(self) -> None:
        """Поднять сервис."""
        ...

    async def stop(self) -> None:
        """Погасить сервис, освободив ресурсы."""
        ...


class ServiceRunner:
    """Запускает сервисы по порядку и останавливает в обратном."""

    def __init__(self) -> None:
        self._services: list[tuple[Service, bool]] = []
        self._started: list[Service] = []

    def add(self, service: Service, *, heavy: bool = False) -> Service:
        """Зарегистрировать сервис. Возвращает его же — удобно для цепочек.

        :param heavy: сервис грузит модели и потому нужен не всегда. Отчёт о
            сборке (``--check``) поднимает систему без таких сервисов: ему
            важен каталог инструментов, а не то, что Whisper умеет слушать.
        """
        self._services.append((service, heavy))
        return service

    @property
    def services(self) -> tuple[Service, ...]:
        """Все зарегистрированные сервисы в порядке запуска."""
        return tuple(service for service, _ in self._services)

    async def start_all(self, *, skip_heavy: bool = False) -> None:
        """Запустить всё. При сбое гасит уже поднятое и пробрасывает ошибку.

        :param skip_heavy: пропустить сервисы, помеченные ``heavy``.
        """
        for service, heavy in self._services:
            if heavy and skip_heavy:
                logger.debug("Сервис %s пропущен: модели не нужны", service.service_name)
                continue
            try:
                await service.start()
            except Exception:
                logger.exception("Сервис %s не запустился", service.service_name)
                await self.stop_all()
                raise
            self._started.append(service)
            logger.debug("Сервис %s запущен", service.service_name)

    async def stop_all(self) -> None:
        """Остановить всё запущенное в обратном порядке, не прерываясь на ошибках."""
        while self._started:
            service = self._started.pop()
            try:
                await service.stop()
                logger.debug("Сервис %s остановлен", service.service_name)
            except Exception:
                logger.exception("Ошибка при остановке сервиса %s", service.service_name)
