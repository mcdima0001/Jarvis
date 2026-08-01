"""Менеджер скиллов: обнаружение, загрузка, жизненный цикл, перезагрузка.

Скиллы грузятся автоматически из каталогов, указанных в конфиге. Регистрировать
их вручную не нужно, править ядро — тоже. Ошибка в одном скилле не мешает
загрузиться остальным: сбойный просто пропускается с записью в лог.

Перезагрузка (`reload`) работает потому, что все регистрации идут через
`SkillScope`: отозвали scope — от скилла в системе не осталось следов.
Файлового watcher'а пока нет, но добавить его поверх `reload()` — десяток строк.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Iterable

from jarvis.core.bus import EventBus
from jarvis.core.config import SkillsConfig
from jarvis.core.contracts import SkillLoaded, SkillUnloaded
from jarvis.core.errors import SkillError, SkillLoadError, SkillUnsupportedPlatform
from jarvis.core.situation import Situation
from jarvis.core.state import Modes
from jarvis.core.tools import ToolRegistry, collect_tools

from .base import HealthStatus, Skill
from .context import SkillContext
from .discovery import SkillCandidate, discover
from .loader import find_skill_class, import_module
from .scope import SkillScope

if TYPE_CHECKING:
    from jarvis.core.llm import LLMService
    from jarvis.core.memory import Memory
    from jarvis.core.tts import TTS

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SkillRecord:
    """Загруженный скилл и всё, что с ним связано."""

    candidate: SkillCandidate
    module: ModuleType
    instance: Skill
    scope: SkillScope
    started: bool = False

    @property
    def name(self) -> str:
        """Имя скилла из его паспорта."""
        return self.instance.meta.name

    @property
    def parent(self) -> str:
        """Главный скилл, если этот — подскилл. Иначе пусто."""
        return self.candidate.parent


class SkillManager:
    """Владеет всеми скиллами и их жизненным циклом."""

    def __init__(
        self,
        *,
        config: SkillsConfig,
        events: EventBus,
        tools: ToolRegistry,
        memory: "Memory",
        llm: "LLMService",
        tts: "TTS",
        root: Path,
        modes: "Modes | None" = None,
        situation: "Situation | None" = None,
    ) -> None:
        self._config = config
        self._events = events
        self._tools = tools
        self._memory = memory
        self._llm = llm
        self._tts = tts
        self._root = root
        # Свои — только чтобы менеджер собирался в тесте, где состояние
        # системы не при чём. В живой сборке приходят снаружи: те же объекты
        # читают конвейер и резолвер модели.
        self._modes = modes if modes is not None else Modes()
        self._situation = (
            situation if situation is not None else Situation(modes=self._modes)
        )
        self._records: dict[str, SkillRecord] = {}

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "skills"

    @property
    def loaded(self) -> tuple[str, ...]:
        """Имена загруженных скиллов."""
        return tuple(sorted(self._records))

    @property
    def versions(self) -> dict[str, str]:
        """Имя скилла -> его версия. Для одной строки в логе о том, что запущено."""
        return {name: record.instance.meta.version for name, record in self._records.items()}

    @property
    def tree(self) -> tuple[str, ...]:
        """Как скиллы лежат на диске: ``browser`` и ``browser/page``.

        Для отчёта о сборке: по одним именам инструментов не видно, что `page` —
        часть браузера, а не сосед. Подскилл идёт сразу за своим главным.
        """
        parents = sorted(name for name, item in self._records.items() if not item.parent)
        orphans = sorted(
            name
            for name, item in self._records.items()
            if item.parent and item.parent not in self._records
        )
        listed: list[str] = []
        for name in parents:
            listed.append(name)
            listed += sorted(
                f"{name}/{child}"
                for child, item in self._records.items()
                if item.parent == name
            )
        return tuple(listed + orphans)

    def get(self, name: str) -> Skill | None:
        """Вернуть экземпляр скилла по имени."""
        record = self._records.get(name)
        return record.instance if record else None

    # --- жизненный цикл ----------------------------------------------------

    async def start(self) -> None:
        """Загрузить все скиллы и запустить их."""
        await self.load_all()
        for record in list(self._records.values()):
            await self._start_record(record)

    async def stop(self) -> None:
        """Остановить и выгрузить все скиллы.

        Подскиллы гасятся раньше главных: наоборот означало бы, что подскилл
        доживает свои мгновения без того, на чём он держится.
        """
        for name in sorted(self._records, key=lambda item: not self._records[item].parent):
            await self.unload(name)

    async def load_all(self) -> None:
        """Обнаружить и загрузить скиллы из настроенных каталогов."""
        for candidate in discover(self._config.paths):
            if candidate.name in self._config.disabled:
                logger.info("Скилл %s отключён в конфиге", candidate.label)
                continue
            # Подскилл без своего главного не имеет смысла: он на нём и держится.
            # Порядок гарантирует `discover` — главные идут первыми.
            if candidate.parent and candidate.parent not in self._records:
                logger.info(
                    "Подскилл %s пропущен: главный скилл %s не загружен",
                    candidate.label,
                    candidate.parent,
                )
                continue
            try:
                await self.load(candidate)
            except SkillUnsupportedPlatform as exc:
                # Штатная ситуация: WindowsSkill на Linux — не ошибка сборки.
                logger.info("Скилл %s пропущен: %s", candidate.label, exc)
            except SkillError as exc:
                logger.error("Скилл %s не загружен: %s", candidate.label, exc)
            except Exception:
                logger.exception("Неожиданная ошибка при загрузке скилла %s", candidate.label)

    async def load(self, candidate: SkillCandidate, *, reload: bool = False) -> SkillRecord:
        """Загрузить один скилл: импорт, создание, регистрация инструментов."""
        module = import_module(candidate, reload=reload)
        skill_class = find_skill_class(module, path=candidate.path)
        meta = skill_class.meta

        if not meta.supported_here():
            raise SkillUnsupportedPlatform(
                f"рассчитан на {', '.join(meta.platforms)}, текущая система другая"
            )
        if meta.name in self._records:
            raise SkillLoadError(f"скилл с именем {meta.name!r} уже загружен")

        instance = skill_class()
        scope = SkillScope(skill=meta.name, events=self._events, tools=self._tools)
        context = SkillContext(
            skill=meta.name,
            config=self._config.settings_for(meta.name),
            logger=logging.getLogger(f"jarvis.skills.{meta.name}"),
            events=self._events,
            tools=self._tools,
            memory=self._memory,
            llm=self._llm,
            tts=self._tts,
            scope=scope,
            root=self._root,
            modes=self._modes,
            situation=self._situation,
        )

        try:
            await instance.setup(context)
            for tool in collect_tools(instance, namespace=meta.name):
                scope.register_tool(tool)
        except Exception as exc:
            await scope.revoke()
            raise SkillLoadError(f"инициализация не удалась: {exc}") from exc

        record = SkillRecord(candidate=candidate, module=module, instance=instance, scope=scope)
        self._records[meta.name] = record

        logger.info(
            "Скилл %s v%s загружен: %d инструмент(ов)",
            candidate.label,
            meta.version,
            len(scope.tool_names),
        )
        self._events.emit(
            SkillLoaded(
                source="skills",
                skill=meta.name,
                version=meta.version,
                tools=scope.tool_names,
            )
        )
        return record

    async def unload(self, name: str) -> None:
        """Остановить скилл и отозвать все его регистрации."""
        record = self._records.pop(name, None)
        if record is None:
            return
        try:
            if record.started:
                await record.instance.on_stop()
        except Exception:
            logger.exception("Скилл %s упал при остановке", name)
        finally:
            await record.scope.revoke()

        logger.info("Скилл %s выгружен", record.candidate.label)
        self._events.emit(SkillUnloaded(source="skills", skill=name))

    async def reload(self, name: str) -> SkillRecord:
        """Перезагрузить скилл с диска, не перезапуская приложение."""
        record = self._records.get(name)
        if record is None:
            raise SkillError(f"Скилл {name!r} не загружен")

        candidate = record.candidate
        # Подскиллы держатся на инструментах главного: пережить его перезагрузку
        # они не могут, поэтому едут вместе с ним.
        children = [
            item.candidate for item in self._records.values() if item.parent == name
        ]
        for child in children:
            await self.unload(child.name)

        await self.unload(name)
        reloaded = await self.load(candidate, reload=True)
        await self._start_record(reloaded)
        for child in children:
            try:
                await self._start_record(await self.load(child, reload=True))
            except SkillError as exc:
                logger.error("Подскилл %s не вернулся: %s", child.label, exc)
        logger.info("Скилл %s перезагружен", name)
        return reloaded

    async def health(self) -> dict[str, HealthStatus]:
        """Опросить состояние всех скиллов."""
        report: dict[str, HealthStatus] = {}
        for name, record in self._records.items():
            try:
                report[name] = await record.instance.health()
            except Exception as exc:
                report[name] = HealthStatus.degraded(f"{type(exc).__name__}: {exc}")
        return report

    def missing_requirements(self) -> dict[str, tuple[str, ...]]:
        """Скиллы, чьи зависимости (чужие инструменты) не зарегистрированы."""
        gaps: dict[str, tuple[str, ...]] = {}
        for name, record in self._records.items():
            absent = tuple(t for t in record.instance.meta.requires if not self._tools.has(t))
            if absent:
                gaps[name] = absent
        return gaps

    # --- вспомогательное ---------------------------------------------------

    async def _start_record(self, record: SkillRecord) -> None:
        """Запустить фоновую часть скилла, изолировав ошибки."""
        try:
            await record.instance.on_start()
            record.started = True
        except Exception:
            logger.exception("Скилл %s не запустился", record.name)

    def candidates(self) -> Iterable[SkillCandidate]:
        """Кандидаты в скиллы на диске (без загрузки) — для диагностики."""
        return discover(self._config.paths)
