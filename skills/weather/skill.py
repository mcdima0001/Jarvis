"""Погода через Open-Meteo.

Отдельный скилл, а не вопрос к поиску: погода — это число, а не текст. Из
поисковой выдачи её пришлось бы вытаскивать пересказом, каждый раз тратя
запрос к модели и каждый раз рискуя получить прошлогодний прогноз со случайного
сайта. Здесь ответ приходит цифрами прямо из метеослужбы.

Open-Meteo не требует ключа и не просит регистрации: для некоммерческого
использования достаточно вежливой частоты запросов. Города ищутся через её же
геокодер, поэтому «погода в Праге» работает без справочника городов в коде.
"""

from __future__ import annotations

import itertools
import re
from datetime import date, timedelta

import httpx

from jarvis.core.contracts import ToolResult, detect_language
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

_GEOCODER = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"

#: Коды погоды WMO. Ключ — код, значение — описание по-русски и по-английски.
_CONDITIONS: dict[int, tuple[str, str]] = {
    0: ("ясно", "clear"),
    1: ("почти ясно", "mostly clear"),
    2: ("переменная облачность", "partly cloudy"),
    3: ("пасмурно", "overcast"),
    45: ("туман", "fog"),
    48: ("изморозь", "freezing fog"),
    51: ("морось", "light drizzle"),
    53: ("морось", "drizzle"),
    55: ("сильная морось", "heavy drizzle"),
    56: ("ледяная морось", "freezing drizzle"),
    57: ("ледяная морось", "freezing drizzle"),
    61: ("небольшой дождь", "light rain"),
    63: ("дождь", "rain"),
    65: ("сильный дождь", "heavy rain"),
    66: ("ледяной дождь", "freezing rain"),
    67: ("ледяной дождь", "freezing rain"),
    71: ("небольшой снег", "light snow"),
    73: ("снег", "snow"),
    75: ("сильный снег", "heavy snow"),
    77: ("снежная крупа", "snow grains"),
    80: ("ливень", "rain showers"),
    81: ("ливень", "rain showers"),
    82: ("сильный ливень", "violent rain showers"),
    85: ("снегопад", "snow showers"),
    86: ("сильный снегопад", "heavy snow showers"),
    95: ("гроза", "thunderstorm"),
    96: ("гроза с градом", "thunderstorm with hail"),
    99: ("сильная гроза с градом", "severe thunderstorm with hail"),
}

#: Как назвать день вслух: сегодня, завтра, послезавтра.
_DAY_NAMES: dict[int, tuple[str, str]] = {
    0: ("сегодня", "today"),
    1: ("завтра", "tomorrow"),
    2: ("послезавтра", "the day after tomorrow"),
}

#: Слова, которыми в команде обычно называют день.
_DAY_WORDS: dict[str, int] = {
    "сегодня": 0, "сейчас": 0, "today": 0, "now": 0,
    "завтра": 1, "tomorrow": 1,
    "послезавтра": 2,
}


#: Окончания предложного падежа и чем их заменить, чтобы получить именительный.
#: «Погода в Праге» приходит именно так, а геокодер знает только «Прага».
#: Порядок важен: первым идёт самый частый вариант.
_CASE_ENDINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ах", ("ы",)),                  # Афинах→Афины
    ("ях", ("и",)),                  # Сокольниках и прочие множественные
    ("ем", ("ий", "ый")),            # Нижнем→Нижний: прилагательное в названии
    ("ом", ("ый", "")),              # Хрустальном→Хрустальный
    ("е", ("а", "", "я", "ь")),      # Москве→Москва, Лондоне→Лондон
    ("и", ("ь", "я", "а", "")),      # Твери→Тверь, Италии→Италия
    ("у", ("", "а")),                # Крыму→Крым
    ("ю", ("й", "я")),               # Дубаю→Дубай
)

#: Разговорные названия, которых нет в справочнике геокодера.
_ALIASES: dict[str, str] = {
    "питер": "Санкт-Петербург",
    "питере": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "мск": "Москва",
    "нижний": "Нижний Новгород",
    "ебург": "Екатеринбург",
    "екат": "Екатеринбург",
}

