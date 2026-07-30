"""Composition root — единственное место, где собирается вся система.

Здесь и только здесь создаются конкретные реализации и связываются друг с
другом через конструкторы. Ни глобальных переменных, ни синглтонов, ни
service locator: любой компонент подменяется правкой одной строки в `build`,
а в тестах — передачей другого объекта.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Sequence

from jarvis.core.audio import AudioStack, build_audio
from jarvis.core.builtin import NAMESPACE as CORE_NAMESPACE
from jarvis.core.builtin import CoreTools
from jarvis.core.bus import LocalEventBus
from jarvis.core.config import JarvisConfig
from jarvis.core.contracts import (
    SystemStarted,
    SystemStopping,
    ToolResult,
    Utterance,
    detect_language,
)
from jarvis.core.lifecycle import EARS, VOICE, ServiceRunner
from jarvis.core.llm import LLMService, ProfileRegistry, build_provider
from jarvis.core.memory import Memory, build_memory
from jarvis.core.persona import FAREWELL, GREETING, Persona
from jarvis.core.router import (
    AliasResolver,
    Dispatcher,
    FallbackResolver,
    LearnedResolver,
    LLMResolver,
    PhraseResolver,
    Resolver,
    Router,
)
from jarvis.core.runtime import BlockingWorker
from jarvis.core.skills import SkillManager
from jarvis.core.stt import build_stt
from jarvis.core.tools import ToolRegistry, collect_tools
from jarvis.core.tts import build_tts
from jarvis.core.voice import VoicePipeline

logger = logging.getLogger(__name__)


def _quiet_broken_connections() -> None:
    """Не показывать стек, когда собеседник просто закрыл соединение.

    Расширение браузера отключается когда угодно — служебный поток уснул,
    вкладка закрылась, браузер вышел. На Windows это прилетает уже после
    закрытия транспорта, в служебном обработчике asyncio, и тот печатает
    полный стек `ConnectionResetError`. Событие рядовое, а выглядит как
    авария; в логе, который читают глазами, это дороже, чем кажется.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(target: asyncio.AbstractEventLoop, context: dict) -> None:
        error = context.get("exception")
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError)):
            logger.debug("Соединение оборвал собеседник: %s", error)
            return
        if previous is None:
            target.default_exception_handler(context)
        else:
            previous(target, context)

    loop.set_exception_handler(handler)


