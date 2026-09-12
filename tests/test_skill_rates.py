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
