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
from typing import TYPE_CHECKING, Mapping

from jarvis.core.contracts import ToolResult
from jarvis.core.llm import LLMService
from jarvis.core.memory import Memory
from jarvis.core.persona import Persona
from jarvis.core.situation import Situation
from jarvis.core.state import BRIEF, DEAF, WAKE_PHRASES, Modes, minutes_word
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
    ) -> None:
        self._llm = llm
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

    @tool(name="as_usual", phrases=list(WAKE_PHRASES), routable=False)
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
