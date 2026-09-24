"""Протоколы: одна фраза — целый набор действий.

Как в фильме: «протокол «вечеринка»» — и дом делает всё сам. Просьба владельца
14.09.2026: «протокол работа» открывает рабочее, «протокол отбой» гасит всё.

**Шаги пишутся фразами** — теми же, что говорят вслух: «открой телеграм»,
«сделай потише». Разбираются они шаблонами фраз, как у роутера, — без модели,
бесплатно и мгновенно. Фраза, которую шаблоны не узнали, **не угадывается**:
протокол выполняет остальное и докладывает, какой шаг не понял. Угадывать шаг
моделью значило бы, что «протокол отбой» однажды сделает не то, и никто не
заметит. Можно и прямо инструментом: ``{tool: windows.set_volume, args: {level: 30}}``.

Протокол пишет сам владелец, поэтому его шаги — то же, что прямая команда, и
пометка обратимости на них не проверяется. А сам протокол помечен необратимым:
план не запустит его без вопроса.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.router.resolvers.phrase import PhraseResolver
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match
from jarvis.core.tools import tool

#: Порог узнавания имени протокола на слух: «протокол отбоя» — это «отбой».
SIMILARITY = 0.75
#: Пауза между шагами, сек: программа открывается не мгновенно, а следующий шаг
#: часто работает уже с ней.
PAUSE_S = 0.5
#: Свои инструменты: протокол внутри протокола — прямой путь к бесконечному кругу.
OWN_PREFIX = "protocols."
#: Сколько шагов в протоколе — предел здравого смысла и защита от опечатки в YAML.
MAX_STEPS = 20
#: Как часто смотреть, не появился ли повод (запущенная игра, например).
WATCH_EVERY_S = 3.0


def parse_steps(raw: object) -> list[str | dict[str, Any]]:
    """Шаги протокола: строки-фразы и записи ``{tool, args}``.

    Сломанный шаг отбрасывается молча: опечатка в YAML не должна ронять
    загрузку скилла и уносить с собой остальные протоколы.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    clean: list[str | dict[str, Any]] = []
    for step in list(raw)[:MAX_STEPS]:
        if isinstance(step, str) and step.strip():
            clean.append(step.strip())
        elif isinstance(step, Mapping) and str(step.get("tool", "")).strip():
            args = step.get("args")
            clean.append({
                "tool": str(step["tool"]).strip(),
                "args": dict(args) if isinstance(args, Mapping) else {},
            })
    return clean


def parse_protocols(raw: object) -> dict[str, "Protocol"]:
    """Прочитать протоколы из настроек: имя → протокол.

    Записывается протокол двумя способами. Короткий — просто список шагов, как
    было с самого начала. Полный — со словом «когда»: шаги, которые запускаются
    сами, и шаги «после», которые вернут всё обратно.
    """
    if not isinstance(raw, Mapping):
        return {}
    found: dict[str, Protocol] = {}
    for name, body in raw.items():
        key = " ".join(str(name).split()).lower().strip("«»\"' ")
        if not key:
            continue
        if isinstance(body, Mapping):
            steps = parse_steps(body.get("steps"))
            after = parse_steps(body.get("after"))
            when = body.get("when")
            watch = tuple(
                str(item).lower() for item in (when or {}).get("process", ())
                if isinstance(when, Mapping) and str(item).strip()
            )
        else:
            steps, after, watch = parse_steps(body), [], ()
        if steps or after:
            found[key] = Protocol(name=key, steps=steps, after=after, processes=watch)
    return found


@dataclass(frozen=True, slots=True)
class Protocol:
    """Протокол: что сделать, чем это отменить и по какому поводу запускать.

    Повод — имена процессов (`when.process`). Именно они, а не полноэкранное
    окно: полный экран — признак обманчивый, так же выглядит фильм, и протокол
    срабатывал бы посреди кино.
    """

    name: str
    steps: list[str | dict[str, Any]]
    after: list[str | dict[str, Any]]
    processes: tuple[str, ...] = ()

    @property
    def watched(self) -> bool:
        """Запускается ли сам."""
        return bool(self.processes)


