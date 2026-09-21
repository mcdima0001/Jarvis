"""Сбой, о котором нужно сказать человеческими словами.

Ассистент, который на пустой счёт провайдера отвечает «не справился, сэр»,
заставляет искать поломку там, где её нет: вечером 21.09.2026 владелец два дня
слышал ровно эту реплику, а в логе всё это время стояло «You have no credits
remaining» (`insufficient_quota`). Причина не в механизме, и починить её кодом
нельзя — можно только назвать вслух.

Разделение то же, что у речи без вопроса: **кто упал — знает сбой, как о нём
сказать — знает это место**. Иначе каждый путь к модели (разбор намерения,
свободный разговор, план, скилл) заводит своё объяснение, и они разъезжаются.

Журнал помнит **последний** сбой и его время. Больше не нужно: реплика о
неудаче произносится сразу после самого сбоя, а старый сбой хуже, чем
никакого, — за час счёт могли и пополнить.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from jarvis.core.errors import LLMNotConfigured, LLMOutOfCredits

#: Сколько секунд сбой считается тем самым, из-за которого команда не вышла.
FRESH_S = 60.0

#: Виды сбоев, о которых есть что сказать.
NO_MONEY = "no_money"
NO_KEY = "no_key"
TOO_OFTEN = "too_often"
NO_NETWORK = "no_network"
UNKNOWN = "unknown"

#: Слова в тексте чужой ошибки, по которым видно, что случилось. Разбирать
#: коды каждого провайдера здесь нельзя — это их дело; здесь только последняя
#: попытка понять то, что провайдер не пометил своим видом ошибки.
_MARKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (NO_MONEY, ("insufficient_quota", "credit_balance", "no credits", "billing", "payment required")),
    (TOO_OFTEN, ("rate limit", "rate_limit", "too many requests", " 429")),
    (NO_NETWORK, ("connect", "timeout", "timed out", "temporary failure", "name resolution", "unreachable", "сеть недоступна")),
)

_SPEECH: dict[str, dict[str, str]] = {
    NO_MONEY: {
        "ru": "На счету {provider} кончились деньги.",
        "en": "The {provider} account is out of credit.",
    },
    NO_KEY: {
        "ru": "Языковая модель не подключена: нет ключа.",
        "en": "The language model isn't connected: no key.",
    },
    TOO_OFTEN: {
        "ru": "{provider} не принимает запросы — слишком часто.",
        "en": "{provider} is refusing requests: too many at once.",
    },
    NO_NETWORK: {
        "ru": "Не дотянулся до сети.",
        "en": "I couldn't reach the network.",
    },
}
#: Чей счёт, если провайдер не назвался.
_NOBODY = {"ru": "модели", "en": "the model"}


@dataclass(frozen=True, slots=True)
class Fault:
    """Что сломалось, когда и у кого."""

    kind: str
    provider: str
    detail: str
    at: float

    @property
    def tellable(self) -> bool:
        """Есть ли что сказать вслух: о непонятном сбое лучше молчать."""
        return self.kind in _SPEECH

    def speech(self, language: str | None = "ru") -> str:
        """Одна фраза человеку; пусто — сказать нечего."""
        code = "en" if (language or "ru").startswith("en") else "ru"
        line = _SPEECH.get(self.kind, {}).get(code, "")
        return line.format(provider=self.provider or _NOBODY[code])


def classify(exc: BaseException) -> str:
    """Что за сбой. Вид ошибки важнее текста: текст у каждого провайдера свой."""
    if isinstance(exc, LLMOutOfCredits):
        return NO_MONEY
    if isinstance(exc, LLMNotConfigured):
        return NO_KEY
    low = f"{exc}".lower()
    for kind, marks in _MARKS:
        if any(mark in low for mark in marks):
            return kind
    if isinstance(exc, (TimeoutError, OSError)):
        return NO_NETWORK
    return UNKNOWN


class Faults:
    """Последний сбой обращения к модели — чтобы назвать причину вслух."""

    def __init__(self) -> None:
        self._last: Fault | None = None

    def note(self, exc: BaseException, *, provider: str = "") -> Fault:
        """Запомнить сбой. Возвращает разобранное — для лога."""
        fault = Fault(
            kind=classify(exc),
            provider=provider or str(getattr(exc, "provider", "") or ""),
            detail=f"{type(exc).__name__}: {exc}"[:400],
            at=time.monotonic(),
        )
        self._last = fault
        return fault

    def recent(self, within_s: float = FRESH_S) -> Fault | None:
        """Сбой, случившийся только что и объяснимый словами; иначе ``None``."""
        fault = self._last
        if fault is None or not fault.tellable:
            return None
        return fault if time.monotonic() - fault.at <= within_s else None

    def forget(self) -> None:
        """Забыть сбой — например, когда следующий запрос прошёл."""
        self._last = None
