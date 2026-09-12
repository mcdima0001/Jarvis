"""Курс валют: разбор запроса, чтение ответа ЦБ и русская фраза.

Сеть сюда не ходит: ответ Центробанка подделан фикстурой ровно той формы, что он
отдаёт. Проверяется всё, что можно посчитать без сети, — выбор валюты, деление на
номинал и согласование рублей с копейками.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "rates" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rates = _load()

#: Ответ ЦБ той формы, что отдаёт cbr-xml-daily: цена за Nominal единиц.
_CBR = {
    "Date": "2026-09-12T11:30:00+03:00",
    "Valute": {
        "USD": {"CharCode": "USD", "Nominal": 1, "Value": 92.5, "Name": "Доллар США"},
        "EUR": {"CharCode": "EUR", "Nominal": 1, "Value": 100.0, "Name": "Евро"},
        "JPY": {"CharCode": "JPY", "Nominal": 100, "Value": 62.0, "Name": "Иен"},
    },
}


# --- какую валюту просят ----------------------------------------------------


def test_named_currency_is_picked() -> None:
    """«Курс доллара» — это доллар, падеж не мешает."""
    assert rates.pick_currencies("курс доллара") == ("USD",)
    assert rates.pick_currencies("курс евро") == ("EUR",)


def test_ruble_means_the_basket() -> None:
    """«Курс рубля» — вопрос без второй валюты: отвечаем главными."""
    assert rates.pick_currencies("курс рубля") == ("USD", "EUR")
    assert rates.pick_currencies("") == ("USD", "EUR")


def test_synonyms_do_not_double_a_currency() -> None:
    """«dollar» и «доллар» — один код, в ответе он не должен задваиваться."""
    assert rates.pick_currencies("dollar курс доллара") == ("USD",)


# --- чтение ответа ----------------------------------------------------------


def test_rate_divides_by_nominal() -> None:
    """Цена дана за Nominal единиц: иена за 100, значит делим.

    Без деления редкая валюта завышена на порядок: 62 рубля за иену вместо 0.62.
    """
    assert rates.rate_of(_CBR, "USD") == 92.5
    assert round(rates.rate_of(_CBR, "JPY"), 3) == 0.62


# --- русская фраза ----------------------------------------------------------


def test_phrase_has_rubles_and_kopecks() -> None:
    """«92 рубля 50 копеек» — с правильными формами слов."""
    assert rates.describe_ru(_CBR, ("USD",)) == "Доллар 92 рубля 50 копеек."


def test_whole_number_drops_kopecks() -> None:
    """Ровно сто рублей — без копеек и с верной формой «рублей»."""
    assert rates.describe_ru(_CBR, ("EUR",)) == "Евро 100 рублей."


def test_basket_lists_both() -> None:
    """Корзина для «курса рубля» перечисляет обе валюты через запятую."""
    phrase = rates.describe_ru(_CBR, ("USD", "EUR"))
    assert phrase == "Доллар 92 рубля 50 копеек, Евро 100 рублей."


# --- за курсом не ходят дважды ----------------------------------------------


def _skill(handler: Any) -> Any:
    """Скилл с поддельным HTTP: on_setup мимо, поля выставляем прямо."""
    skill = rates.RatesSkill.__new__(rates.RatesSkill)
    skill._timeout = 1.0
    skill._cached = None
    skill._cached_at = 0.0
    skill._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return skill


async def test_daily_rates_are_asked_once() -> None:
    """ЦБ публикует курсы раз в сутки — ходить за ними на каждый вопрос незачем.

    Замер 12.09.2026: запрос стоит 0.27 с на разогретом соединении и 1.2 с на
    холодном, то есть заметную долю того, что человек ждёт после «курс доллара».
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_CBR)

    skill = _skill(handler)
    try:
        first = await skill._daily()
        second = await skill._daily()
    finally:
        await skill._client.aclose()

    assert calls == 1, "за одними и теми же дневными курсами сходили дважды"
    assert first is second


async def test_stale_rates_are_asked_again() -> None:
    """Срок годности есть: иначе новые курсы не подхватились бы до перезапуска."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_CBR)

    skill = _skill(handler)
    try:
        await skill._daily()
        skill._cached_at -= rates.CACHE_TTL_S + 1
        await skill._daily()
    finally:
        await skill._client.aclose()

    assert calls == 2
