"""Иерархия ошибок ядра.

Скиллы ловят `JarvisError` и его потомков, а не голые исключения Python.
"""

from __future__ import annotations


class JarvisError(Exception):
    """Базовая ошибка Jarvis."""


class ConfigError(JarvisError):
    """Конфигурация некорректна или недоступна."""


class SkillError(JarvisError):
    """Ошибка загрузки или работы скилла."""


class SkillLoadError(SkillError):
    """Скилл не удалось импортировать или инициализировать."""


class SkillUnsupportedPlatform(SkillLoadError):
    """Скилл рассчитан на другую ОС. Это не поломка, а штатный пропуск."""


class ToolError(JarvisError):
    """Ошибка вызова инструмента."""


class ToolNotFound(ToolError):
    """Инструмента с таким именем нет в реестре."""


class ToolTimeout(ToolError):
    """Инструмент не ответил за отведённое время."""


class ToolInvalidArguments(ToolError):
    """Аргументы не соответствуют схеме инструмента."""


class LLMError(JarvisError):
    """Ошибка обращения к языковой модели."""


class LLMNotConfigured(LLMError):
    """Провайдер не настроен: нет ключа, модели или самого провайдера."""


class MemoryError_(JarvisError):
    """Ошибка работы с памятью."""


class AudioError(JarvisError):
    """Ошибка захвата или воспроизведения звука."""
