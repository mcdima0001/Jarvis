"""Эквалайзер Peace Nexus: пресеты, басы и верха, баланс — голосом.

Peace Nexus у владельца — своя сборка Peace с API (`nexus-api`, README там же).
Скилл зовёт его модуль `peace_api.py` напрямую, а не MCP-сервер и не
`nexus.py`: это ctypes и сообщения окну, быстрые и без процесса на команду.
Модуль лежит вне проекта, путь к нему — `api_dir` в настройках скилла.

**Команды строго по одной.** Peace на параллельный запрос отвечает «ещё
выполняет предыдущую команду», поэтому все обращения идут под одним замком, а
сами вызовы — в отдельном потоке: `SendMessageTimeout` ждёт ответа окна до пяти
секунд, и голосовой круг это время висел бы.

**Что обратимо, а что нет.** Полосы, предусиление, баланс и вкл/выкл
возвращаются той же командой обратно. Загрузка пресета, «выровнять» и
сохранение — нет: несохранённая кривая пропадает, а сохранение перезаписывает
пресет с тем же именем. В плане такие шаги спросят.

**В каталог модели идут четыре инструмента из десятка** — состояние, вкл/выкл,
пресет и сдвиг басов или верхов. Остальное голосом говорят одинаково, и шаблоны
разбирают это бесплатно; платить за каждый инструмент токенами в каждом
запросе незачем.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

#: Путь к `nexus-api`, если в настройках не задан.
DEFAULT_API_DIR = r"C:\Users\mcdim\PeaceBuild\nexus-api"

DECIBEL = ("децибел", "децибела", "децибел")

#: Порог узнавания пресета на слух. Список короткий, спутать в нём мало что,
#: но «поставь пресет вечер» не должно загрузить «Вечеринку».
PRESET_SIMILARITY = 0.75


class PeaceUnavailable(RuntimeError):
    """Модуль peace_api не нашёлся или не загрузился."""


# --- чистые функции ---------------------------------------------------------


def decibels(value: float) -> str:
    """Число с «децибел» для речи: «минус 3 децибела», «2.5 децибела»."""
    rounded = round(value, 1)
    sign = "минус " if rounded < 0 else ""
    magnitude = abs(rounded)
    if magnitude == int(magnitude):
        whole = int(magnitude)
        return f"{sign}{whole} {plural_form(whole, DECIBEL)}"
    # Дробь читается с родительным падежом: «два запятая пять децибела».
    return f"{sign}{magnitude} {DECIBEL[1]}"


def pick_preset(spoken: str, names: Sequence[str]) -> str | None:
    """Найти пресет по услышанному названию: «ультра бас» → «ULTRA BASS»."""
    if not spoken.strip():
        return None
    return best_match(spoken, names, similarity=PRESET_SIMILARITY)


def shifted_gains(
    bands: Sequence[dict[str, Any]],
    choose: Callable[[float], bool],
    step: float,
    limit: float,
) -> list[tuple[int, float]]:
    """Какие полосы и до какого усиления сдвинуть.

    :param choose: подходит ли полоса по частоте.
    :return: пары «номер полосы, новое усиление»; полосы, упёршиеся в предел,
        не включаются — менять им нечего.
    """
    changes: list[tuple[int, float]] = []
    for band in bands:
        if not choose(float(band.get("frequency_hz", 0))):
            continue
        current = float(band.get("gain_db", 0.0))
        target = round(max(-limit, min(limit, current + step)), 1)
        if target != round(current, 1):
            changes.append((int(band["band"]), target))
    return changes


def describe_status(status: dict[str, Any]) -> str:
    """Состояние эквалайзера одной фразой — так, как его произносят."""
    if not status.get("equalizer_on"):
        return "Эквалайзер выключен."
    parts = ["Эквалайзер включён"]
    preset = str(status.get("preset") or "")
    if preset:
        # Звёздочка у Peace — несохранённые правки («BassBoost*»). Вслух её не
        # прочесть, а сказать о правках стоит: перед «поставь пресет» это важно.
        edited = preset.endswith("*")
        parts.append(f"пресет {preset.rstrip('*')}" + (", с изменениями" if edited else ""))
    preamp = float(status.get("preamp_db") or 0.0)
    if preamp:
        parts.append(f"предусиление {decibels(preamp)}")
    pan = int(status.get("pan") or 0)
    if pan:
        parts.append(f"баланс {'влево' if pan < 0 else 'вправо'} на {abs(pan)}")
    return ", ".join(parts) + "."


# --- скилл ------------------------------------------------------------------


class PeaceSkill(Skill):
    """Управление эквалайзером Peace Nexus."""

    meta = SkillMeta(
        name="peace",
        description="Эквалайзер Peace Nexus: пресеты, басы, верха, баланс",
        version="0.1.0",
        platforms=("windows",),
        spoken=("пис", "peace", "эквалайзер"),
    )

    async def on_setup(self) -> None:
        """Прочитать настройки. Сам Peace здесь не трогается: он может быть не запущен."""
        self._api_dir = Path(str(self.context.setting("api_dir", DEFAULT_API_DIR)))
        self._step = float(self.context.setting("step_db", 2.0))
        self._bass_below = float(self.context.setting("bass_below_hz", 250))
        self._treble_above = float(self.context.setting("treble_above_hz", 4000))
        self._limit = float(self.context.setting("max_gain_db", 12.0))
        self._api: Any = None
        self._lock = asyncio.Lock()

    # --- доступ к Peace ----------------------------------------------------

    def _module(self) -> Any:
        """Загрузить `peace_api` из папки nexus-api один раз."""
        if self._api is None:
            if not (self._api_dir / "peace_api.py").is_file():
                raise PeaceUnavailable(f"нет peace_api.py в {self._api_dir}")
            if str(self._api_dir) not in sys.path:
                sys.path.insert(0, str(self._api_dir))
            try:
                self._api = importlib.import_module("peace_api")
            except Exception as exc:  # noqa: BLE001 — чужой модуль падает как угодно
                raise PeaceUnavailable(f"peace_api не загрузился: {exc}") from exc
        return self._api

    async def _run(self, work: Callable[[Any], Any]) -> Any:
        """Выполнить работу с Peace: по одной, в отдельном потоке."""
        async with self._lock:
            api = self._module()
            return await asyncio.to_thread(work, api)

    def _failure(self, exc: Exception) -> ToolResult:
        """Отказ Peace вслух: его тексты уже человеческие («закройте настройки»)."""
        text = str(exc) or type(exc).__name__
        self.log.warning("Peace: %s", text)
        return ToolResult.failure(
            f"Peace: {text}",
            speech={"ru": f"Эквалайзер не ответил: {text}.", "en": f"Peace refused: {text}."},
        )

    async def _safely(self, work: Callable[[Any], Any]) -> tuple[Any, ToolResult | None]:
        """Выполнить работу; отказ Peace или отсутствие модуля — готовый ответ."""
        try:
            return await self._run(work), None
        except PeaceUnavailable as exc:
            return None, self._failure(exc)
        except Exception as exc:  # noqa: BLE001 — PeaceError и сбои ctypes
            if self._api is not None and isinstance(exc, getattr(self._api, "PeaceError", ())):
                return None, self._failure(exc)
            raise

    async def health(self) -> HealthStatus:
        """Готовность: модуль на месте и окно Peace находится."""
        try:
            await self._run(lambda api: api._window())
        except PeaceUnavailable as exc:
            return HealthStatus.degraded(str(exc))
        except Exception as exc:  # noqa: BLE001
            return HealthStatus.degraded(str(exc))
        return HealthStatus.healthy("Peace отвечает")

    # --- состояние ---------------------------------------------------------

    @tool(phrases=["что с эквалайзером", "статус эквалайзера", "какой пресет стоит",
                   "какой сейчас пресет", "какой пресет в эквалайзере"],
          reversible=True)
    async def status(self) -> ToolResult:
        """Состояние эквалайзера Peace: включён ли, пресет, предусиление, баланс, полосы."""
        state, refusal = await self._safely(lambda api: api.status())
        if refusal:
            return refusal
        return ToolResult.success(state, speech={"ru": describe_status(state), "en": "Equalizer status read."})

    @tool(routable=False, phrases=["какие есть пресеты", "список пресетов", "какие пресеты есть"],
          reversible=True)
    async def presets(self) -> ToolResult:
        """Перечислить пресеты эквалайзера."""
        names, refusal = await self._safely(lambda api: api.presets())
        if refusal:
            return refusal
        listed = ", ".join(names) or "ни одного"
        return ToolResult.success(names, speech={"ru": f"Пресеты: {listed}.", "en": f"Presets: {listed}."})

    # --- включение ---------------------------------------------------------

    @tool(reversible=True)
    async def equalizer(self, on: bool) -> ToolResult:
        """Включить или выключить эквалайзер Peace целиком.

        :param on: true — включить, false — выключить.
        """
        state, refusal = await self._safely(lambda api: api.set_equalizer(bool(on)))
        if refusal:
            return refusal
        return ToolResult.success(
            {"equalizer_on": state},
            speech={
                "ru": ("Эквалайзер включён.", "Включил эквалайзер.") if state
                else ("Эквалайзер выключен.", "Выключил эквалайзер."),
                "en": "Equalizer on." if state else "Equalizer off.",
            },
        )

    @tool(routable=False, phrases=["включи эквалайзер", "включи пис"], reversible=True)
    async def equalizer_on(self) -> ToolResult:
        """Включить эквалайзер."""
        return await self.equalizer(True)

    @tool(routable=False, phrases=["выключи эквалайзер", "выключи пис"], reversible=True)
    async def equalizer_off(self) -> ToolResult:
        """Выключить эквалайзер."""
        return await self.equalizer(False)

    # --- пресеты -----------------------------------------------------------

    @tool(phrases=["поставь пресет {name}", "включи пресет {name}", "загрузи пресет {name}",
                   "пресет {name}"],
          reversible=False)
    async def load_preset(self, name: str) -> ToolResult:
        """Загрузить пресет эквалайзера по названию. Несохранённая кривая пропадает.

        :param name: название пресета, как его назвали.
        """
        names, refusal = await self._safely(lambda api: api.presets())
        if refusal:
            return refusal
        found = pick_preset(name, names)
        if found is None:
            listed = ", ".join(names) or "ни одного"
            return ToolResult.failure(
                f"пресет {name!r} не найден. Есть: {listed}",
                speech={"ru": f"Не нашёл пресет {name}. Есть: {listed}.", "en": f"No preset {name}."},
            )
        selected, refusal = await self._safely(lambda api: api.load_preset(found))
        if refusal:
            return refusal
        self.log.info("Peace: загружен пресет %s", found)
        return ToolResult.success(
            {"preset": selected or found},
            speech={"ru": f"Пресет {found}.", "en": f"Preset {found}."},
        )

    @tool(routable=False, phrases=["сохрани пресет {name}", "сохрани пресет как {name}",
                                   "сохрани эквалайзер как {name}"],
          reversible=False)
    async def save_preset(self, name: str) -> ToolResult:
        """Сохранить текущий звук как пресет. Пресет с тем же именем перезаписывается.

        :param name: имя пресета.
        """
        saved, refusal = await self._safely(lambda api: api.save_preset(name.strip()))
        if refusal:
            return refusal
        self.log.info("Peace: сохранён пресет %s", saved)
        return ToolResult.success({"preset": saved}, speech={"ru": f"Сохранил пресет {saved}.", "en": f"Saved {saved}."})

    @tool(routable=False, phrases=["выровняй эквалайзер", "сбрось эквалайзер", "эквалайзер в ноль",
                                   "убери эквалайзер в ноль"],
          reversible=False)
    async def flat(self) -> ToolResult:
        """Все полосы в ноль — ровная кривая."""
        _, refusal = await self._safely(lambda api: api.set_all_gains(0))
        if refusal:
            return refusal
        return ToolResult.success(None, speech={"ru": "Эквалайзер выровнен.", "en": "Equalizer is flat."})

    # --- басы и верха ------------------------------------------------------

    @tool(reversible=True)
    async def shift(self, part: str, db: float = 0.0) -> ToolResult:
        """Добавить или убавить басы или верха на эквалайзере.

        :param part: «bass» — басы, «treble» — верха.
        :param db: на сколько децибел; положительное — больше, отрицательное —
            меньше. Ноль — шаг из настроек в сторону «больше».
        """
        bass = part.strip().lower() in ("bass", "басы", "бас", "низы", "low")
        step = float(db) if db else self._step
        # Границы включительно: у владельца полосы стоят ровно на 250 Гц и 4 кГц,
        # и это края басов и верхов, а не середина.
        if bass:
            choose: Callable[[float], bool] = lambda hz: hz <= self._bass_below  # noqa: E731
        else:
            choose = lambda hz: hz >= self._treble_above  # noqa: E731
        what = "басы" if bass else "верха"

        def work(api: Any) -> list[tuple[int, float]]:
            changes = shifted_gains(api.bands(), choose, step, self._limit)
            for number, gain in changes:
                api.set_band_gain(number, gain)
            return changes

        changes, refusal = await self._safely(work)
        if refusal:
            return refusal
        if not changes:
            return ToolResult.failure(
                f"{what}: менять нечего — полос нет или они уже на пределе {self._limit} дБ",
                speech={"ru": f"Сильнее {what} уже не сдвинуть.", "en": "Nothing to change."},
            )
        more = step > 0
        return ToolResult.success(
            {"part": "bass" if bass else "treble", "bands": changes},
            speech={
                "ru": f"{'Добавил' if more else 'Убавил'} {'басов' if bass else 'верхов'} на {decibels(abs(step))}.",
                "en": f"{'More' if more else 'Less'} {'bass' if bass else 'treble'}.",
            },
        )

    @tool(routable=False, phrases=["больше басов", "добавь басов", "добавь баса", "сделай басы громче"],
          reversible=True)
    async def more_bass(self) -> ToolResult:
        """Больше басов."""
        return await self.shift("bass", self._step)

    @tool(routable=False, phrases=["меньше басов", "убавь басы", "убери басы", "сделай басы тише"],
          reversible=True)
    async def less_bass(self) -> ToolResult:
        """Меньше басов."""
        return await self.shift("bass", -self._step)

    @tool(routable=False, phrases=["больше верхов", "добавь верхов", "добавь высоких"],
          reversible=True)
    async def more_treble(self) -> ToolResult:
        """Больше верхов."""
        return await self.shift("treble", self._step)

    @tool(routable=False, phrases=["меньше верхов", "убавь верха", "убери верха", "убавь высокие"],
          reversible=True)
    async def less_treble(self) -> ToolResult:
        """Меньше верхов."""
        return await self.shift("treble", -self._step)

    # --- предусиление, баланс, окно ----------------------------------------

    @tool(routable=False, phrases=["предусиление {db}", "поставь предусиление {db}"], reversible=True)
    async def preamp(self, db: float) -> ToolResult:
        """Предусиление эквалайзера в децибелах.

        :param db: значение в дБ, обычно от −12 до 0.
        """
        value, refusal = await self._safely(lambda api: api.set_preamp(float(db)))
        if refusal:
            return refusal
        return ToolResult.success({"preamp_db": value}, speech={"ru": f"Предусиление {decibels(value)}.", "en": "Preamp set."})

    @tool(routable=False, phrases=["баланс {value}"], reversible=True)
    async def pan(self, value: int) -> ToolResult:
        """Баланс: −100 левый край, 0 центр, 100 правый край.

        :param value: положение баланса.
        """
        result, refusal = await self._safely(lambda api: api.set_pan(int(value)))
        if refusal:
            return refusal
        spoken = "по центру" if result == 0 else f"{'влево' if result < 0 else 'вправо'} на {abs(result)}"
        return ToolResult.success({"pan": result}, speech={"ru": f"Баланс {spoken}.", "en": f"Pan {result}."})

    @tool(routable=False, phrases=["баланс по центру", "баланс в центр", "верни баланс"], reversible=True)
    async def pan_center(self) -> ToolResult:
        """Баланс по центру."""
        return await self.pan(0)

    @tool(routable=False, phrases=["покажи эквалайзер", "открой эквалайзер", "открой пис"], reversible=True)
    async def show(self) -> ToolResult:
        """Показать окно Peace."""
        _, refusal = await self._safely(lambda api: api.show_window(True))
        return refusal or ToolResult.success(None, speech={"ru": "Открыл эквалайзер.", "en": "Showing Peace."})

    @tool(routable=False, phrases=["спрячь эквалайзер", "сверни эквалайзер", "закрой эквалайзер"],
          reversible=True)
    async def hide(self) -> ToolResult:
        """Спрятать окно Peace в трей."""
        _, refusal = await self._safely(lambda api: api.show_window(False))
        return refusal or ToolResult.success(None, speech={"ru": "Спрятал эквалайзер.", "en": "Peace hidden."})
