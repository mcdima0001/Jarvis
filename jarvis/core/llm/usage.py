"""Расход моделей по дням и примерная цена.

Просьба владельца (14.09.2026): в панели «не токенов за сеанс, а за сегодня,
и хотя бы примерная цена». Сеансовый счётчик (`Spending`) обнуляет каждый
перезапуск, а их за вечер десяток — вопрос «сколько сегодня ушло» по нему не
решается вовсе.

Поэтому расход копится в файле на каждый день (`memory/usage/<дата>.json`):
по задаче и модели — запросы, вход, вход из кеша, выход.

**Цена считается нами и она примерная.** OpenAI стоимость в ответе не
присылает, поэтому она пересчитывается по тарифам из `llm.prices` в конфиге.
Кешированный вход считается отдельно: каталог инструментов со второй фразы
идёт по цене кеша, вдесятеро дешевле, и без этого цена выходила бы завышенной
в разы. OpenRouter цену присылает сам — тогда берётся его число.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Mapping

from jarvis.core.config.schema import ModelPrice

logger = logging.getLogger(__name__)

#: Сколько дней показывать в истории расхода.
HISTORY_DAYS = 7


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]  # разбираем что дали
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]  # разбираем что дали
    except (TypeError, ValueError):
        return 0.0


def cached_tokens(usage: Mapping[str, object]) -> int:
    """Сколько входа пришло из кеша — OpenAI пишет это во вложенном поле."""
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        return _int(details.get("cached_tokens"))
    return 0


def price_of(prompt: int, cached: int, completion: int, price: ModelPrice | None) -> float | None:
    """Цена в долларах по тарифу; нет тарифа — ``None``, а не ноль: «бесплатно» было бы враньём."""
    if price is None:
        return None
    fresh = max(0, prompt - cached)
    return (fresh * price.input + min(cached, prompt) * price.cached + completion * price.output) / 1_000_000


@dataclass(frozen=True, slots=True)
class UsageRow:
    """Расход одной задачи на одной модели за день."""

    task: str
    model: str
    calls: int
    prompt: int
    cached: int
    completion: int
    #: Примерная цена в долларах; ``None`` — у модели нет тарифа в конфиге.
    cost: float | None

    @property
    def tokens(self) -> int:
        """Всего токенов: вход плюс выход."""
        return self.prompt + self.completion


class UsageLog:
    """Расход по дням с записью на диск.

    :param directory: куда писать файлы дней; ``None`` — только в памяти (тесты, `--check`).
    :param prices: тарифы моделей из `llm.prices`.
    :param today: какой сегодня день — подменяется в тестах.
    """

    def __init__(
        self,
        directory: Path | None,
        *,
        prices: Mapping[str, ModelPrice] | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._dir = directory
        self._prices = dict(prices or {})
        self._today = today
        self._lock = threading.Lock()
        self._day: date | None = None
        self._rows: dict[str, dict[str, object]] = {}

    def add(self, task: str, model: str, usage: Mapping[str, object]) -> None:
        """Учесть один ответ модели. Пишет файл дня — звать из потока, а не из цикла событий."""
        day = self._today()
        with self._lock:
            if self._day != day:
                self._rows, self._day = self._load(day), day
            row = self._rows.setdefault(
                f"{task}|{model}",
                {"task": task, "model": model, "calls": 0, "prompt": 0, "cached": 0, "completion": 0, "reported": 0.0},
            )
            row["calls"] = _int(row.get("calls")) + 1
            row["prompt"] = _int(row.get("prompt")) + _int(usage.get("prompt_tokens"))
            row["cached"] = _int(row.get("cached")) + cached_tokens(usage)
            row["completion"] = _int(row.get("completion")) + _int(usage.get("completion_tokens"))
            # Цену присылает только OpenRouter; ей верим больше, чем своему пересчёту.
            row["reported"] = _float(row.get("reported")) + _float(usage.get("cost"))
            self._save(day, self._rows)

    def day(self, day: date | None = None) -> list[UsageRow]:
        """Расход за день, от самой затратной строки к скромной."""
        wanted = day or self._today()
        with self._lock:
            raw = dict(self._rows) if self._day == wanted else self._load(wanted)
        rows = [self._row(value) for value in raw.values() if isinstance(value, Mapping)]
        return sorted(rows, key=lambda row: (-(row.cost or 0.0), -row.tokens))

    def history(self, days: int = HISTORY_DAYS) -> list[tuple[date, list[UsageRow]]]:
        """Последние дни, начиная с сегодняшнего."""
        today = self._today()
        return [(today - timedelta(days=shift), self.day(today - timedelta(days=shift))) for shift in range(days)]

    def priced(self, model: str) -> bool:
        """Есть ли у модели тариф в конфиге."""
        return model in self._prices

    # --- внутреннее ----------------------------------------------------------

    def _row(self, raw: Mapping[str, object]) -> UsageRow:
        model = str(raw.get("model", ""))
        prompt, cached, completion = _int(raw.get("prompt")), _int(raw.get("cached")), _int(raw.get("completion"))
        reported = _float(raw.get("reported"))
        cost = reported if reported > 0 else price_of(prompt, cached, completion, self._prices.get(model))
        return UsageRow(
            task=str(raw.get("task", "")),
            model=model,
            calls=_int(raw.get("calls")),
            prompt=prompt,
            cached=cached,
            completion=completion,
            cost=cost,
        )

    def _path(self, day: date) -> Path | None:
        return self._dir / f"{day.isoformat()}.json" if self._dir is not None else None

    def _load(self, day: date) -> dict[str, dict[str, object]]:
        path = self._path(day)
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Расход за %s не прочитан (%s): начинаю день заново", day, exc)
            return {}
        return {str(key): dict(value) for key, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}

    def _save(self, day: date, rows: Mapping[str, Mapping[str, object]]) -> None:
        path = self._path(day)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            # Учёт расхода не повод ронять ответ ассистента.
            logger.warning("Расход не записан (%s): %s", path, exc)
