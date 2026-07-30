"""Встроенные инструменты ядра.

Их немного и они намеренно системные: свободный диалог, справка по каталогу,
перезагрузка скилла, смена модели. Всё остальное — дело скиллов.

Свободный разговор оформлен обычным инструментом ``core.chat``, а не особым
путём внутри роутера: у него такое же имя, схема и результат, как у «включи
свет». Меньше исключений в архитектуре — меньше сюрпризов через год.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from jarvis.core.contracts import ToolResult
from jarvis.core.llm import LLMService
from jarvis.core.memory import Memory
from jarvis.core.persona import Persona
from jarvis.core.tools import ToolRegistry, tool

if TYPE_CHECKING:
    from jarvis.core.router import LearnedResolver
    from jarvis.core.skills import SkillManager

#: Договорённость об отмене обучения: любой скилл, который что-то запоминает
#: сам, объявляет инструмент с таким именем — и попадает под общую команду
#: «не сохраняй в память». Так это работает у скилла `page`, который запоминает
#: кнопки сайтов.
FORGET_TOOL = "forget_last"

logger = logging.getLogger(__name__)

NAMESPACE = "core"

#: Системные подсказки под каждый язык: модель должна отвечать так же, как её
#: спросили, и коротко — реплику будут произносить вслух. Манера речи сюда не
#: вписана: она приходит из `Persona.style`, потому что задаётся один раз на
#: весь проект и настраивается в конфиге.
_DIALOG_SYSTEM = {
    "ru": (
        "Ты — Jarvis, голосовой ассистент домашней студии. Отвечай по-русски, "
        "кратко и по делу: реплику будут произносить вслух. Без списков и разметки. "
        "Сюда попадают только те реплики, которые не удалось выполнить как команду, "
        "поэтому действий ты не выполняешь: не отвечай «включаю», «открываю», "
        "«готово». Если просят что-то сделать — скажи, что не понял команду, и "
        "попроси сказать иначе."
    ),
    "en": (
        "You are Jarvis, the voice assistant of a home studio. Answer in English, "
        "briefly and to the point: your reply will be spoken aloud. "
        "No lists, no markdown. Only phrases that could not be carried out as a "
        "command reach you, so you perform no actions: never say “playing”, "
        "“opening” or “done”. If asked to do something, say you didn't catch the "
        "command and ask for it another way."
    ),
}


#: Дни недели: у модели даты нет, а «какой сегодня день» спрашивают постоянно.
_WEEKDAYS = {
    "ru": ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _language(code: str | None) -> str:
    """Привести код языка к короткому виду с откатом на русский."""
    short = (code or "ru").split("-")[0].lower()
    return short if short in _DIALOG_SYSTEM else "ru"


def _now_line(code: str) -> str:
    """Строка с текущей датой и временем для системной подсказки.

    Модель не знает, какой сегодня день: её знания заканчиваются на дате
    обучения, а часов у неё нет. Без этой строки на «какой сегодня день» она
    честно выдумывает.
    """
    now = datetime.now().astimezone()
    weekday = _WEEKDAYS[code][now.weekday()]
    if code == "ru":
        return (
            f"Сейчас {now.day} {_MONTHS_RU[now.month - 1]} {now.year} года, "
            f"{weekday}, {now:%H:%M}."
        )
    return f"Current date and time: {weekday}, {now:%d %B %Y, %H:%M}."


class CoreTools:
    """Инструменты, которые ядро регистрирует само."""

    def __init__(
        self,
        *,
        llm: LLMService,
        memory: Memory,
        registry: ToolRegistry,
        skills: "SkillManager",
        persona: Persona | None = None,
        learner: "LearnedResolver | None" = None,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._registry = registry
        self._skills = skills
        self._persona = persona or Persona()
        self._learner = learner

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
            system=f"{_DIALOG_SYSTEM[code]} {self._persona.style(code)} {_now_line(code)}",
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

    @tool(name="reload_skill", routable=False)
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

    @tool(name="set_model", routable=False)
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
        spending = self._llm.spending
        payload = {
            "skills": {name: state.ok for name, state in health.items()},
            "tools": len(self._registry),
            "models": self._llm.models(),
            "llm_available": self._llm.available,
            "llm_calls": spending.calls,
            "llm_tokens": spending.total_tokens,
            "llm_cost": round(spending.cost, 5),
            "llm_by_task": dict(spending.by_task),
        }
        # Расход проговариваем только когда он есть: в тишине это лишний шум.
        cost = ""
        if spending.calls:
            cost = (
                f" Модель: {spending.calls} запросов, {spending.total_tokens} токенов."
            )
        if broken:
            speech = {
                "ru": f"Есть проблемы в модулях: {', '.join(broken)}.{cost}",
                "en": f"Problems in modules: {', '.join(broken)}.{cost}",
            }
        else:
            speech = {
                "ru": f"Всё работает: {len(health)} модулей, "
                      f"{len(self._registry)} команд.{cost}",
                "en": f"All good: {len(health)} modules, "
                      f"{len(self._registry)} commands.{cost}",
            }
        return ToolResult.success(payload, speech=speech)

    @tool(
        name="forget_last",
        phrases=["не сохраняй в память", "не запоминай", "не запоминай это",
                 "забудь это", "забудь последнюю команду", "не надо это запоминать",
                 "don't remember that", "forget that", "forget the last command"],
    )
    async def forget_last(self) -> ToolResult:
        """Отменить последнее, что ассистент запомнил сам.

        Обучение идёт молча и по факту успеха, но «сработало» и «сработало так,
        как я хотел» — разные вещи: модель могла выбрать похожую команду или
        соседнюю кнопку. Эта команда отменяет и выученную формулировку, и
        выученный способ нажать что-то на странице — всё, что ассистент
        записал последним.

        Отмена не просто стирает, а **запоминает промах**: и формулировка, и
        кнопка попадают в список «это уже пробовали, не то». В следующий раз
        отвергнутое не предлагается — ни в плане нажатий, ни модели. Иначе
        отмена была бы бессмысленной: модель уверенно предложила бы то же
        самое, разбор прошёл бы «удачно», и связка выучилась бы снова.
        """
        forgotten: list[str] = []

        if self._learner is not None:
            phrase = await self._learner.reject()
            if phrase:
                forgotten.append(phrase)

        # Скиллы, которые учатся сами, отменяют это своим инструментом. Имя —
        # договорённость (`FORGET_TOOL`), поэтому новый такой скилл попадает
        # под эту команду без правки ядра.
        for spec in self._registry.specs():
            if spec.name == f"{NAMESPACE}.{FORGET_TOOL}":
                continue
            if not spec.name.endswith(f".{FORGET_TOOL}"):
                continue
            result = await self._registry.invoke(spec.name, {})
            if result.ok and result.value:
                forgotten.append(str(result.value))

        if not forgotten:
            return ToolResult.success(
                [],
                speech={
                    "ru": "Мне нечего забывать.",
                    "en": "There's nothing for me to forget.",
                },
            )
        listed = "; ".join(forgotten)
        return ToolResult.success(
            forgotten,
            speech={
                "ru": f"Забыл: {listed}.",
                "en": f"Forgotten: {listed}.",
            },
        )

    @tool(name="spending", phrases=["сколько потрачено", "расход токенов",
                                    "how much have you spent", "token usage"])
    async def spending(self) -> ToolResult:
        """Показать расход токенов с момента запуска."""
        report = self._llm.spending
        return ToolResult.success(
            {
                "calls": report.calls,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "cost": round(report.cost, 5),
                "by_task": dict(report.by_task),
            },
            speech={
                "ru": f"С запуска: {report.calls} запросов к модели, "
                      f"{report.total_tokens} токенов."
                if report.calls
                else "Модель ещё ни разу не вызывалась.",
                "en": f"Since start: {report.calls} model calls, "
                      f"{report.total_tokens} tokens."
                if report.calls
                else "The model hasn't been called yet.",
            },
        )