#: Сколько написаний перебирать: каждое — отдельный запрос к геокодеру.
#: Хватает на составные названия вроде «в Нижнем Новгороде», где окончание
#: снимается сразу с двух слов. Перебор идёт только при промахе и кешируется.
_MAX_CANDIDATES = 8


def _word_forms(word: str) -> list[str]:
    """Варианты одного слова: как есть плюс попытки снять падежное окончание."""
    forms = [word]
    lowered = word.lower()
    for ending, replacements in _CASE_ENDINGS:
        if not lowered.endswith(ending) or len(word) <= len(ending) + 1:
            continue
        stem = word[: -len(ending)]
        forms.extend(stem + replacement for replacement in replacements)
        break
    return forms


def _nominative_candidates(city: str) -> list[str]:
    """Перебрать написания города от косвенного падежа к именительному.

    Склонение здесь не разбирается по-настоящему: задача не в грамматике, а в
    том, чтобы попасть в справочник геокодера, который знает только
    именительный падеж. Кандидаты пробуются по очереди, побеждает первый
    совпавший буквально.

    Многословные названия («в Нижнем Новгороде») склоняются целиком, поэтому
    окончание снимается у каждого слова.
    """
    city = city.strip()
    if alias := _ALIASES.get(city.casefold()):
        return [alias]

    # Составные названия склоняются по частям, причём не все: «Ростов-на-Дону»
    # в предложном падеже становится «Ростове-на-Дону» — меняется только первая
    # часть. Поэтому разделители сохраняем, а варианты перебираем по каждой
    # части независимо.
    parts = re.split(r"([ \-])", city)
    forms = [_word_forms(part) if index % 2 == 0 else [part]
             for index, part in enumerate(parts)]

    # Сначала варианты, где изменено меньше частей: так «Нижний Новгород»
    # находится раньше, чем «Нижный Новгородя».
    combinations = sorted(
        itertools.product(*(range(len(f)) for f in forms)), key=lambda idx: sum(idx)
    )
    candidates = [
        "".join(forms[position][choice] for position, choice in enumerate(combination))
        for combination in combinations
    ]

    unique = list(dict.fromkeys(candidate for candidate in candidates if candidate))
    return unique[:_MAX_CANDIDATES]


def _describe(code: int, language: str) -> str:
    """Перевести код WMO в человеческое описание."""
    pair = _CONDITIONS.get(int(code))
    if pair is None:
        return "без осадков" if language == "ru" else "no precipitation"
    return pair[0] if language == "ru" else pair[1]


