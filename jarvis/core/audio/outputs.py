"""Аудиовыходы: какие есть, как их называют вслух и как найти названный.

Нужно ради одной просьбы владельца: выбирать голосом, куда ассистент говорит
(«говори через колонку»). Сменить устройство на ходу ничего не стоит: вывод
открывает поток заново на каждую реплику (`SoundDeviceSink`), так что следующая
реплика просто уходит в другое место.

**Список устройств у PortAudio грязный**, и показывать его как есть нельзя.
Одна и та же колонка видна четырьмя интерфейсами (MME, DirectSound, WASAPI,
WDM-KS), у MME имя обрезано до 31 знака («Динамики (VB-Audio Voicemeeter »), а
виртуальные выходы Voicemeeter повторяются под одним именем по пять раз.
Поэтому берётся **один** интерфейс — DirectSound: имена у него полные, а
частоту он пересчитывает сам. WASAPI в общем режиме этого не умеет и отказал
бы голосу на 22 кГц. Одинаковые имена схлопываются: различить их всё равно
нечем, ни на слух, ни глазами.

**Имя устройства — не то, как его называют.** «Onboard Speaker (Audio Device)»
никто не произносит, говорят «динамики ноутбука». Отсюда `audio.output_names`:
часть имени устройства → как называть вслух. По этому названию выход
выбирается, им же и называется в ответе.

Ограничение честное: PortAudio читает список устройств один раз, при старте.
Колонка, подключённая уже после запуска, появится только после перезапуска —
перечитать список на ходу значит оборвать открытый микрофон.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.core.text import best_match

#: Интерфейсы по предпочтению. DirectSound — полные имена и пересчёт частоты.
PREFERRED_HOSTAPIS = ("Windows DirectSound", "MME")

#: Служебные «устройства», которые означают «системный выход» и сами по себе
#: ничего не называют.
_GENERIC = (
    "первичный звуковой драйвер",
    "primary sound driver",
    "переназначение звуковых устр",
    "microsoft sound mapper",
)

#: Слова, которыми просят вернуть выход из настроек.
DEFAULT_WORDS = (
    "по умолчанию", "стандартный", "стандартное", "системный", "системное",
    "обычный", "как обычно", "как было", "default", "usual",
)

#: Порог нечёткой ступени. Ошибка тут дешёвая — голос уйдёт не туда и об этом
#: будет сказано вслух, — но и перебирать соседей по алфавиту незачем.
SIMILARITY = 0.75


@dataclass(frozen=True, slots=True)
class Output:
    """Аудиовыход, через который можно говорить."""

    #: Номер устройства у PortAudio. Живёт до перезапуска: после него номера
    #: другие, поэтому выбор запоминается по имени.
    index: int
    name: str
    #: Как называть вслух.
    spoken: str


def spoken_name(name: str, names: Mapping[str, str]) -> str:
    """Название выхода для речи: из `audio.output_names`, иначе имя без скобок."""
    low = name.lower()
    for part, spoken in names.items():
        if part and part.lower() in low:
            return spoken
    return " ".join(re.sub(r"[()]", " ", name).split())


def usable_outputs(
    devices: Sequence[Mapping[str, Any]],
    hostapis: Sequence[Mapping[str, Any]],
    names: Mapping[str, str] | None = None,
) -> list[Output]:
    """Выходы для выбора голосом: один интерфейс, без служебных и повторов."""
    names = names or {}
    by_api: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, device in enumerate(devices):
        if int(device.get("max_output_channels", 0) or 0) <= 0:
            continue
        api = int(device.get("hostapi", -1))
        api_name = str(hostapis[api].get("name", "")) if 0 <= api < len(hostapis) else ""
        by_api.setdefault(api_name, []).append((index, device))

    chosen = next((by_api[api] for api in PREFERRED_HOSTAPIS if api in by_api), None)
    if chosen is None:
        chosen = [pair for pairs in by_api.values() for pair in pairs]

    seen: set[str] = set()
    result: list[Output] = []
    for index, device in chosen:
        name = str(device.get("name", "")).strip()
        key = name.lower()
        if not name or key in seen or any(generic in key for generic in _GENERIC):
            continue
        seen.add(key)
        result.append(Output(index=index, name=name, spoken=spoken_name(name, names)))
    return result


def is_default(query: str) -> bool:
    """Просят ли вернуть выход по умолчанию, а не назвали устройство."""
    low = " ".join(query.lower().split())
    return any(word in low for word in DEFAULT_WORDS)


def find_output(query: str, outputs: Sequence[Output]) -> Output | None:
    """Найти выход, названный вслух: по своему названию, имени или его части в скобках."""
    labels: dict[str, Output] = {}
    for output in outputs:
        labels.setdefault(output.spoken, output)
        labels.setdefault(output.name, output)
        inner = re.search(r"\(([^()]+)\)", output.name)
        if inner:
            labels.setdefault(inner.group(1).strip(), output)
    found = best_match(query, labels, similarity=SIMILARITY)
    return labels[found] if found else None


def query_outputs(names: Mapping[str, str] | None = None) -> list[Output]:
    """Спросить у PortAudio, какие выходы есть. Блокирующий вызов."""
    from .devices import _import_sounddevice

    sd = _import_sounddevice()
    return usable_outputs(list(sd.query_devices()), list(sd.query_hostapis()), names)
