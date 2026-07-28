"""Скачивание моделей синтеза речи.

Модель Whisper faster-whisper тянет сам при первом запуске, а голоса — нет.
Этот модуль убирает ручной шаг: настройка на новой машине не превращается в
поиск ссылок по репозиториям.

Голоса записываются как ``движок:голос``. Без префикса подразумевается Piper —
так работают старые конфиги.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from jarvis.core.errors import JarvisError

logger = logging.getLogger(__name__)

_PIPER_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_KOKORO_REPO = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_SILERO_REPO = "https://models.silero.ai/models/tts"

#: Голоса Piper: лёгкий движок, 22 кГц.
PIPER_VOICES = (
    "ru_RU-dmitri-medium",
    "ru_RU-denis-medium",
    "ru_RU-ruslan-medium",
    "ru_RU-irina-medium",
    "en_US-ryan-high",
    "en_US-lessac-high",
    "en_GB-alan-medium",
)

#: Голоса Kokoro: 24 кГц, естественная интонация. Британские мужские — самое
#: близкое к Джарвису из «Железного человека».
KOKORO_VOICES = (
    "bm_george",
    "bm_daniel",
    "bm_lewis",
    "bm_fable",
    "am_michael",
    "am_onyx",
    "bf_emma",
    "af_heart",
)

#: Голоса Silero: 48 кГц, русский звучит живее, чем у Piper.
SILERO_VOICES = ("eugene", "aidar", "baya", "kseniya", "xenia")

#: Нейроголоса Microsoft: считаются в облаке, звучат естественнее всего
#: локального. Ключ не нужен, но нужен интернет.
EDGE_VOICES = (
    "ru-RU-DmitryNeural",
    "ru-RU-SvetlanaNeural",
    "en-GB-RyanNeural",
    "en-GB-ThomasNeural",
    "en-GB-SoniaNeural",
    "en-US-GuyNeural",
)

#: Что это за голос по-человечески.
VOICE_NOTES: dict[str, str] = {
    "piper:ru_RU-dmitri-medium": "русский мужской, ровный",
    "piper:ru_RU-denis-medium": "русский мужской, мягче",
    "piper:ru_RU-ruslan-medium": "русский мужской, ниже",
    "piper:ru_RU-irina-medium": "русский женский",
    "piper:en_US-ryan-high": "английский мужской, американский",
    "piper:en_US-lessac-high": "английский женский",
    "piper:en_GB-alan-medium": "английский мужской, британский",
    "kokoro:bm_george": "британский мужской, спокойный — ближе всего к Джарвису",
    "kokoro:bm_daniel": "британский мужской, суше",
    "kokoro:bm_lewis": "британский мужской, ниже",
    "kokoro:bm_fable": "британский мужской, теплее",
    "kokoro:am_michael": "американский мужской",
    "kokoro:am_onyx": "американский мужской, глубокий",
    "kokoro:bf_emma": "британский женский",
    "kokoro:af_heart": "американский женский",
    "edge:ru-RU-DmitryNeural": "русский мужской, облачный — самый естественный",
    "edge:ru-RU-SvetlanaNeural": "русский женский, облачный",
    "edge:en-GB-RyanNeural": "британский мужской, облачный",
    "edge:en-GB-ThomasNeural": "британский мужской, облачный, суше",
    "edge:en-GB-SoniaNeural": "британский женский, облачный",
    "edge:en-US-GuyNeural": "американский мужской, облачный",
    "xtts:bm_george": "русский голосом bm_george — тот же тембр, что в английском",
    "silero:eugene": "русский мужской",
    "silero:aidar": "русский мужской, ровный",
    "silero:baya": "русский женский",
    "silero:kseniya": "русский женский, мягче",
    "silero:xenia": "русский женский, живее",
}

#: Фраза для эталона XTTS: нужно 10–20 секунд связной чистой речи.
REFERENCE_TEXT = (
    "Good evening, sir. The studio is at twenty two degrees and everything is "
    "running smoothly. I have prepared the recording session, and the lights are "
    "set to your usual preference. Shall I switch to game mode, or would you "
    "rather continue working for a while longer?"
)


def make_reference(source: str, models_dir: Path) -> Path:
    """Создать эталон голоса для XTTS из любого другого движка.

    Так русская речь получает тембр английского голоса: XTTS синтезирует по
    образцу, а образец берётся у того самого голоса, который понравился.

    :param source: голос-источник, например ``kokoro:bm_george``.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    """
    import wave

    from jarvis.core.tts.backends import build_backend

    engine, voice = _split(source)
    if engine == "xtts":
        raise JarvisError("Эталон нужно снимать с обычного голоса, а не с xtts")

    download_voice(source, models_dir)
    backend = build_backend(engine, models_dir)

    print(f"Снимаю эталон с {source} — {len(REFERENCE_TEXT.split())} слов")
    audio, rate = backend.synthesize(REFERENCE_TEXT, voice, "en")  # type: ignore[attr-defined]

    target = models_dir / "xtts" / f"{voice}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(audio)

    seconds = len(audio) / 2 / rate
    print(f"Готово: {target} ({seconds:.0f} с, {rate} Гц)")
    print(f"Теперь в config.yaml можно писать:  ru: xtts:{voice}")
    return target


#: Подборки для быстрого прослушивания.
GROUPS: dict[str, tuple[str, ...]] = {
    "jarvis": ("kokoro:bm_george", "kokoro:bm_daniel", "kokoro:bm_lewis", "kokoro:bm_fable"),
    "en": tuple(f"kokoro:{v}" for v in KOKORO_VOICES)
    + ("piper:en_GB-alan-medium", "piper:en_US-ryan-high"),
    "ru": ("edge:ru-RU-DmitryNeural",)
    + tuple(f"silero:{v}" for v in SILERO_VOICES)
    + ("piper:ru_RU-denis-medium", "piper:ru_RU-ruslan-medium"),
    "clone": ("xtts:bm_george",),
    "cloud": tuple(f"edge:{v}" for v in EDGE_VOICES),
    "edge": tuple(f"edge:{v}" for v in EDGE_VOICES),
    "kokoro": tuple(f"kokoro:{v}" for v in KOKORO_VOICES),
    "silero": tuple(f"silero:{v}" for v in SILERO_VOICES),
    "piper": tuple(f"piper:{v}" for v in PIPER_VOICES),
}

#: Что произносить при прослушивании.
SAMPLE_TEXT = {
    "ru": "Добрый вечер. В студии двадцать два градуса. Включить игровой режим?",
    "en": "Good evening, sir. The studio is at twenty two degrees. "
    "Shall I switch to game mode?",
}


def _split(spec: str) -> tuple[str, str]:
    """Разобрать ``движок:голос``; без префикса — Piper."""
    if ":" in spec:
        engine, _, voice = spec.partition(":")
        return engine.strip().lower(), voice.strip()
    return "piper", spec.strip()


def _download(client: httpx.Client, url: str, target: Path) -> int:
    """Скачать файл потоком, показывая прогресс для крупных файлов."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    written = 0

    with client.stream("GET", url, follow_redirects=True, timeout=180.0) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        with temporary.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 16):
                handle.write(chunk)
                written += len(chunk)
                if total > 1 << 20 and written % (10 << 20) < (1 << 16):
                    print(f"    {written / 1048576:.0f} из {total / 1048576:.0f} МБ", flush=True)

    temporary.replace(target)
    return written