class WeatherSkill(Skill):
    """Прогноз погоды и текущие условия."""

    meta = SkillMeta(
        name="weather",
        description="Погода и прогноз через Open-Meteo",
        version="0.1.0",
    )

    async def on_setup(self) -> None:
        """Прочитать город по умолчанию."""
        self._default_city = str(self.context.setting("city", "Москва"))
        self._timeout = float(self.context.setting("timeout", 15.0))
        self._client: httpx.AsyncClient | None = None
        # Координаты города меняются редко — второй раз спрашивать незачем.
        self._places: dict[str, dict[str, object]] = {}
        self.log.info("Погода: город по умолчанию %s", self._default_city)

    async def on_stop(self) -> None:
        """Закрыть соединения."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        """Один клиент на весь скилл."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def _locate(self, city: str, language: str) -> dict[str, object] | None:
        """Найти координаты города, разобравшись с падежом.

        Из команды город приходит так, как его произнесли: «погода в Праге».
        Геокодер знает только именительный, поэтому написания перебираются.
        """
        key = city.strip().lower()
        if key in self._places:
            return self._places[key]

        # Геокодер отвечает похожим, а не точным: на «Твери» он выдаёт «Тверия»
        # в Израиле. Поэтому сначала ищем кандидата, чьё имя совпало буквально,
        # и только если такого нет — соглашаемся на первое похожее.
        similar: dict[str, object] | None = None
        for candidate in _nominative_candidates(city.strip()):
            response = await self._http().get(
                _GEOCODER, params={"name": candidate, "count": 1, "language": language}
            )
            response.raise_for_status()
            results = response.json().get("results") or []
            if not results:
                continue
            place = results[0]
            if str(place.get("name", "")).casefold() == candidate.casefold():
                if candidate != city.strip():
                    self.log.debug("Город %r найден как %r", city, candidate)
                self._places[key] = place
                return place
            similar = similar or place

        if similar is not None:
            self.log.debug("Город %r точно не найден, беру похожий: %s", city, similar.get("name"))
            self._places[key] = similar
        return similar

    @tool(phrases=["какая погода", "погода", "погода в {city}", "погода на завтра",
                   "what is the weather", "weather", "weather in {city}"])
    async def forecast(self, city: str = "", day: str = "сегодня") -> ToolResult:
        """Узнать погоду на сегодня, завтра или послезавтра.

        :param city: город; пустой — город из настроек скилла.
        :param day: сегодня, завтра или послезавтра.
        """
        city = (city or self._default_city).strip()
        language = detect_language(city, default="ru")
        offset = _DAY_WORDS.get(day.strip().lower(), 0)

        place = await self._locate(city, language)
        if place is None:
            return ToolResult.failure(
                f"город {city} не найден",
                speech={
                    "ru": f"Не нашёл город {city}.",
                    "en": f"Could not find {city}.",
                },
            )

        response = await self._http().get(
            _FORECAST,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,weather_code,wind_speed_10m_max",
                "timezone": "auto",
                "forecast_days": offset + 1,
            },
        )
        response.raise_for_status()
        daily = response.json()["daily"]

        low = round(daily["temperature_2m_min"][offset])
        high = round(daily["temperature_2m_max"][offset])
        chance = daily["precipitation_probability_max"][offset] or 0
        condition_ru = _describe(daily["weather_code"][offset], "ru")
        condition_en = _describe(daily["weather_code"][offset], "en")
        when_ru, when_en = _DAY_NAMES.get(offset, ("сегодня", "today"))
        name = place.get("name", city)

        value = {
            "city": name,
            "date": str(date.today() + timedelta(days=offset)),
            "low": low,
            "high": high,
            "precipitation_probability": chance,
            "condition": condition_en,
        }
        rain_ru = f" Осадки — {chance} процентов." if chance >= 30 else ""
        rain_en = f" Precipitation chance {chance} percent." if chance >= 30 else ""
        return ToolResult.success(
            value,
            speech={
                "ru": f"{when_ru.capitalize()} в городе {name} от {low} до {high} "
                      f"градусов, {condition_ru}.{rain_ru}",
                "en": f"{when_en.capitalize()} in {name}: {low} to {high} degrees, "
                      f"{condition_en}.{rain_en}",
            },
        )

    @tool(phrases=["сколько градусов на улице", "температура на улице",
                   "how cold is it outside", "temperature outside"])
    async def now(self, city: str = "") -> ToolResult:
        """Узнать погоду прямо сейчас.

        :param city: город; пустой — город из настроек скилла.
        """
        city = (city or self._default_city).strip()
        language = detect_language(city, default="ru")

        place = await self._locate(city, language)
        if place is None:
            return ToolResult.failure(
                f"город {city} не найден",
                speech={"ru": f"Не нашёл город {city}.", "en": f"Could not find {city}."},
            )

        response = await self._http().get(
            _FORECAST,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "timezone": "auto",
            },
        )
        response.raise_for_status()
        current = response.json()["current"]

        temperature = round(current["temperature_2m"])
        humidity = round(current["relative_humidity_2m"])
        name = place.get("name", city)
        return ToolResult.success(
            {
                "city": name,
                "temperature": temperature,
                "humidity": humidity,
                "condition": _describe(current["weather_code"], "en"),
            },
            speech={
                "ru": f"Сейчас в городе {name} {temperature} градусов, "
                      f"{_describe(current['weather_code'], 'ru')}, "
                      f"влажность {humidity} процентов.",
                "en": f"Right now in {name}: {temperature} degrees, "
                      f"{_describe(current['weather_code'], 'en')}, "
                      f"humidity {humidity} percent.",
            },
        )

    async def health(self) -> HealthStatus:
        """Ключей не требует, поэтому исправен всегда, пока есть сеть."""
        return HealthStatus.healthy(f"город по умолчанию {self._default_city}")
