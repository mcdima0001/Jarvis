"""Курс валют: «курс доллара», «курс рубля» — ответ живым числом.

Появился ради флагманского примера клавиатурного наблюдателя: печатаешь «курс
рубля», и ассистент отвечает, не дожидаясь Enter. Но это обычный голосовой скилл,
и работает он и голосом тоже.

Источник — дневной JSON Центробанка (`cbr-xml-daily.ru`): без ключа, отдаёт
курсы в рублях. Новых зависимостей не тянет — только `httpx`, который и так
основной. «Курс рубля» — вопрос без второй валюты, и разумнее всего понимать его
как «рубль против главных»: отвечаем и долларом, и евро.
"""

from __future__ import annotations

import httpx

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

#: Дневные курсы ЦБ в рублях. Без ключа, отдаёт JSON.
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

#: Как валюту называют вслух → код в ответе ЦБ. Для сравнения текст в нижнем
#: регистре; проверяется вхождением, чтобы падеж не мешал («доллара», «евро»).
_SPOKEN = {
    "доллар": "USD",
    "dollar": "USD",
    "евро": "EUR",
    "euro": "EUR",
    "юан": "CNY",
    "yuan": "CNY",
    "фунт": "GBP",
    "pound": "GBP",
    "franc": "CHF",
    "франк": "CHF",
    "иен": "JPY",
    "yen": "JPY",
}

#: На «курс рубля» второй валюты нет — отвечаем рублём против главных.
_RUBLE_BASKET = ("USD", "EUR")

#: Ошибки сети и разбора, при которых честно отвечаем «не смог узнать».
_RATE_ERRORS = (httpx.HTTPError, KeyError, ValueError, TypeError)


def pick_currencies(text: str) -> tuple[str, ...]:
    """Какие валюты просят. Пусто/«рубль» — корзина из главных.

    :param text: реплика, как она пришла («курс доллара»).
    :return: коды валют ЦБ; для «курса рубля» — доллар и евро.
    """
    low = text.lower()
    found = [code for spoken, code in _SPOKEN.items() if spoken in low]
    if found:
        # Не теряем порядок и убираем повторы: «доллар» и «dollar» — один код.
        seen: list[str] = []
        for code in found:
            if code not in seen:
                seen.append(code)
        return tuple(seen)
    return _RUBLE_BASKET


def rate_of(data: dict, code: str) -> float:
    """Курс одной валюты в рублях за одну единицу.

    ЦБ даёт цену за `Nominal` единиц (за 10 крон, за 100 иен), поэтому делим:
    иначе редкие валюты завышены на порядок.
    """
    valute = data["Valute"][code]
    nominal = float(valute.get("Nominal", 1)) or 1.0
    return float(valute["Value"]) / nominal


def _money_ru(value: float) -> str:
    """«92 рубля 50 копеек» — с правильными формами и без копеек, когда их нет."""
    rubles = int(value)
    kopecks = round((value - rubles) * 100)
    if kopecks == 100:  # округление вверх переносит в рубль
        rubles += 1
        kopecks = 0
    text = f"{rubles} {plural_form(rubles, ('рубль', 'рубля', 'рублей'))}"
    if kopecks:
        text += f" {kopecks} {plural_form(kopecks, ('копейка', 'копейки', 'копеек'))}"
    return text


def describe_ru(data: dict, codes: tuple[str, ...]) -> str:
    """Собрать русскую фразу о курсе для перечисленных валют."""
    names = {"USD": "Доллар", "EUR": "Евро", "CNY": "Юань", "GBP": "Фунт",
             "CHF": "Франк", "JPY": "Иена"}
    parts = [
        f"{names.get(code, code)} {_money_ru(rate_of(data, code))}"
        for code in codes
    ]
    return ", ".join(parts) + "."


class RatesSkill(Skill):
    """Сообщает курс валют по данным Центробанка."""

    meta = SkillMeta(
        name="rates",
        description="Курс валют по данным ЦБ: «курс доллара», «курс рубля».",
        version="0.1.0",
        spoken=("курс", "валюта", "rates"),
    )

    async def on_setup(self) -> None:
        """Прочитать таймаут и приготовить клиент."""
        self._timeout = float(self.context.setting("timeout", 15.0))
        self._client: httpx.AsyncClient | None = None

    async def on_stop(self) -> None:
        """Закрыть соединения."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Один клиент на весь скилл."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True
            )
        return self._client

    @tool(
        phrases=[
            # Слот {what} несёт саму валюту: «курс доллара» → what=«доллара».
            # Пустой хвост («курс рубля», «курс валют») даёт корзину главных.
            "курс {what}",
            "какой курс {what}",
            "сколько стоит {what}",
            "курс валют",
            "exchange rate",
            "{what} rate",
        ],
        reversible=True,
    )
    async def currency(self, what: str = "") -> ToolResult:
        """Сообщить курс валюты в рублях по данным Центробанка.

        :param what: какая валюта («доллар», «евро»); пусто — рубль против
            главных, то есть доллар и евро сразу.
        """
        codes = pick_currencies(what)
        try:
            response = await self._http().get(CBR_URL)
            response.raise_for_status()
            data = response.json()
            # Сразу дёргаем курсы: недостающий код всплывёт тут, а не в речи.
            spoken = describe_ru(data, codes)
        except _RATE_ERRORS as error:
            self.log.warning("Курс не получен: %s", error)
            return ToolResult.failure(
                f"курс валют недоступен: {error}",
                speech={
                    "ru": "Не смог узнать курс, сэр.",
                    "en": "I couldn't get the exchange rate.",
                },
            )
        rates = {code: round(rate_of(data, code), 4) for code in codes}
        self.log.info("Курс: %s", rates)
        return ToolResult.success(
            {"rates": rates, "date": data.get("Date", "")},
            speech={"ru": spoken, "en": spoken},
        )

    async def health(self) -> HealthStatus:
        """Здоров, пока источник курса на связи."""
        try:
            response = await self._http().get(CBR_URL)
            response.raise_for_status()
        except _RATE_ERRORS as error:
            return HealthStatus.degraded(f"курс недоступен: {error}")
        return HealthStatus.healthy()
