"""Профили задач: разные модели под разные задачи, смена на лету.

Диалог, код, суммаризация и разбор команд имеют разную цену ошибки и разную
цену токена. Профиль связывает тип задачи с провайдером, моделью и параметрами;
менять их можно в рантайме, без перезапуска.
"""

from __future__ import annotations

import logging
from typing import Mapping

from jarvis.core.config import TaskProfile
from jarvis.core.errors import LLMNotConfigured

logger = logging.getLogger(__name__)


class ProfileRegistry:
    """Изменяемый реестр профилей задач."""

    def __init__(self, profiles: Mapping[str, TaskProfile], *, default_task: str) -> None:
        self._profiles: dict[str, TaskProfile] = dict(profiles)
        self._default = default_task

    @property
    def default_task(self) -> str:
        """Задача, используемая когда тип не указан явно."""
        return self._default

    def tasks(self) -> tuple[str, ...]:
        """Все известные типы задач."""
        return tuple(sorted(self._profiles))

    def get(self, task: str | None = None) -> TaskProfile:
        """Вернуть профиль задачи."""
        key = task or self._default
        profile = self._profiles.get(key)
        if profile is None:
            raise LLMNotConfigured(
                f"Профиль задачи {key!r} не найден. Доступны: "
                f"{', '.join(self.tasks()) or '(ни одного)'}"
            )
        return profile

    def set_model(self, task: str, model: str) -> TaskProfile:
        """Сменить модель для задачи прямо во время работы."""
        current = self.get(task)
        updated = TaskProfile(
            task=current.task,
            provider=current.provider,
            model=model,
            temperature=current.temperature,
            max_tokens=current.max_tokens,
            system=current.system,
        )
        self._profiles[task] = updated
        logger.info("Модель для задачи %s изменена: %s -> %s", task, current.model, model)
        return updated

    def set_provider(self, task: str, provider: str) -> TaskProfile:
        """Перевести задачу на другого провайдера."""
        current = self.get(task)
        updated = TaskProfile(
            task=current.task,
            provider=provider,
            model=current.model,
            temperature=current.temperature,
            max_tokens=current.max_tokens,
            system=current.system,
        )
        self._profiles[task] = updated
        logger.info("Провайдер для задачи %s изменён: %s -> %s", task, current.provider, provider)
        return updated

    def snapshot(self) -> dict[str, str]:
        """Текущая раскладка «задача -> провайдер/модель» для диагностики."""
        return {
            task: f"{profile.provider}/{profile.model}"
            for task, profile in sorted(self._profiles.items())
        }
