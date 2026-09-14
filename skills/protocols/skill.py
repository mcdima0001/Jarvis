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


def parse_protocols(raw: object) -> dict[str, list[str | dict[str, Any]]]:
    """Прочитать протоколы из настроек: имя → шаги.

    Шаг — строка-фраза или словарь ``{tool, args}``. Всё прочее отбрасывается:
    сломанный шаг в YAML не должен ронять загрузку скилла.
    """
    if not isinstance(raw, Mapping):
        return {}
    found: dict[str, list[str | dict[str, Any]]] = {}
    for name, steps in raw.items():
        if not isinstance(steps, Sequence) or isinstance(steps, str):
            continue
        clean: list[str | dict[str, Any]] = []
        for step in list(steps)[:MAX_STEPS]:
            if isinstance(step, str) and step.strip():
                clean.append(step.strip())
            elif isinstance(step, Mapping) and str(step.get("tool", "")).strip():
                args = step.get("args")
                clean.append({"tool": str(step["tool"]).strip(), "args": dict(args) if isinstance(args, Mapping) else {}})
        key = " ".join(str(name).split()).lower().strip("«»\"' ")
        if key and clean:
            found[key] = clean
    return found


def step_label(step: str | Mapping[str, Any]) -> str:
    """Как шаг назвать в докладе."""
    return step if isinstance(step, str) else str(step.get("tool", ""))


class ProtocolsSkill(Skill):
    """Протоколы — наборы действий по одной фразе."""

    meta = SkillMeta(
        name="protocols",
        description="Протоколы: одна фраза — набор действий",
        version="0.1.0",
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
        self.log.info("Протоколов: %d (%s)", len(self._protocols), ", ".join(self._protocols) or "пусто")

    async def health(self) -> HealthStatus:
        """Протоколов может и не быть — это не поломка."""
        return HealthStatus.healthy()

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

        steps = self._protocols[found]
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
