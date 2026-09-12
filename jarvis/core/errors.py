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


class LLMOutOfCredits(LLMError):
    """На счету провайдера кончились деньги.

    Отдельный вид, а не строка в тексте ошибки, и причина этому живая. Ночью
    13.09.2026 замер скилла `photo_place` выдал шестнадцать «не узнаю» подряд, и
    выглядело это провалом механизма — а на деле OpenRouter отвечал «можешь
    позволить себе 105 токенов из запрошенных 300». Сбой, о котором ассистент
    говорит не своими словами, стоит часов поисков не там.

    Чинится это не кодом, поэтому и сказать надо прямо: пополнить счёт.
    """


class MemoryError_(JarvisError):
    """Ошибка работы с памятью."""


class AudioError(JarvisError):
    """Ошибка захвата или воспроизведения звука."""


class STTError(JarvisError):
    """Распознавание речи не поднялось: нет модели, не хватило памяти."""