@dataclass(slots=True)
class JarvisApp:
    """Собранное приложение и его жизненный цикл."""

    config: JarvisConfig
    events: LocalEventBus
    registry: ToolRegistry
    memory: Memory
    llm: LLMService
    skills: SkillManager
    persona: Persona
    router: Router
    dispatcher: Dispatcher
    pipeline: VoicePipeline
    audio: AudioStack
    worker: BlockingWorker
    runner: ServiceRunner
    #: Поднимались ли звук и модели. Отчёт о сборке обходится без них.
    models_loaded: bool = False

    # --- сборка ------------------------------------------------------------

    @classmethod
    def build(cls, config: JarvisConfig) -> "JarvisApp":
        """Создать все компоненты и связать их между собой."""
        worker = BlockingWorker(config.runtime.worker_threads)
        events = LocalEventBus()
        registry = ToolRegistry(events=events, default_timeout=config.runtime.tool_timeout)

        memory = build_memory(config.memory)

        providers = {
            name: build_provider(provider_config)
            for name, provider_config in config.llm.providers.items()
        }
        llm = LLMService(
            providers=providers,
            profiles=ProfileRegistry(
                config.llm.profiles,
                default_task=config.llm.default_task,
            ),
        )

        persona = Persona(
            phrases=config.persona.phrases,
            address=config.persona.address,
            replace=config.persona.replace,
            default_language=config.app.language,
            greet_on_start=config.persona.greet_on_start,
            farewell_on_stop=config.persona.farewell_on_stop,
        )

        audio = build_audio(config.audio)
        stt = build_stt(config.stt, worker)
        tts = build_tts(config.tts, worker, sink=audio.sink)

        skills = SkillManager(
            config=config.skills,
            events=events,
            tools=registry,
            memory=memory,
            llm=llm,
            tts=tts,
            root=config.root,
        )

        # Кеш удачных разборов моделью: один объект и подсказывает роутеру, и
        # запоминает после успеха, и забывает по команде.
        learner = LearnedResolver(
            memory.documents,
            section=config.router.learned_section,
            enabled=config.router.learn_commands,
        )

        router = Router(
            _build_resolvers(
                config.router.resolvers,
                registry=registry,
                llm=llm,
                config=config,
                learner=learner,
            ),
            threshold=config.router.confidence_threshold,
            events=events,
        )
        dispatcher = Dispatcher(
            router=router,
            registry=registry,
            events=events,
            learner=learner if config.router.learn_commands else None,
        )

        # Встроенные инструменты ядра: диалог, справка, перезагрузка, модели.
        core_tools = CoreTools(
            llm=llm,
            memory=memory,
            registry=registry,
            skills=skills,
            persona=persona,
            learner=learner,
        )
        for core_tool in collect_tools(core_tools, namespace=CORE_NAMESPACE):
            registry.register(core_tool)

        pipeline = VoicePipeline(
            source=audio.source,
            sink=audio.sink,
            vad=audio.vad,
            wake_word=audio.wake_word,
            stt=stt,
            tts=tts,
            dispatcher=dispatcher,
            events=events,
            config=config.audio,
            persona=persona,
        )

        runner = ServiceRunner()
        # Порядок важен, метка — нет. Метка говорит, для чего сервис нужен:
        # слышать или говорить. Модели грузятся минутами (Whisper, Vosk,
        # Kokoro), и разным режимам запуска нужны разные их половины — отчёту о
        # сборке ни одна, одиночной команде только голос.
        for service, needs in (
            (worker, ""),
            (events, ""),
            (memory, ""),
            (llm, ""),
            (audio.sink, VOICE),
            (audio.source, EARS),
            (stt, EARS),
            (tts, VOICE),
            (skills, ""),
            (pipeline, EARS),
        ):
            runner.add(service, needs=needs)

        return cls(
            config=config,
            events=events,
            registry=registry,
            memory=memory,
            llm=llm,
            skills=skills,
            persona=persona,
            router=router,
            dispatcher=dispatcher,
            pipeline=pipeline,
            audio=audio,
            worker=worker,
            runner=runner,
        )

    # --- жизненный цикл ----------------------------------------------------

    async def start(self, *, ears: bool = True, voice: bool = True) -> None:
        """Поднять сервисы и загрузить скиллы.

        Половины голоса поднимаются отдельно, потому что стоят они минут, а
        нужны не всем режимам:

        * ``--check`` не нужна ни одна: ему важен каталог инструментов;
        * ``--say`` не нужны уши — текст команды уже написан, распознавать
          нечего, а Whisper поднимался впустую и открывал микрофон;
        * живому запуску нужны обе.

        :param ears: микрофон, распознавание, цикл прослушивания.
        :param voice: синтез и звуковой выход.
        """
        _quiet_broken_connections()
        skip = {name for name, needed in ((EARS, ears), (VOICE, voice)) if not needed}
        await self.runner.start_all(without=skip)
        self.models_loaded = ears and voice

        gaps = self.skills.missing_requirements()
        for skill, missing in gaps.items():
            logger.warning(
                "Скиллу %s не хватает инструментов: %s", skill, ", ".join(missing)
            )

        logger.info(
            "%s готов: скиллов %d, инструментов %d",
            self.config.app.name,
            len(self.skills.loaded),
            len(self.registry),
        )
        await self.events.publish(
            SystemStarted(source="app", skills=self.skills.loaded, tools=len(self.registry))
        )

    async def stop(self, reason: str = "") -> None:
        """Погасить приложение."""
        await self.events.publish(SystemStopping(source="app", reason=reason))
        await self.runner.stop_all()
        logger.info("%s остановлен", self.config.app.name)

    async def run(self) -> None:
        """Запустить и работать до сигнала остановки."""
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                # Windows: обработчики сигналов через loop недоступны,
                # остановку ловим по KeyboardInterrupt в __main__.
                pass

        await self.start()
        # Приветствие и прощание — свойство живого сеанса, а не запуска
        # сервисов: служебные режимы (`--check`, `--say`) остаются молчаливыми.
        if self.persona.greet_on_start:
            await self.pipeline.announce(GREETING)
        try:
            await stop_event.wait()
        finally:
            if self.persona.farewell_on_stop:
                await self.pipeline.announce(FAREWELL)
            await self.stop("получен сигнал остановки")

    # --- работа ------------------------------------------------------------

    async def say(
        self,
        text: str,
        *,
        source: str = "text",
        language: str | None = None,
    ) -> ToolResult:
        """Обработать текстовую команду тем же путём, что и голосовую.

        Язык у текста никто не сообщает — в отличие от голоса, где его даёт
        Whisper. Определяем по алфавиту, чтобы ответ пришёл на языке вопроса.
        """
        return await self.pipeline.handle(
            Utterance(
                text=text,
                language=language or detect_language(text, default=self.config.app.language),
                source=source,
            )
        )

    def summary(self) -> str:
        """Человекочитаемый отчёт о состоянии сборки (режим ``--check``)."""
        lines: list[str] = [
            f"Конфигурация:  {self.config.source}",
            f"Корень:        {self.config.root}",
            f"Резолверы:     {' -> '.join(self.router.resolvers)}",
            f"Порог:         {self.config.router.confidence_threshold}",
            f"Персона:       {self.persona.summary()}",
            "",
            f"Скиллы ({len(self.skills.loaded)}):",
        ]
        lines.extend(f"  - {name}" for name in self.skills.loaded)
        if not self.skills.loaded:
            lines.append("  (ни одного — положи модуль в skills/)")

        catalog = self.registry.catalog()
        lines += ["", f"Инструменты ({len(catalog.specs)}):"]
        for spec in catalog.specs:
            params = ", ".join(spec.parameters.get("properties", {})) or "—"
            lines.append(f"  {spec.name:<28} [{params}]  {spec.description}")

        phrases = self.registry.phrase_index()
        lines += ["", f"Фразы без обращения к LLM ({len(phrases)}):"]
        for phrase, tool_name in sorted(phrases.items()):
            lines.append(f"  {phrase!r} -> {tool_name}")

        lines += ["", "Модели по задачам:"]
        for task, model in self.llm.models().items():
            lines.append(f"  {task:<12} {model}")
        lines.append(
            f"  LLM {'настроена' if self.llm.available else 'НЕ настроена (работает заглушка)'}"
        )
        if not self.models_loaded:
            # Иначе успешный отчёт легко принять за проверку голоса целиком.
            lines += [
                "",
                "Звук, распознавание и синтез не поднимались — отчёт о сборке "
                "их не требует.",
            ]
        return "\n".join(lines)