def _piper_url(voice: str) -> str:
    """Адрес голоса Piper по имени вида ``ru_RU-dmitri-medium``."""
    parts = voice.split("-")
    if len(parts) != 3:
        raise JarvisError(
            f"Непонятное имя голоса Piper {voice!r}. Ожидается ru_RU-dmitri-medium."
        )
    locale, speaker, quality = parts
    return f"{_PIPER_REPO}/{locale.split('_')[0]}/{locale}/{speaker}/{quality}/{voice}"


def download_voice(spec: str, models_dir: Path) -> Path:
    """Скачать голос и вернуть путь к главному файлу модели.

    :param spec: ``движок:голос`` либо просто имя голоса Piper.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    """
    engine, voice = _split(spec)

    with httpx.Client() as client:
        try:
            if engine == "piper":
                target = models_dir / "piper" / f"{voice}.onnx"
                if target.is_file() and target.with_suffix(".onnx.json").is_file():
                    return target
                print(f"Скачиваю голос Piper {voice}")
                base = _piper_url(voice)
                _download(client, f"{base}.onnx", target)
                _download(client, f"{base}.onnx.json", models_dir / "piper" / f"{voice}.onnx.json")
                return target

            if engine == "kokoro":
                directory = models_dir / "kokoro"
                model = directory / "kokoro-v1.0.onnx"
                voices = directory / "voices-v1.0.bin"
                if model.is_file() and voices.is_file():
                    return model
                print("Скачиваю модель Kokoro (338 МБ, одна на все её голоса)")
                _download(client, f"{_KOKORO_REPO}/kokoro-v1.0.onnx", model)
                _download(client, f"{_KOKORO_REPO}/voices-v1.0.bin", voices)
                return model

            if engine == "silero":
                target = models_dir / "silero" / "v4_ru.pt"
                if target.is_file():
                    return target
                print("Скачиваю модель Silero (39 МБ, одна на все её голоса)")
                _download(client, f"{_SILERO_REPO}/ru/v4_ru.pt", target)
                return target

            if engine == "edge":
                # Модель живёт в облаке — скачивать нечего.
                return models_dir

            if engine == "xtts":
                # Саму модель тянет coqui-tts при первом синтезе; здесь нужен
                # только эталон, снятый с другого голоса.
                reference = models_dir / "xtts" / f"{voice}.wav"
                if reference.is_file():
                    return reference
                raise JarvisError(
                    f"Нет эталона голоса {voice}. Сними его с понравившегося голоса: "
                    f"python -m jarvis --make-reference kokoro:{voice}"
                )

        except httpx.HTTPStatusError as exc:
            raise JarvisError(
                f"Голос {spec} не найден ({exc.response.status_code}). "
                f"Список: python -m jarvis --download-voice"
            ) from exc
        except httpx.HTTPError as exc:
            raise JarvisError(f"Сеть недоступна: {exc}") from exc

    raise JarvisError(f"Неизвестный движок {engine!r}. Есть: piper, kokoro, silero.")


