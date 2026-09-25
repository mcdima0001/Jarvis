"""Проверь себя: совпало ли то, что на машине, с тем, о чём просили.

Стенд `tools/agent_bench` 25.09.2026, двадцать просьб без своего скилла:
решено **3**, честный отказ 8 — и **ложный успех 9**. Почти в половине случаев
ассистент уверенно говорил «сделано», а на машине было другое: диспетчер задач
вместо диспетчера устройств, главная Википедии вместо статьи, «репозиторий
открыт» при открытом поиске. Ложный успех — худший исход, потому что его не
слышно: отказ человек переспросит, а «готово» примет на веру.

Отсюда правило: **то, что угадала модель, проверяется глазами.** Глаза тут
дешёвые — не снимок экрана, а текст: заголовки окон и адрес активной вкладки.
Их отдают инструменты скиллов, названные в конфиге (`agent.observe`): ядро не
знает скиллы по именам, как и у резолвера `verbatim`.

Проверяется только то, что угадывала модель, и только у инструментов, чей
результат виден (`@tool(shows=True)`). Шаблоны и выученное — проверенная
дорога, платить за их проверку незачем. Прогноз погоды — ответ сам по себе, и
смотреть на экран после него бессмысленно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.core.errors import LLMError, LLMNotConfigured
from jarvis.core.llm.protocol import Message

if TYPE_CHECKING:
    from jarvis.core.llm import LLMService
    from jarvis.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

#: Сколько ждать, пока сделанное проявится на экране: окно и вкладка
#: открываются не мгновенно. Ждём не вслепую, а пока картина не изменится, —
#: быстрое проверяется быстро.
SETTLE_S = 3.0
SETTLE_EVERY_S = 0.3

#: Предел длины того, что уходит в модель: заголовков бывают десятки.
SNAPSHOT_LIMIT = 700

_JUDGE = {
    "ru": (
        "Ты проверяешь, выполнена ли просьба владельца компьютера. Тебе дают "
        "просьбу, что сделал ассистент, и что сейчас на экране: заголовки окон и "
        "адрес активной вкладки браузера. Суди только по экрану, а не по словам "
        "ассистента. Ответь строго одной строкой: «ДА», если на экране именно "
        "то, о чём просили, или «НЕТ — <что на экране вместо нужного>». Если "
        "просьба — узнать что-то, а не открыть или сделать, ответь «ДА»."
    ),
    "en": (
        "You check whether the computer owner's request was fulfilled. You get "
        "the request, what the assistant did, and what is on screen now: window "
        "titles and the active browser tab address. Judge by the screen only, "
        "not by the assistant's words. Reply in exactly one line: \"YES\" if the "
        "screen shows exactly what was asked, or \"NO — <what is there instead>\". "
        "If the request was to learn something rather than open or do something, "
        "reply \"YES\"."
    ),
}

_YES = ("да", "yes")
_NO = ("нет", "no")


@dataclass(frozen=True, slots=True)
class Verdict:
    """Суждение проверки."""

    ok: bool
    #: Что на экране вместо нужного — пусто, если всё сошлось.
    reason: str = ""


def parse_verdict(text: str) -> Verdict:
    """Разобрать ответ проверки. Непонятный ответ — «да».

    Перекос в эту сторону намеренный: проверка — добавка, а не новая причина
    отказать. Если судья ответил мусором, лучше поступить как раньше, чем
    отменить сделанное из-за собственной ошибки.
    """
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    word = line.split(maxsplit=1)[0].strip("«»\"'.,:;!—-").lower() if line else ""
    if word in _NO:
        reason = line[len(line.split(maxsplit=1)[0]):].strip(" «»\"'.,:;!—-")
        return Verdict(ok=False, reason=reason or "на экране не то")
    return Verdict(ok=True)


def describe(values: Mapping[str, Any]) -> str:
    """Собрать увиденное в строку для модели. Чистая функция — её проверяют тесты."""
    parts: list[str] = []
    for source, value in values.items():
        if value in (None, "", {}, []):
            continue
        if isinstance(value, Mapping):
            shown = {key: value[key] for key in ("active", "title", "url", "windows") if value.get(key)}
            text = json.dumps(shown or dict(value), ensure_ascii=False)
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{source}: {text}")
    return "; ".join(parts)[:SNAPSHOT_LIMIT]


class Checker:
    """Глаза и суждение: что сейчас на экране и совпало ли это с целью."""

    def __init__(
        self,
        *,
        llm: "LLMService",
        registry: "ToolRegistry",
        observe: Mapping[str, Mapping[str, Any]],
        task: str = "plan",
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._observe = dict(observe)
        self._task = task

    @property
    def able(self) -> bool:
        """Есть ли чем смотреть. Нет инструментов — проверка молча не делается."""
        return any(self._registry.has(name) for name in self._observe)

    async def snapshot(self) -> str:
        """Что сейчас на экране, текстом. Пусто — смотреть нечем."""
        values: dict[str, Any] = {}
        for name, arguments in self._observe.items():
            if not self._registry.has(name):
                continue
            try:
                result = await self._registry.invoke(name, dict(arguments))
            except Exception as exc:  # noqa: BLE001 — глаза не важнее самой работы
                logger.debug("Не посмотрел через %s: %s", name, exc)
                continue
            if result.ok:
                values[name] = result.value
        return describe(values)

    async def settled(self, before: str) -> str:
        """Дождаться, пока картина изменится, но не дольше `SETTLE_S`."""
        deadline = time.monotonic() + SETTLE_S
        seen = await self.snapshot()
        while seen == before and time.monotonic() < deadline:
            await asyncio.sleep(SETTLE_EVERY_S)
            seen = await self.snapshot()
        return seen

    async def judge(self, *, goal: str, did: str, seen: str, language: str = "ru") -> Verdict:
        """Совпало ли увиденное с просьбой. Не смогли спросить — «да»."""
        if not seen:
            return Verdict(ok=True)
        messages = [
            Message.system(_JUDGE.get(language, _JUDGE["ru"])),
            Message.user(f"Просьба: {goal}\nСделано: {did}\nНа экране: {seen}"),
        ]
        try:
            response = await self._llm.complete(messages, task=self._task, max_tokens=60)
        except (LLMError, LLMNotConfigured) as exc:
            logger.debug("Проверку не спросил: %s", exc)
            return Verdict(ok=True)
        verdict = parse_verdict(response.text)
        logger.info(
            "Проверка «%s»: %s%s", goal, "сошлось" if verdict.ok else "НЕ сошлось",
            f" — {verdict.reason}" if verdict.reason else "",
        )
        return verdict
