"""Расход моделей по дням и примерная цена для панели.

Просьба владельца (14.09.2026): «не токенов за сеанс, а за сегодня, и хотя бы
примерная цена». Главное здесь — кеш: каталог инструментов идёт по цене кеша, и
без его учёта цена завышалась бы в разы.
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from jarvis.core.config import load_config
from jarvis.core.config.schema import ModelPrice
from jarvis.core.errors import ConfigError
from jarvis.core.llm.usage import UsageLog, price_of

NANO = ModelPrice(input=0.20, cached=0.02, output=1.25)


def test_cached_input_is_billed_at_the_cached_price() -> None:
    # Живая фраза разбора: 4026 входа, из них 3840 из кеша, 27 выхода.
    cost = price_of(4026, 3840, 27, NANO)
    assert cost == pytest.approx((186 * 0.20 + 3840 * 0.02 + 27 * 1.25) / 1_000_000)
    # По полной цене вышло бы почти впятеро дороже.
    assert price_of(4026, 0, 27, NANO) > cost * 4  # type: ignore[operator]


def test_model_without_price_has_no_cost_not_zero() -> None:
    assert price_of(100, 0, 10, None) is None


def test_usage_is_kept_per_day_and_survives_restart(tmp_path: Path) -> None:
    day = {"value": date(2026, 9, 14)}
    usage = {"prompt_tokens": 4000, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 3800}}
    log = UsageLog(tmp_path, prices={"gpt-5.4-nano": NANO}, today=lambda: day["value"])
    log.add("intent", "gpt-5.4-nano", usage)
    log.add("intent", "gpt-5.4-nano", usage)
    log.add("place", "gpt-5.5", {"prompt_tokens": 1000, "completion_tokens": 100})

    # Перезапуск: новый журнал читает тот же файл дня.
    again = UsageLog(tmp_path, prices={"gpt-5.4-nano": NANO}, today=lambda: day["value"])
    rows = {row.task: row for row in again.day()}
    assert rows["intent"].calls == 2 and rows["intent"].cached == 7600 and rows["intent"].tokens == 8060
    assert rows["intent"].cost == pytest.approx(price_of(8000, 7600, 60, NANO))
    assert rows["place"].cost is None, "тарифа gpt-5.5 в этом журнале нет"

    # Новый день начинается с нуля, а вчерашний остаётся в истории.
    day["value"] = date(2026, 9, 15)
    assert again.day() == []
    history = dict(again.history(2))
    assert history[date(2026, 9, 14)] and history[date(2026, 9, 15)] == []


def test_openrouter_reported_cost_wins(tmp_path: Path) -> None:
    log = UsageLog(None, prices={"m": NANO})
    log.add("dialog", "m", {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0042})
    assert log.day()[0].cost == pytest.approx(0.0042)


def test_prices_are_read_from_config(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_text(textwrap.dedent("""
        llm:
          prices:
            gpt-5.4-nano: {input: 0.20, cached: 0.02, output: 1.25}
            cheap: {input: 1, output: 2}
    """), encoding="utf-8")
    prices = load_config(source).llm.prices
    assert prices["gpt-5.4-nano"] == NANO
    assert prices["cheap"].cached == 1.0, "без cached кеш считается по цене входа"


def test_broken_price_is_a_config_error(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_text("llm:\n  prices:\n    x: {input: дорого, output: 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(source)
