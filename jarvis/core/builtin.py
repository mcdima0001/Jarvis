"""Встроенные инструменты ядра.

Их немного и они намеренно системные: свободный диалог, справка по каталогу,
перезагрузка скилла, смена модели. Всё остальное — дело скиллов.

Свободный разговор оформлен обычным инструментом ``core.chat``, а не особым
путём внутри роутера: у него такое же имя, схема и результат, как у «включи
свет». Меньше исключений в архитектуре — меньше сюрпризов через год.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Mapping

from jarvis.core.agent import Outcome, Planner
from jarvis.core.contracts import Intent, ToolResult
from jarvis.core.jobs import Jobs, busy_line, shorten
from jarvis.core.jobs import describe as describe_jobs
from jarvis.core.llm import LLMService
from jarvis.core.memory import Memory
from jarvis.core.persona import Persona
from jarvis.core.situation import Situation
from jarvis.core.state import BRIEF, DEAF, WAKE_PHRASES, Modes, minutes_word
from jarvis.core.text import best_match
from jarvis.core.tools import ToolRegistry, tool
from jarvis.core.version import current

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


#: Сколько длится «не слушай», если срок не назвали. Полчаса — то, что обычно
#: и имеют в виду; ошибиться в эту сторону безопаснее, чем оглохнуть навсегда.
DEFAULT_SLEEP_MINUTES = 30

#: Сколько секунд считать «тем же событием». Одна команда учит сразу нескольких
#: (формулировку и кнопку), и записи ложатся в одну секунду; всё, что старше, —
#: уже другая история, и отменять его никто не просил.
FORGET_WINDOW_S = 10.0


def _language(code: str | None) -> str:
    """Привести код языка к короткому виду с откатом на русский."""
    short = (code or "ru").split("-")[0].lower()
    return short if short in _DIALOG_SYSTEM else "ru"


def _stt_spending(stt: Any) -> tuple[int, float]:
    """Сколько запросов и секунд звука ушло в облачное распознавание.

    Спрашиваем **бережно**: распознавателей несколько, и счёт есть только у
    облачного. У местной модели платить не за что, у заглушки — тем более, и
    требовать от них этот метод значило бы тащить тариф в контракт STT.
    """
    spent = getattr(stt, "spent", None)
    if spent is None:
        # Пара «облако + запасное»: счёт живёт у основного.
        spent = getattr(getattr(stt, "_primary", None), "spent", None)
    if not spent:
        return 0, 0.0
    calls, seconds = spent
    return int(calls), float(seconds)


#: Что сказать, если план дошёл до конца, но своих слов у него не нашлось.
_DONE_PLAIN = {"ru": "Готово.", "en": "Done."}


#: Сколько текста аргументов уместно произнести в вопросе.
STEP_LIMIT = 120


def _describe_step(intent: "Intent", registry: ToolRegistry) -> str:
    """Назвать шаг так, чтобы вопрос о нём можно было понять на слух.

    Имя инструмента для этого не годится: «memory.remember» на слух не значит
    ничего. Берётся **описание из каталога** — та самая строка, по которой
    инструмент выбирает модель, то есть человеческая по определению.

    Значения аргументов в вопрос нужны, а их названия нет: соглашаются на то,
    что будет сделано, а не на устройство вызова.
    """
    found = registry.get(intent.tool)
    what = (found.spec.description if found else intent.tool).rstrip(".")
    # Описание начинается с заглавной («Записать факт в журнал»), а встаёт оно
    # в середину фразы «дальше нужно …». На письме это мелочь, на слух — ничто,
    # но в логе выглядит опечаткой, а логи тут читают.
    if what[:1].isupper() and not what[:2].isupper():
        what = what[0].lower() + what[1:]
    shown = ", ".join(
        str(value) for value in intent.arguments.values() if str(value).strip()
    )
    if not shown:
        return what
    return f"{what}: {shorten(shown, limit=STEP_LIMIT)}"


def _steps_done(outcome: "Outcome", language: str) -> str:
    """Приставка «столько-то шагов сделал» к неудачному концу плана.

    Без неё отказ звучит так, будто не сделано ничего, — а половина работы уже
    выполнена, и владельцу важно знать, с какого места продолжать.
    """
    count = len(outcome.steps)
    if not count:
        return ""
    if language == "en":
        return f"Did {count} step{'s' if count > 1 else ''}. "
    return f"Сделал шагов: {count}. "


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
        modes: Modes | None = None,
        situation: Situation | None = None,
        stt: Any = None,
        jobs: Jobs | None = None,
    ) -> None:
        self._llm = llm
        #: Распознавание — только чтобы показать его расход. Облачное считает
        #: секунды звука, и они должны быть видны там же, где токены: лимит
        #: иначе кончится незаметно, посреди вечера.
        self._stt = stt
        self._memory = memory
        self._registry = registry
        self._skills = skills
        self._persona = persona or Persona()
        self._learner = learner
        #: Режимы и обстановка приходят снаружи: их же читают конвейер, скиллы
        #: и резолвер модели. Свои завести здесь означало бы два разных
        #: состояния с одним смыслом.
        self._modes = modes if modes is not None else Modes()
        self._situation = (
            situation if situation is not None else Situation(modes=self._modes)
        )
        #: Фоновые поручения. Свои завести нельзя: их останавливает
        #: `ServiceRunner`, а докладывают они в ту же шину.
        self._jobs = jobs if jobs is not None else Jobs()

    @tool(name="chat", reversible=True)
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
        # Режим «отвечай коротко» тут не проверяется намеренно: он живёт в
        # `LLMService`, через который проходит любой текст вслух. Пока проверка
        # была здесь, поиск в том же режиме зачитывал три предложения из
        # Википедии — он про режим не знал.
        system = f"{_DIALOG_SYSTEM[code]} {self._persona.style(code)}"
        # Обстановка вместо одной только даты: разговор тоже выигрывает от того,
        # что ассистент знает, в каком он режиме и что делал минуту назад.
        answer = await self._llm.ask(
            text,
            task="dialog",
            system=f"{system} {self._situation.describe(code)}",
            context=context or None,
        )
        await self._memory.remember(f"Вопрос: {text}", tags=("dialog",))
        return ToolResult.success(answer, speech=answer)

    @tool(name="plan", reversible=False)
    async def plan(self, goal: str, language: str = "ru") -> ToolResult:
        """Выполнить составную просьбу из нескольких действий подряд.

        Годится, когда одной командой не обойтись и следующий шаг зависит от
        того, что получилось на предыдущем: «найди, что играет, и добавь в
        избранное», «посмотри, что за ошибка на экране, и найди её причину».
        Для одиночной команды не нужен — её выполняет обычный инструмент.

        :param goal: просьба целиком, своими словами владельца.
        :param language: язык, на котором отвечать.
        """
        code = _language(language)
        if not self._llm.available:
            return ToolResult.failure(
                "Языковая модель не настроена: задай JARVIS_OPENROUTER_KEY в .env",
                speech={
                    "ru": "Языковая модель не подключена. Добавь ключ в настройки.",
                    "en": "The language model isn't connected. Add the key in settings.",
                },
            )

        outcome = await self._planner().run(goal, language=code)

        # Упёрлись в необратимое — спрашиваем. Разрешение приходит голосом
        # владельца, и подтверждённый шаг не получает никаких особых прав: он
        # идёт тем же путём, что и та же команда, сказанная вслух сразу.
        if outcome.blocked is not None:
            done = _steps_done(outcome, code)
            what = _describe_step(outcome.blocked, self._registry)
            return ToolResult.asking(
                outcome.blocked,
                value={
                    "steps": [step.tool for step in outcome.steps],
                    "blocked": outcome.blocked.tool,
                },
                question={
                    "ru": f"{done}Дальше нужно {what}. Делать?",
                    "en": f"{done}Next step is {what}. Shall I?",
                },
            )

        if outcome.stopped:
            done = _steps_done(outcome, code)
            return ToolResult.failure(
                f"план остановлен: {outcome.stopped}",
                speech={
                    "ru": f"{done}Дальше не получилось: {outcome.stopped}.",
                    "en": f"{done}Could not continue: {outcome.stopped}.",
                },
            )

        answer = outcome.answer or _DONE_PLAIN[code]
        return ToolResult.success(
            {"steps": [step.tool for step in outcome.steps], "answer": answer},
            speech=answer,
        )

    def _planner(self) -> Planner:
        """Цикл создаётся на каждую просьбу: своего состояния он не держит."""
        return Planner(
            llm=self._llm, registry=self._registry, situation=self._situation
        )

    @tool(
        name="later",
        phrases=["займись {goal}", "сделай в фоне {goal}", "потом сделай {goal}",
                 "work on {goal}", "do {goal} in the background"],
        reversible=False,
    )
    async def later(self, goal: str, language: str = "ru") -> ToolResult:
        """Взяться за долгую работу в фоне и доложить, когда будет готово.

        Для того, что займёт минуты и чего не ждут стоя: разобраться в логах,
        собрать что-нибудь, подготовить. Ассистент отвечает сразу и заговорит
        сам, когда закончит.

        :param goal: что сделать, своими словами владельца.
        :param language: язык, на котором докладывать.
        """
        code = _language(language)
        if not self._llm.available:
            return ToolResult.failure(
                "Языковая модель не настроена: задай JARVIS_OPENROUTER_KEY в .env",
                speech={
                    "ru": "Языковая модель не подключена. Добавь ключ в настройки.",
                    "en": "The language model isn't connected. Add the key in settings.",
                },
            )

        job = self._jobs.submit(goal, self._carry_out(goal, code), language=code)
        if job is None:
            return ToolResult.failure("все места заняты", speech=busy_line(code))

        return ToolResult.success(
            {"job": job.id, "title": job.title},
            speech={
                "ru": "Займусь и доложу.",
                "en": "I'll take care of it and report back.",
            },
        )

    async def _carry_out(self, goal: str, language: str) -> str:
        """Что именно делает фоновое поручение: тот же цикл, только не в спешке.

        Доклад собирается здесь, а не в `Jobs`: тот не должен знать, что внутри
        задачи был план, — завтра там окажется что-нибудь другое.
        """
        outcome = await self._planner().run(goal, language=language)
        if outcome.blocked is not None:
            # В фоне спрашивать не у кого: человек занят другим и на вопрос,
            # прозвучавший через пять минут, отвечать не готов. Докладываем, обо
            # что упёрлись, и ждём прямой команды.
            what = _describe_step(outcome.blocked, self._registry)
            return (
                f"дошёл до шага {what}, а такое сам не делаю"
                if language == "ru"
                else f"got as far as {what}, which I won't do on my own"
            )
        if outcome.stopped:
            return outcome.stopped
        return outcome.answer or ("готово" if language == "ru" else "done")

    @tool(
        name="jobs",
        phrases=["чем ты занят", "что в работе", "какие задачи", "what are you doing",
                 "what is running"],
        reversible=True,
    )
    async def jobs(self, language: str = "ru") -> ToolResult:
        """Рассказать, какие поручения сейчас в работе."""
        code = _language(language)
        running = self._jobs.running
        return ToolResult.success(
            {"running": [{"id": job.id, "title": job.title} for job in running]},
            speech=describe_jobs(running, code),
        )

    @tool(
        name="help",
        phrases=["что ты умеешь", "список команд", "помощь", "what can you do", "help"],
        reversible=True,
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

    @tool(
        name="reload_skill",
        routable=False,
        phrases=[
            "переподключи модуль {skill}",
            "переподключи {skill}",
            "перезагрузи модуль {skill}",
            "перезагрузи скилл {skill}",
            "перезапусти модуль {skill}",
            "обнови модуль {skill}",
            "reload module {skill}",
            "reload skill {skill}",
        ],
        reversible=False,
    )
    async def reload_skill(self, skill: str) -> ToolResult:
        """Перезагрузить скилл с диска без перезапуска приложения.

        Фразы есть, а в каталог модели инструмент не уходит (`routable=False`),
        и это не противоречие: `phrase_index` строится по всем инструментам, и
        шаблон работает **до** модели, то есть бесплатно. Платить за него
        входными токенами в каждом запросе незачем — просят перезагрузку редко
        и говорят при этом одинаково.

        Имя приходит из речи как попало — «браузер», «страницу», «телеграм», —
        поэтому сверяется со списком загруженных, а не подставляется напрямую:
        иначе `reload` получил бы несуществующее имя и ответил бы «скилл не
        загружен» вместо понятного «такого модуля нет».

        :param skill: имя скилла.
        """
        found = self._skills.find(skill) or self._fresh_skill(skill)
        if found is None:
            known = ", ".join(self._skills.loaded) or "ни одного"
            return ToolResult.failure(
                f"Скилл {skill!r} не найден. Загружены: {known}",
                speech={
                    "ru": f"Не нашёл модуль {skill}. Есть: {known}.",
                    "en": f"No module named {skill}. Available: {known}.",
                },
            )
        # Вслух отвечаем теми словами, какими спросили: «модуль браузер
        # перезагружен» вместо «модуль browser». Настоящее имя нужно коду, а
        # человеку — его собственное.
        spoken, skill = skill, found

        fresh = skill not in self._skills.loaded
        try:
            # `adopt`, а не `reload`: перезагрузить можно только загруженное, а
            # сюда теперь приходят и модули, появившиеся на диске уже после
            # запуска, — ровно то, что пишет себе сам ассистент.
            record = await self._skills.adopt(skill)
        except Exception as exc:
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                speech={
                    "ru": f"Не удалось подключить модуль {spoken}.",
                    "en": f"Couldn't load module {spoken}.",
                },
            )
        return ToolResult.success(
            {"skill": record.name, "tools": list(record.scope.tool_names), "fresh": fresh},
            speech={
                "ru": (
                    f"Модуль {spoken} подключён." if fresh
                    else f"Модуль {spoken} перезагружен."
                ),
                "en": (
                    f"Module {spoken} connected." if fresh
                    else f"Module {spoken} reloaded."
                ),
            },
        )

    def _fresh_skill(self, spoken: str) -> str | None:
        """Найти скилл, который лежит на диске, но ещё не загружен.

        `find` знает только загруженные — у незагруженного неоткуда взять
        произносимые имена, они объявлены внутри самого модуля. Поэтому здесь
        сравнение идёт по именам каталогов, той же лестницей и тем же порогом.
        """
        names = {
            candidate.name: candidate.name
            for candidate in self._skills.candidates()
            if candidate.name not in self._skills.loaded
        }
        if not names:
            return None
        found = best_match(spoken, names, similarity=0.7)
        return names.get(found) if found else None

    @tool(name="set_model", routable=False, reversible=True)
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

    @tool(name="status", phrases=["статус", "как дела", "status", "how are you"], reversible=True)
    async def status(self) -> ToolResult:
        """Показать состояние скиллов и подключённых моделей."""
        health = await self._skills.health()
        broken = [name for name, state in health.items() if not state.ok]
        spending = self._llm.spending
        build = current()
        payload = {
            "version": build.label,
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
        # Версию проговариваем: «какая у тебя версия» спрашивают ровно тогда,
        # когда сомневаются, что запущено свежее.
        build_line = f" Версия {build.version}."
        if broken:
            speech = {
                "ru": f"Есть проблемы в модулях: {', '.join(broken)}.{cost}{build_line}",
                "en": f"Problems in modules: {', '.join(broken)}.{cost}{build_line}",
            }
        else:
            speech = {
                "ru": f"Всё работает: {len(health)} модулей, "
                      f"{len(self._registry)} команд.{cost}{build_line}",
                "en": f"All good: {len(health)} modules, "
                      f"{len(self._registry)} commands.{cost}{build_line}",
            }
        return ToolResult.success(payload, speech=speech)

    # --- режимы ------------------------------------------------------------
    #
    # Просьбы вроде «полчаса не слушай» — не действия, а поведение на время, и
    # выразить их каталогом из одних глаголов было нечем. Механизм лежит в
    # `core/state.py`; здесь только способы включить и выключить голосом.

    @tool(
        name="sleep",
        # Окончание у «минуты» своё на каждое число («21 минуту», «22 минуты»,
        # «30 минут»), а шаблон сравнивается целиком — отсюда три написания на
        # глагол. Выглядит избыточно, стоит того: без них каждая просьба с
        # числом уезжает в платную модель. Остальные формулировки ей и
        # достанутся — один раз, дальше их запомнит резолвер `learned`.
        phrases=[
            "не слушай", "не слушай меня", "не реагируй", "поспи", "отдохни",
            "помолчи", "спи", "не слушай полчаса", "поспи полчаса",
            "полчаса не слушай", "не слушай меня полчаса",
            "не слушай {minutes} минут", "не слушай {minutes} минуты",
            "не слушай {minutes} минуту",
            "поспи {minutes} минут", "поспи {minutes} минуты",
            "поспи {minutes} минуту",
            "stop listening", "go to sleep", "don't listen",
            "stop listening for {minutes} minutes",
        ],
        reversible=True,
    )
    async def sleep(self, minutes: int = DEFAULT_SLEEP_MINUTES) -> ToolResult:
        """Перестать принимать команды на заданное время.

        Ассистент продолжает работать, но всё услышанное пропускает мимо: до
        роутера не доходит ничего, а значит и денег это не стоит. Вернуть
        обратно можно в любой момент — фразой «Джарвис, проснись».

        :param minutes: сколько молчать; 0 — пока не скажут иначе.
        """
        span = max(0, minutes)
        self._modes.on(DEAF, minutes=span)
        # Способ вернуться называется вслух намеренно: режим этот выключает
        # ассистента целиком, и человек должен услышать, чем его включить
        # обратно, — иначе выглядит как поломка.
        if span:
            speech = {
                "ru": (
                    f"Не слушаю {span} {minutes_word(span)}. "
                    f"Скажи «Джарвис, проснись», если понадоблюсь раньше.",
                    f"Молчу {span} {minutes_word(span)}. "
                    f"Позови «Джарвис, проснись», когда буду нужен.",
                ),
                "en": (
                    f"Not listening for {span} minutes. "
                    f"Say “Jarvis, wake up” to bring me back.",
                ),
            }
        else:
            speech = {
                "ru": (
                    "Не слушаю, пока не скажешь «Джарвис, проснись».",
                    "Молчу до тех пор, пока не позовёшь: «Джарвис, проснись».",
                ),
                "en": ("Not listening until you say “Jarvis, wake up”.",),
            }
        return ToolResult.success({"mode": DEAF, "minutes": span}, speech=speech)

    @tool(
        name="be_brief",
        phrases=[
            "отвечай покороче", "покороче", "отвечай коротко", "говори короче",
            "короче отвечай", "be brief", "keep it short", "shorter answers",
        ],
        reversible=True,
    )
    async def be_brief(self, minutes: int = 0) -> ToolResult:
        """Отвечать короче обычного — одним предложением.

        :param minutes: на сколько; 0 — пока не скажут иначе.
        """
        self._modes.on(BRIEF, minutes=max(0, minutes))
        return ToolResult.success(
            {"mode": BRIEF, "minutes": max(0, minutes)},
            speech={
                "ru": ("Буду краток.", "Понял, коротко."),
                "en": ("I'll keep it short.", "Understood, briefly."),
            },
        )

    @tool(name="as_usual", phrases=list(WAKE_PHRASES), routable=False, reversible=True)
    async def as_usual(self) -> ToolResult:
        """Вернуться к обычному поведению: выключить все режимы разом.

        Один выключатель на всё, а не по инструменту на режим. Причин две.
        Голосом неудобно вспоминать, какой именно режим мешает, — «как обычно»
        говорят про всё сразу. И каталог инструментов уезжает в модель на каждой
        неузнанной фразе, то есть каждый лишний инструмент — это деньги на
        каждой фразе.

        В каталог для модели он не идёт намеренно: в режиме «не слушаю» до
        роутера вообще ничего не доходит, кроме фраз пробуждения, и они
        сверяются буквально.
        """
        was = self._modes.clear()
        if not was:
            return ToolResult.success(
                [],
                speech={
                    "ru": ("Я и так в обычном режиме.", "Ничего и не включено."),
                    "en": ("I'm already in the usual mode.",),
                },
            )
        return ToolResult.success(
            [mode.name for mode in was],
            speech={
                "ru": ("Снова слушаю, сэр.", "Вернулся к обычной работе."),
                "en": ("Listening again, sir.", "Back to normal."),
            },
        )

    @tool(
        name="modes",
        phrases=["какие режимы", "в каком ты режиме", "какой режим",
                 "что у тебя включено", "what mode are you in", "active modes"],
        routable=False,
        reversible=True,
    )
    async def modes(self) -> ToolResult:
        """Перечислить включённые режимы.

        Режим меняет поведение молча и надолго — ровно тот случай, когда
        состояние обязано быть видимым. Иначе «почему он не отвечает» разбирать
        нечем.
        """
        active = self._modes.all()
        listed = self._modes.describe("ru")
        listed_en = self._modes.describe("en")
        if not active:
            return ToolResult.success(
                [],
                speech={
                    "ru": ("Работаю как обычно.", "Никаких особых режимов."),
                    "en": ("Working as usual.",),
                },
            )
        now = time.monotonic()
        return ToolResult.success(
            [{"name": mode.name, "seconds": round(mode.remaining(now))} for mode in active],
            speech={"ru": f"Сейчас: {listed}.", "en": f"Right now: {listed_en}."},
        )

    async def _forgettable(self) -> list[tuple[float, str]]:
        """Собрать всё, что можно отменить, вместе со временем записи.

        Возвращает пары «когда» и «чем отменять»: пустое имя — резолвер
        выученных формулировок (он объект, а не инструмент), остальное — имена
        инструментов `*.forget_last` у скиллов, которые учатся сами.
        """
        found: list[tuple[float, str]] = []
        if self._learner is not None and self._learner.last_at:
            found.append((self._learner.last_at, ""))

        # Имя инструмента — договорённость (`FORGET_TOOL`), поэтому новый
        # самообучающийся скилл попадает под общую отмену без правки ядра.
        for spec in self._registry.specs():
            if spec.name == f"{NAMESPACE}.{FORGET_TOOL}":
                continue
            if not spec.name.endswith(f".{FORGET_TOOL}"):
                continue
            # Спрашиваем по схеме, а не по факту вызова: инструмент без
            # `apply` ответил бы отказом, неотличимым от «мне нечего забывать».
            if "apply" not in spec.parameters.get("properties", {}):
                logger.warning(
                    "%s не принимает apply — отмена его не увидит. Добавь этот "
                    "параметр: при apply=False инструмент только сообщает "
                    "{'what': …, 'at': …}, ничего не стирая",
                    spec.name,
                )
                continue
            seen = await self._registry.invoke(spec.name, {"apply": False})
            if not seen.ok or not isinstance(seen.value, Mapping) or not seen.value.get("what"):
                continue
            found.append((float(seen.value.get("at") or 0.0), spec.name))
        return found

    @tool(
        name="forget_last",
        phrases=["не сохраняй в память", "не запоминай", "не запоминай это",
                 "забудь это", "забудь последнюю команду", "не надо это запоминать",
                 "don't remember that", "forget that", "forget the last command"],
        reversible=False,
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

        **Отменяется одно событие, а не по записи у каждого, кто учится.**
        Сперва все кандидаты опрашиваются «что и когда», потом забывается
        только самое свежее — и вместе с ним то, что записано в те же
        секунды, потому что одна команда вполне могла научить сразу двоих
        (формулировку и кнопку). Живой случай 01.08.2026: владелец отменял
        разбор фразы, сказанной сорок секунд назад, а вместе с ней слетел
        верный рецепт кнопки, выученный девятью минутами раньше.
        """
        candidates = await self._forgettable()
        if candidates:
            newest = max(moment for moment, _ in candidates)
            # Одна команда учит сразу нескольких, и записи ложатся в одну
            # секунду. Всё, что старше окна, к этой отмене отношения не имеет.
            group = [name for moment, name in candidates if newest - moment <= FORGET_WINDOW_S]
            dropped = [
                f"{name} (запомнено {time.strftime('%H:%M:%S', time.localtime(moment))})"
                for moment, name in candidates
                if newest - moment > FORGET_WINDOW_S
            ]
            if dropped:
                logger.info("Не трогаю более раннее: %s", "; ".join(dropped))
        else:
            group = []

        forgotten: list[str] = []
        for name in group:
            if name == "":
                phrase = await self._learner.reject() if self._learner else ""
                if phrase:
                    forgotten.append(phrase)
                continue
            result = await self._registry.invoke(name, {})
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
        # Что именно забыто — в лог и в значение: там имена инструментов,
        # подстановки вида {control} и адреса сайтов. Вслух такое произносить
        # нельзя, это не фраза, а внутренности.
        logger.info("Забыто по команде: %s", "; ".join(forgotten))
        return ToolResult.success(
            forgotten,
            speech={
                "ru": "Забыл. Больше так делать не буду.",
                "en": "Forgotten. I won't do that again.",
            },
        )

    @tool(name="spending", phrases=["сколько потрачено", "расход токенов",
                                    "how much have you spent", "token usage"],
          reversible=True)
    async def spending(self) -> ToolResult:
        """Показать расход токенов с момента запуска."""
        report = self._llm.spending
        heard, seconds = _stt_spending(self._stt)

        said = {
            "ru": f"С запуска: {report.calls} запросов к модели, "
                  f"{report.total_tokens} токенов."
            if report.calls
            else "Модель ещё ни разу не вызывалась.",
            "en": f"Since start: {report.calls} model calls, "
                  f"{report.total_tokens} tokens."
            if report.calls
            else "The model hasn't been called yet.",
        }
        if heard:
            said["ru"] += f" Распознавание: {seconds:.0f} секунд звука."
            said["en"] += f" Speech: {seconds:.0f} seconds of audio."

        return ToolResult.success(
            {
                "calls": report.calls,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "cost": round(report.cost, 5),
                "by_task": dict(report.by_task),
                "stt_calls": heard,
                "stt_seconds": round(seconds, 1),
            },
            speech=said,
        )