def _build_resolvers(
    names: Sequence[str],
    *,
    registry: ToolRegistry,
    llm: LLMService,
    config: JarvisConfig,
    learner: LearnedResolver | None = None,
) -> list[Resolver]:
    """Собрать цепочку резолверов в порядке, заданном конфигом.

    Порядок — это и есть политика экономии: дешёвые детерминированные звенья
    идут первыми, LLM вызывается только если они не справились. Выученное
    стоит между синонимами и моделью: написанное человеком главнее, а модель
    дороже всего.
    """
    factories = {
        "phrase": lambda: PhraseResolver(registry),
        "alias": lambda: AliasResolver(registry, config.router.aliases),
        "learned": lambda: learner,
        "llm": lambda: LLMResolver(
            registry, llm, tasks=_intent_tasks(llm, config), learner=learner
        ),
        "fallback": lambda: FallbackResolver(),
    }

    resolvers: list[Resolver] = []
    for name in names:
        factory = factories.get(name)
        if factory is None:
            logger.warning(
                "Неизвестный резолвер %r в конфиге — пропускаю. Доступны: %s",
                name,
                ", ".join(sorted(factories)),
            )
            continue
        resolver = factory()
        if resolver is None:
            logger.debug("Резолвер %r собрать нечем — пропускаю", name)
            continue
        resolvers.append(resolver)

    if not resolvers:
        logger.warning("Цепочка резолверов пуста — использую phrase + fallback")
        resolvers = [PhraseResolver(registry), FallbackResolver()]
    return resolvers


def _intent_tasks(llm: LLMService, config: JarvisConfig) -> tuple[str, ...]:
    """Проверить задачи разбора команд и вернуть только настроенные.

    Опечатка в названии задачи иначе обошлась бы дорого: попытка молча
    провалилась бы в рантайме, а выглядело бы это как «модель не поняла». Те же
    грабли уже были с `router.aliases`, где синоним вёл на несуществующий
    инструмент.
    """
    known = llm.models()
    tasks = tuple(task for task in config.router.intent_tasks if task in known)
    for task in config.router.intent_tasks:
        if task not in known:
            logger.warning(
                "Задача %r из router.intent_tasks не описана в llm.profiles — пропускаю. "
                "Есть: %s",
                task,
                ", ".join(sorted(known)),
            )
    if not tasks:
        logger.warning("Ни одной задачи для разбора команд — беру intent")
        return ("intent",)
    if len(tasks) > 1:
        logger.info(
            "Разбор команд: %s, при отказе — %s",
            known[tasks[0]],
            ", ".join(known[task] for task in tasks[1:]),
        )
    return tasks
