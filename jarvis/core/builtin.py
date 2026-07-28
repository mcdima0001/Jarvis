"""Встроенные инструменты ядра.

Их немного и они намеренно системные: свободный диалог, справка по каталогу,
перезагрузка скилла, смена модели. Всё остальное — дело скиллов.

Свободный разговор оформлен обычным инструментом ``core.chat``, а не особым
путём внутри роутера: у него такое же имя, схема и результат, как у «включи
свет». Меньше исключений в архитектуре — меньше сюрпризов через год.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.core.contracts import ToolResult
from jarvis.core.llm import LLMService
from jarvis.core.memory import Memory
from jarvis.core.tools import ToolRegistry, tool

if TYPE_CHECKING:
    from jarvis.core.skills import SkillManager

logger = logging.getLogger(__name__)

NAMESPACE = "core"

#: Системные подсказки под каждый язык: модель должна отвечать так же, как её
#: спросили, и коротко — реплику будут произносить вслух.
_DIALOG_SYSTEM = {
    "ru": (
        "Ты — Jarvis, голосовой ассистент домашней студии. Отвечай по-русски, "
        "кратко и по делу: реплику будут произносить вслух. Без списков и разметки."
    ),
    "en": (
        "You are Jarvis, the voice assistant of a home studio. Answer in English, "
        "briefly and to the point: your reply will be spoken aloud. "
        "No lists, no markdown."
    ),
}


def _language(code: str | None) -> str:
    """Привести код языка к короткому виду с откатом на русский."""
    short = (code or "ru").split("-")[0].lower()
    return short if short in _DIALOG_SYSTEM else "ru"


class CoreTools:
    """Инструменты, которые ядро регистрирует само."""

    def __init__(
        self,
        *,
        llm: LLMService,
        memory: Memory,
        registry: ToolRegistry,
        skills: "SkillManager",
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._registry = registry
        self._skills = skills

    @tool(name="chat")
    async def chat(self, text: str, language: str = "ru") -> ToolResult:
        """Ответить на свободный вопрос через языковую модель.

        :param text: реплика пользователя.
        :param language: язык, на котором отвечать.
        """
        code = _language(language)
        if not self._llm.available:
            return ToolResult.failure(
                # Технические подробности — в error и в лог, а вслух только то,
                # что не превратится в кашу при синтезе.
                "Языковая модель не настроена: задай JARVIS_OPENROUTER_KEY в .env",
                speech={
                    "ru": "Языковая модель не подключена. Добавь ключ в настройки.",
                    "en": "The language model isn't connected. Add the key in settings.",
                },
            )

        context = await self._memory.context.build(
            documents=("profile", "preferences", "studio"),
            journals=("today",),
            journal_limit=5,
        )
        answer = await self._llm.ask(
            text,
            task="dialog",
            system=_DIALOG_SYSTEM[code],
            context=context or None,
        )
        await self._memory.remember(f"Вопрос: {text}", tags=("dialog",))
        return ToolResult.success(answer, speech=answer)

    @tool(
        name="help",
        phrases=["что ты умеешь", "список команд", "помощь", "what can you do", "help"],
    )
    async def help(self) -> ToolResult:
        """Перечислить доступные команды."""
        specs = self._registry.specs()
        if not specs:
            return ToolResult.success(
                [],
                speech={
                    "ru": "Пока ни одной команды не подключено.",
                    "en": "No commands are connected yet.",
                },
            )

        skills = sorted({spec.skill for spec in specs if spec.skill})
        return ToolResult.success(
            [{"name": spec.name, "description": spec.description} for spec in specs],
            speech={
                "ru": f"Подключено {len(specs)} команд в модулях: {', '.join(skills)}.",
                "en": f"{len(specs)} commands available in modules: {', '.join(skills)}.",
            },
        )

    @tool(name="reload_skill")
    async def reload_skill(self, skill: str) -> ToolResult:
        """Перезагрузить скилл с диска без перезапуска приложения.

        :param skill: имя скилла.
        """
        try:
            record = await self._skills.reload(skill)
        except Exception as exc:
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                speech={
                    "ru": f"Не удалось перезагрузить модуль {skill}.",
                    "en": f"Couldn't reload module {skill}.",
                },
            )
        return ToolResult.success(
            {"skill": record.name, "tools": list(record.scope.tool_names)},
            speech={
                "ru": f"Модуль {skill} перезагружен.",
                "en": f"Module {skill} reloaded.",
            },
        )

    @tool(name="set_model")
    async def set_model(self, task: str, model: str) -> ToolResult:
        """Сменить модель для типа задач во время работы.

        :param task: тип задачи — dialog, code, summarize, intent, analysis.
        :param model: идентификатор модели у провайдера.
        """
        try:
            self._llm.set_model(task, model)
        except Exception as exc:
            return ToolResult.failure(
                str(exc),
                speech={
                    "ru": "Не получилось сменить модель.",
                    "en": "Couldn't switch the model.",
                },
            )
        return ToolResult.success(
            self._llm.models(),
            speech={
                "ru": f"Для задачи {task} теперь используется {model}.",
                "en": f"Task {task} now uses {model}.",
            },
        )

    @tool(name="status", phrases=["статус", "как дела", "status", "how are you"])
    async def status(self) -> ToolResult:
        """Показать состояние скиллов и подключённых моделей."""
        health = await self._skills.health()
        broken = [name for name, state in health.items() if not state.ok]
        payload = {
            "skills": {name: state.ok for name, state in health.items()},
            "tools": len(self._registry),
            "models": self._llm.models(),
            "llm_available": self._llm.available,
        }
        if broken:
            speech = {
                "ru": f"Есть проблемы в модулях: {', '.join(broken)}.",
                "en": f"Problems in modules: {', '.join(broken)}.",
            }
        else:
            speech = {
                "ru": f"Всё работает: {len(health)} модулей, {len(self._registry)} команд.",
                "en": f"All good: {len(health)} modules, {len(self._registry)} commands.",
            }
        return ToolResult.success(payload, speech=speech)