def step_label(step: str | Mapping[str, Any]) -> str:
    """Как шаг назвать в докладе."""
    return step if isinstance(step, str) else str(step.get("tool", ""))


class ProtocolsSkill(Skill):
    """Протоколы — наборы действий по одной фразе."""

    meta = SkillMeta(
        name="protocols",
        description="Протоколы: одна фраза — набор действий",
        version="0.3.0",
        spoken=("протоколы", "protocols"),
    )

    async def on_setup(self) -> None:
        """Прочитать протоколы из настроек."""
        self._protocols = parse_protocols(self.context.setting("protocols", {}))
        self._pause = float(self.context.setting("pause_s", PAUSE_S))
        # Каталог фраз читается на каждом шаге заново: скиллы грузятся после нас,
        # и их шаблоны должны быть видны, когда протокол запускают, а не когда
        # этот скилл поднялся.
        self._phrases = PhraseResolver(self.tools)
        #: Как часто смотреть, не появился ли повод, и смотреть ли вообще.
        self._every = max(1.0, float(self.context.setting("watch_every_s", WATCH_EVERY_S)))
        self._watch = bool(self.context.setting("watch", True))
        #: Какой протокол сейчас «идёт» и из-за какого процесса.
        self._active: dict[str, str] = {}
        self.log.info("Протоколов: %d (%s)", len(self._protocols), ", ".join(self._protocols) or "пусто")

    async def on_start(self) -> None:
        """Начать смотреть за теми протоколами, у которых есть повод."""
        watched = [item for item in self._protocols.values() if item.watched]
        if watched and self._watch:
            self.context.scope.spawn(self._watching(watched), name="protocols-watch")
            self.log.info(
                "Слежу за поводами: %s", ", ".join(f"{item.name} ({len(item.processes)})" for item in watched)
            )

    async def health(self) -> HealthStatus:
        """Протоколов может и не быть — это не поломка."""
        return HealthStatus.healthy()

    # --- протокол, который запускается сам ---------------------------------

    async def _watching(self, watched: list[Protocol]) -> None:
        """Раз в несколько секунд смотреть, не появился ли повод.

        Список процессов стоит миллисекунды, а заметить запуск игры хочется до
        того, как загрузится уровень.
        """
        running = self._processes()
        if running is None:
            self.log.warning("Нет psutil — протоколы по поводу работать не будут")
            return
        while True:
            await asyncio.sleep(self._every)
            try:
                await self._look(watched, running)
            except Exception:  # noqa: BLE001 — наблюдатель не должен падать молча
                self.log.exception("Проверка повода сорвалась")

    async def _look(self, watched: list[Protocol], running: Any) -> None:
        """Одна проверка: повод появился или пропал."""
        for item in watched:
            found = await asyncio.to_thread(running, item.processes)
            was = self._active.get(item.name, "")
            if found and not was:
                self._active[item.name] = sorted(found)[0]
                self.log.info("Повод для «%s»: %s", item.name, self._active[item.name])
                # Сказать до шагов, а не после: они идут секундами, и молчащий
                # ассистент в этот момент неотличим от не сработавшего. Живой
                # запуск 24.09.2026: протокол отработал целиком и молча, и
                # владелец спросил, сработал ли он вообще.
                self._tell(f"{{address}}, протокол «{item.name}» запущен.")
                result = await self._carry_out(item.name, item.steps)
                failed = (result.value or {}).get("failed") if result.value else None
                if failed:
                    self._tell(f"В протоколе «{item.name}» не вышло: {', '.join(failed)}.")
            elif was and was not in found:
                self._active.pop(item.name, None)
                self.log.info("Повод для «%s» пропал: %s закрылся", item.name, was)
                if item.after:
                    await self._carry_out(f"{item.name} (отбой)", item.after)
                self._tell(f"Протокол «{item.name}» свёрнут, всё как было.")

    def _tell(self, text: str) -> None:
        """Сказать вслух о протоколе, который запустился сам.

        Через политику речи без вопроса: решать, уместно ли говорить сейчас,
        протоколу не положено — это одна забота на всю систему. Обращение
        подставит персона, поэтому в тексте оно полем `{address}`.
        """
        decision = self.context.announcer.offer(text, importance="normal", language="ru")
        self.log.debug("Протокол сказал (%s): %s", decision, text)

    def _processes(self) -> Any:
        """Чем смотреть за процессами. Живёт в скилле `windows` — Windows-only."""
        import importlib.util
        import sys
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "windows" / "power.py"
        if not path.exists():
            return None
        name = "jarvis_skills.windows_power"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return getattr(module, "running", None)

    @tool(
        phrases=["протокол {name}", "запусти протокол {name}", "включи протокол {name}",
                 "активируй протокол {name}", "protocol {name}", "run protocol {name}"],
        reversible=False,
    )
    async def run(self, name: str) -> ToolResult:
        """Выполнить протокол — набор действий, записанный владельцем.

        :param name: имя протокола, как его назвали.
        """
        found = self._find(name)
        if found is None:
            listed = ", ".join(self._protocols)
            return ToolResult.failure(
                f"протокола {name!r} нет; есть: {listed or 'ни одного'}",
                speech={
                    "ru": (f"Протокола {name} нет. Есть: {listed}." if listed
                           else "Протоколов пока нет. Их записывают в настройках модуля protocols."),
                    "en": f"There's no protocol {name}." if listed else "There are no protocols yet.",
                },
            )

        return await self._carry_out(found, self._protocols[found].steps)

    async def _carry_out(self, found: str, steps: list[Any]) -> ToolResult:
        """Выполнить шаги по очереди и доложить, что не вышло."""
        done: list[str] = []
        failed: list[str] = []
        for index, step in enumerate(steps):
            if index and self._pause > 0:
                await asyncio.sleep(self._pause)
            label = step_label(step)
            ok, why = await self._step(step)
            if ok:
                done.append(label)
                self.log.info("Протокол «%s», шаг %d: %s — ок", found, index + 1, label)
            else:
                failed.append(label)
                self.log.warning("Протокол «%s», шаг %d: %s — не вышло: %s", found, index + 1, label, why)

        payload = {"protocol": found, "done": done, "failed": failed}
        if not failed:
            return ToolResult.success(
                payload, speech={"ru": f"Протокол «{found}» выполнен.", "en": f"Protocol {found} complete."}
            )
        missed = "; ".join(failed)
        if not done:
            return ToolResult.failure(
                f"протокол {found}: не выполнился ни один шаг",
                speech={"ru": f"Протокол «{found}» не выполнился: {missed}.", "en": f"Protocol {found} failed."},
            )
        return ToolResult.success(
            payload,
            speech={
                "ru": f"Протокол «{found}» выполнен не весь. Не вышло: {missed}.",
                "en": f"Protocol {found} partly done.",
            },
        )

    @tool(phrases=["какие протоколы", "какие есть протоколы", "список протоколов", "what protocols"], reversible=True)
    async def list_protocols(self) -> ToolResult:
        """Назвать записанные протоколы."""
        names = list(self._protocols)
        if not names:
            return ToolResult.success(
                [], speech={"ru": "Протоколов пока нет.", "en": "There are no protocols yet."}
            )
        listed = ", ".join(names)
        return ToolResult.success(names, speech={"ru": f"Протоколы: {listed}.", "en": f"Protocols: {listed}."})

    # --- внутреннее ----------------------------------------------------------

    def _find(self, said: str) -> str | None:
        asked = " ".join(said.split()).lower().strip("«»\"' .,")
        if not asked:
            return None
        if asked in self._protocols:
            return asked
        return best_match(asked, list(self._protocols), similarity=SIMILARITY)

    async def _step(self, step: str | Mapping[str, Any]) -> tuple[bool, str]:
        """Выполнить один шаг. Возвращает, получилось ли, и почему нет."""
        if isinstance(step, Mapping):
            name, arguments = str(step["tool"]), dict(step.get("args") or {})
        else:
            intent = await self._phrases.resolve(Utterance(text=step))
            if intent is None:
                return False, "шаблоны фраз его не узнали"
            name, arguments = intent.tool, dict(intent.arguments)
        if name.startswith(OWN_PREFIX):
            return False, "протокол внутри протокола не запускается"
        if not self.tools.has(name):
            return False, f"нет инструмента {name}"
        result = await self.tools.invoke(name, arguments)
        return result.ok, str(result.error or "")
