"""Запасной провайдер: кончились деньги у одного — спрашиваем другого.

Просьба владельца 24.09.2026. Повод прямой: счёт OpenAI пуст с 18.09, и всё это
время свободный разговор, разбор незнакомых формулировок и первая глубина «где
снято» просто не работали, хотя ключ OpenRouter лежал в `.env` рядом.
"""

from __future__ import annotations

import pytest

from jarvis.core.config import TaskProfile
from jarvis.core.errors import LLMError, LLMNotConfigured, LLMOutOfCredits
from jarvis.core.llm import LLMService, ProfileRegistry
from jarvis.core.llm.protocol import LLMRequest, LLMResponse, Message


class Provider:
    """Провайдер, который отвечает или падает — как велено."""

    def __init__(self, name: str, *, fails: Exception | None = None) -> None:
        self.name = name
        self.title = name
        self._fails = fails
        self.asked: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.asked.append(request.model)
        if self._fails is not None:
            raise self._fails
        return LLMResponse(text=f"ответ от {self.name}", usage={})


def _service(main: Provider, spare: Provider | None = None, **extra) -> LLMService:
    profile = TaskProfile(
        task="dialog",
        provider="openai",
        model="gpt-5.4-nano",
        fallback_provider="openrouter" if spare is not None else "",
    )
    providers = {"openai": main}
    if spare is not None:
        providers["openrouter"] = spare
    return LLMService(
        providers=providers,  # type: ignore[arg-type]
        profiles=ProfileRegistry({"dialog": profile}, default_task="dialog"),
        **extra,
    )


async def test_an_empty_account_goes_to_the_spare() -> None:
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    spare = Provider("openrouter")
    service = _service(main, spare)
    answer = await service.complete([Message.user("привет")], task="dialog")
    assert answer.text == "ответ от openrouter"
    assert spare.asked == ["openai/gpt-5.4-nano"], "у OpenRouter модели OpenAI с приставкой"


async def test_the_dead_provider_is_left_alone_for_a_while() -> None:
    """Иначе каждая фраза начинается с похода к пустому счёту."""
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    spare = Provider("openrouter")
    service = _service(main, spare, fallback_retry_min=30)
    await service.complete([Message.user("раз")], task="dialog")
    await service.complete([Message.user("два")], task="dialog")
    assert len(main.asked) == 1, "второй раз основного не спрашивали"
    assert len(spare.asked) == 2


async def test_a_network_failure_does_not_go_to_the_spare() -> None:
    """У запасного та же сеть: вторая попытка только удвоит ожидание."""
    main = Provider("openai", fails=LLMError("connection timed out"))
    spare = Provider("openrouter")
    service = _service(main, spare)
    with pytest.raises(LLMError):
        await service.complete([Message.user("привет")], task="dialog")
    assert spare.asked == []


async def test_without_a_spare_everything_works_as_before() -> None:
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    service = _service(main)
    with pytest.raises(LLMOutOfCredits):
        await service.complete([Message.user("привет")], task="dialog")


async def test_the_fault_is_forgotten_when_the_spare_answers() -> None:
    """Ответ получен — жаловаться вслух не на что."""
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    service = _service(main, Provider("openrouter"))
    await service.complete([Message.user("привет")], task="dialog")
    assert service.faults.recent() is None


async def test_both_dead_still_explains_itself() -> None:
    """Молча пропавший ответ хуже, чем объяснённый: журнал сбоев должен знать."""
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    spare = Provider("openrouter", fails=LLMNotConfigured("нет ключа"))
    service = _service(main, spare)
    with pytest.raises(LLMError):
        await service.complete([Message.user("привет")], task="dialog")
    fault = service.faults.recent()
    assert fault is not None and fault.tellable


async def test_a_dead_provider_is_not_knocked_on_twice_per_phrase() -> None:
    """Живой лог 24.09.2026: при пустом счёте каждая фраза ходила к OpenAI
    дважды — задачами `intent` и `intent_strong` — и теряла на этом секунду.

    Запасного у разбора команд нет намеренно, но и ходить к мёртвому незачем:
    он ответит тем же отказом, только через ожидание.
    """
    main = Provider("openai", fails=LLMOutOfCredits("кончились деньги", provider="OpenAI"))
    service = _service(main)
    for _ in range(3):
        with pytest.raises(LLMOutOfCredits):
            await service.complete([Message.user("привет")], task="dialog")
    assert len(main.asked) == 1, "спросили один раз, дальше отказываем сразу"


async def test_without_a_spare_the_wait_is_short() -> None:
    """Владелец пополняет счёт прямо сейчас — держать отказ полчаса нельзя."""
    from jarvis.core.llm.service import DEAD_RETRY_S

    assert DEAD_RETRY_S <= 60, "полминуты хватает, чтобы не частить"
