"""Жизненный цикл сервисов.

Сервис — это всё, что нужно запустить при старте и корректно погасить при
остановке: шина, worker, голосовой конвейер, менеджер скиллов. Запуск идёт
по порядку, остановка — в обратном, и сбой одного сервиса не мешает погасить
остальные.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from jarvis.core.errors import JarvisError

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


#: Для чего нужен сервис. Метка нужна, чтобы поднимать не всё: модели грузятся
#: минутами, а разным режимам запуска нужны разные их половины.
#:
#: * ``EARS`` — слышать: микрофон, распознавание, цикл прослушивания;
#: * ``VOICE`` — говорить: синтез и звуковой выход.
#:
#: Одного флага «тяжёлый» тут не хватило. Отчёту о сборке не нужно ни то, ни
#: другое; одиночной команде (``--say``) нужен голос, но слушать ей нечего —
#: текст уже написан, и Whisper поднимался впустую.
EARS = "ears"
VOICE = "voice"


class ServiceRunner:
    """Запускает сервисы по порядку и останавливает в обратном."""

    def __init__(self) -> None:
        self._services: list[tuple[Service, str]] = []
        self._started: list[Service] = []

    def add(self, service: Service, *, needs: str = "") -> Service:
        """Зарегистрировать сервис. Возвращает его же — удобно для цепочек.

        :param needs: для чего сервис нужен: :data:`EARS`, :data:`VOICE` или
            пусто — значит нужен всегда. Помеченные грузят модели и поднимаются
            минутами, поэтому режимы запуска берут только свою половину.
        """
        self._services.append((service, needs))
        return service

    @property
    def services(self) -> tuple[Service, ...]:
        """Все зарегистрированные сервисы в порядке запуска."""
        return tuple(service for service, _ in self._services)

    async def start_all(self, *, without: Iterable[str] = ()) -> None:
        """Запустить всё. При сбое гасит уже поднятое и пробрасывает ошибку.

        :param without: какие половины не поднимать (:data:`EARS`, :data:`VOICE`).
        """
        skip = frozenset(without)
        for service, needs in self._services:
            if needs and needs in skip:
                logger.debug(
                    "Сервис %s пропущен: %s тут не нужны", service.service_name, needs
                )
                continue
            try:
                await service.start()
            except JarvisError as exc:
                # Своя ошибка объясняет себя сама: в ней написано, что делать.
                # Стек рядом с таким текстом только мешает — человек читает
                # первую строку и видит простыню вместо ответа.
                logger.error("Сервис %s не запустился: %s", service.service_name, exc)
                await self.stop_all()
                raise
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