def preview_voices(names: list[str], models_dir: Path, *, text: str | None = None) -> None:
    """Скачать голоса и произнести ими образец, чтобы выбрать на слух.

    :param names: голоса либо подборка — ``jarvis``, ``ru``, ``en``.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    :param text: своя фраза вместо образца.
    """
    from jarvis.core.tts.backends import build_backend

    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001 — пакет падает уже при импорте
        raise JarvisError(
            f"Нет звукового устройства ({type(exc).__name__}: {exc}). "
            f"Прослушивание голосов работает только там, где есть колонки."
        ) from exc

    expanded: list[str] = []
    for name in names:
        expanded.extend(GROUPS.get(name, (name,)))

    backends: dict[str, object] = {}
    for spec in expanded:
        engine, voice = _split(spec)
        try:
            download_voice(spec, models_dir)
        except JarvisError as exc:
            print(f"\n✗ {spec}: {exc}")
            continue

        russian = (
            engine in ("silero", "xtts")
            or voice.startswith("ru_")
            or voice.startswith("ru-")
        )
        language = "ru" if russian else "en"
        sample = text or SAMPLE_TEXT[language]

        print(f"\n▶ {spec}  ({VOICE_NOTES.get(spec, '')})")
        print(f"  {sample}")

        backend = backends.get(engine) or build_backend(engine, models_dir)
        backends[engine] = backend
        try:
            audio, rate = backend.synthesize(sample, voice, language)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — один сбойный голос не должен рвать перебор
            print(f"  ✗ не удалось: {type(exc).__name__}: {exc}")
            continue

        with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as stream:
            stream.write(audio)

    print("\nПонравившийся впиши в config.yaml:")
    print("  tts:")
    print("    voices:")
    print("      ru: silero:eugene")
    print("      en: kokoro:bm_george")


def list_voices() -> str:
    """Текстовый список голосов для подсказки в CLI."""
    lines = [
        "Голоса. Пишутся как движок:голос",
        "",
        "Edge — нейроголоса Microsoft, считаются в облаке. Звучат естественнее",
        "всего локального; ключ не нужен, нужен интернет и ~1 с на фразу:",
    ]
    for voice in EDGE_VOICES:
        spec = f"edge:{voice}"
        lines.append(f"  {spec:<28} {VOICE_NOTES.get(spec, '')}")

    lines += ["", "Kokoro — 24 кГц, естественная интонация, модель 338 МБ:"]
    for voice in KOKORO_VOICES:
        spec = f"kokoro:{voice}"
        lines.append(f"  {spec:<22} {VOICE_NOTES.get(spec, '')}")

    lines += ["", "Silero — 48 кГц, русский, модель 39 МБ (нужен torch):"]
    for voice in SILERO_VOICES:
        spec = f"silero:{voice}"
        lines.append(f"  {spec:<22} {VOICE_NOTES.get(spec, '')}")

    lines += [
        "",
        "XTTS — говорит по-русски голосом с образца (модель ~1.8 ГБ, нужна видеокарта",
        "для приемлемой скорости; лицензия Coqui — некоммерческое использование):",
        "  xtts:bm_george         русский тем же голосом, что английский bm_george",
        "  сначала:  python -m jarvis --make-reference kokoro:bm_george",
        "",
        "Piper — самый лёгкий и быстрый, 22 кГц:",
    ]
    for voice in PIPER_VOICES:
        spec = f"piper:{voice}"
        lines.append(f"  {spec:<22} {VOICE_NOTES.get(spec, '')}")

    lines += [
        "",
        "Послушать и выбрать:",
        "  python -m jarvis --try-voice jarvis   # британские мужские, как в фильме",
        "  python -m jarvis --try-voice ru       # русские",
        "  python -m jarvis --try-voice en       # английские",
        "  python -m jarvis --try-voice kokoro:bm_george silero:eugene",
        "  python -m jarvis --try-voice cloud    # облачные нейроголоса",
        "  python -m jarvis --try-voice clone    # русский голосом bm_george",
    ]
    return "\n".join(lines)
