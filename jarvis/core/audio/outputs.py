"""Звуковые устройства: какие есть, как их называют вслух и как найти названное.

Нужно ради двух просьб владельца: выбирать голосом, куда ассистент говорит
(«говори через колонку»), и выбирать в панели, какой микрофон он слушает.
Сменить выход на ходу ничего не стоит: вывод открывает поток заново на каждую
реплику (`SoundDeviceSink`). Микрофон держится открытым всё время, поэтому он
меняется в конфиге и действует после перезапуска.

**Список устройств у PortAudio грязный**, и показывать его как есть нельзя.
Одна и та же колонка видна четырьмя интерфейсами (MME, DirectSound, WASAPI,
WDM-KS), а виртуальные выходы Voicemeeter повторяются под одним именем по пять
раз. Поэтому работаем через **MME** — тем же путём, каким звучит голос по
умолчанию, и он у владельца слышен. Беда MME одна: имена обрезаны до 31 знака
(«Динамики (VB-Audio Voicemeeter »), поэтому полные имена берутся у
DirectSound, где они целые. Через сам DirectSound голос молча уходил в никуда
(замер петлёй, `docs/lessons.md`). WASAPI в общем режиме не пересчитывает
частоту и не открывается вовсе.

**Имя устройства — не то, как его называют.** «Onboard Speaker (Audio Device)»
никто не произносит, говорят «динамики ноутбука». Отсюда `audio.output_names`:
часть имени устройства → как называть вслух.

**Искать надо по словам, а не по строке целиком.** «Колонки ноутбука» — это
динамики ноутбука, но строкой целиком фраза начинается с «колонк» и совпала
краем с названием «колонка», то есть с JBL (живой запуск 14.09.2026). Слова
вида «колонки», «динамики», «выход» называют род устройства, а не само
устройство, и весят поэтому в десять раз меньше слов, которые его отличают.

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
from jarvis.core.text.matching import forms, touches

#: Через что играть и слушать, по предпочтению. MME — путь по умолчанию.
PLAYBACK_HOSTAPIS = ("MME", "Windows DirectSound")
#: У кого брать полные имена.
NAMES_HOSTAPI = "Windows DirectSound"
#: Длина, до которой MME обрезает имя устройства.
MME_NAME_LIMIT = 31

#: Служебные «устройства», которые означают «системное по умолчанию» и сами по
#: себе ничего не называют.
_GENERIC = (
    "первичный звуковой драйвер",
    "первичный драйвер записи звука",
    "primary sound driver",
    "primary sound capture driver",
    "переназначение звуковых устр",
    "microsoft sound mapper",
)

#: Слова, которыми просят вернуть выход из настроек.
DEFAULT_WORDS = (
    "по умолчанию", "стандартный", "стандартное", "системный", "системное",
    "обычный", "как обычно", "как было", "default", "usual",
)

#: Начала слов, называющих род устройства, а не само устройство.
KIND_WORDS = (
    "колонк", "динамик", "наушник", "гарнитур", "выход", "звук", "аудио", "устройств",
    "speaker", "headphone", "headset", "output", "audio", "device", "sound",
)
#: Во сколько раз такое слово легче отличительного.
KIND_WEIGHT = 0.1

#: Порог нечёткой ступени, когда ни одно слово не совпало.
SIMILARITY = 0.75


@dataclass(frozen=True, slots=True)
class Output:
    """Звуковое устройство: выход, через который говорить, или микрофон."""

    #: Номер устройства у PortAudio. Живёт до перезапуска: после него номера
    #: другие, поэтому выбор запоминается по имени.
    index: int
    name: str
    #: Как называть вслух.
    spoken: str
    #: Имя, как его отдал PortAudio в том интерфейсе, через который работаем
    #: (у MME — обрезанное). Нужно для конфига: по нему устройство и открывается.
    raw: str = ""
    hostapi: str = ""


def spoken_name(name: str, names: Mapping[str, str]) -> str:
    """Название устройства для речи: из `audio.output_names`, иначе имя без скобок."""
    low = name.lower()
    for part, spoken in names.items():
        if part and part.lower() in low:
            return spoken
    return " ".join(re.sub(r"[()]", " ", name).split())


def config_value(device: Output) -> str:
    """Как записать устройство в конфиг, чтобы sounddevice нашёл ровно его.

    Одно имя видно в четырёх интерфейсах, и по голому имени sounddevice
    отвечает «нашлось несколько». Имя вместе с интерфейсом совпадает точно.
    """
    return f"{device.raw}, {device.hostapi}" if device.hostapi else device.raw


def _hostapi_name(device: Mapping[str, Any], hostapis: Sequence[Mapping[str, Any]]) -> str:
    api = int(device.get("hostapi", -1))
    return str(hostapis[api].get("name", "")) if 0 <= api < len(hostapis) else ""


def _full_name(name: str, full_names: Sequence[str]) -> str:
    """Восстановить имя, обрезанное MME, по полному имени у DirectSound."""
    if len(name) < MME_NAME_LIMIT:
        return name
    head = name.rstrip()
    return next((full for full in full_names if full.startswith(head)), head)


def _usable(
    devices: Sequence[Mapping[str, Any]],
    hostapis: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    channels: str,
) -> list[Output]:
    by_api: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, device in enumerate(devices):
        if int(device.get(channels, 0) or 0) <= 0:
            continue
        by_api.setdefault(_hostapi_name(device, hostapis), []).append((index, device))

    full_names = [str(device.get("name", "")).strip() for _, device in by_api.get(NAMES_HOSTAPI, [])]
    api = next((api for api in PLAYBACK_HOSTAPIS if api in by_api), None)
    chosen = by_api[api] if api else [pair for pairs in by_api.values() for pair in pairs]

    seen: set[str] = set()
    result: list[Output] = []
    for index, device in chosen:
        raw = str(device.get("name", ""))
        name = (_full_name(raw, full_names) if api == "MME" else raw).strip()
        key = name.lower()
        if not name or key in seen or any(generic in key for generic in _GENERIC):
            continue
        seen.add(key)
        result.append(
            Output(index=index, name=name, spoken=spoken_name(name, names), raw=raw, hostapi=api or "")
        )
    return result


def usable_outputs(
    devices: Sequence[Mapping[str, Any]],
    hostapis: Sequence[Mapping[str, Any]],
    names: Mapping[str, str] | None = None,
) -> list[Output]:
    """Выходы для выбора голосом: один интерфейс, полные имена, без служебных и повторов."""
    return _usable(devices, hostapis, names or {}, "max_output_channels")


def usable_inputs(
    devices: Sequence[Mapping[str, Any]],
    hostapis: Sequence[Mapping[str, Any]],
) -> list[Output]:
    """Микрофоны: по тем же правилам, что и выходы."""
    return _usable(devices, hostapis, {}, "max_input_channels")


def is_default(query: str) -> bool:
    """Просят ли вернуть выход по умолчанию, а не назвали устройство."""
    low = " ".join(query.lower().split())
    return any(word in low for word in DEFAULT_WORDS)


def _words(text: str) -> list[str]:
    return [word for word in re.findall(r"[^\W_]+", text.lower()) if len(word) >= 2]


def _same_word(left: str, right: str) -> bool:
    mine, theirs = forms(left, least=2), forms(right, least=2)
    return bool(mine & theirs) or any(touches(a, b, least=4) for a in mine for b in theirs)


def _weight(word: str) -> float:
    shapes = forms(word, least=2)
    kind = any(shape.startswith(prefix) for shape in shapes for prefix in KIND_WORDS)
    return KIND_WEIGHT if kind else 1.0


def _labels(output: Output) -> list[str]:
    labels = [output.spoken, output.name]
    inner = re.search(r"\(([^()]+)\)", output.name)
    if inner:
        labels.append(inner.group(1))
    return labels


def find_output(query: str, outputs: Sequence[Output]) -> Output | None:
    """Найти выход, названный вслух.

    Сначала по словам: каждое слово запроса, нашедшее себя в названиях выхода,
    приносит свой вес. Побеждает один лучший; ничья — значит назвали неоднозначно
    («динамики»), и лучше переспросить списком, чем угадать. Ни одно слово не
    совпало — последняя попытка лестницей сопоставления по строке целиком.
    """
    asked = _words(query)
    scored: list[tuple[float, Output]] = []
    for output in outputs:
        known = [word for label in _labels(output) for word in _words(label)]
        score = sum(_weight(word) for word in asked if any(_same_word(word, own) for own in known))
        scored.append((score, output))
    best = max((score for score, _ in scored), default=0.0)
    if best > 0:
        leaders = [output for score, output in scored if score == best]
        return leaders[0] if len(leaders) == 1 else None

    labels: dict[str, Output] = {}
    for output in outputs:
        for label in _labels(output):
            labels.setdefault(label.strip(), output)
    found = best_match(query, labels, similarity=SIMILARITY)
    return labels[found] if found else None


def query_outputs(names: Mapping[str, str] | None = None) -> list[Output]:
    """Спросить у PortAudio, какие выходы есть. Блокирующий вызов."""
    from .devices import _import_sounddevice

    sd = _import_sounddevice()
    return usable_outputs(list(sd.query_devices()), list(sd.query_hostapis()), names)


def query_inputs() -> list[Output]:
    """Спросить у PortAudio, какие микрофоны есть. Блокирующий вызов."""
    from .devices import _import_sounddevice

    sd = _import_sounddevice()
    return usable_inputs(list(sd.query_devices()), list(sd.query_hostapis()))
