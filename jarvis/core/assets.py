"""Скачивание моделей синтеза речи.

Модель Whisper faster-whisper тянет сам при первом запуске, а голоса — нет.
Этот модуль убирает ручной шаг: настройка на новой машине не превращается в
поиск ссылок по репозиториям.

Голоса записываются как ``движок:голос``. Без префикса подразумевается Piper —
так работают старые конфиги.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import httpx

from jarvis.core.errors import JarvisError

logger = logging.getLogger(__name__)

_PIPER_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
_KOKORO_REPO = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_SILERO_REPO = "https://models.silero.ai/models/tts"
_VOSK_REPO = "https://alphacephei.com/vosk/models"

#: Модель Silero VAD — та же лаборатория, но это не синтез, а детектор речи.
#: Берётся из репозитория с моделью, лицензия MIT, вес около двух мегабайт.
_SILERO_VAD_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/master"
    "/src/silero_vad/data/silero_vad.onnx"
)


def ensure_vad_model(models_dir: Path) -> Path:
    """Вернуть путь к модели Silero VAD, скачав её при первом запуске.

    Отдельная функция, а не часть загрузки голосов: VAD к синтезу отношения не
    имеет, лежит в своём каталоге и нужен раньше — на входе, а не на выходе.
    """
    target = models_dir / "silero_vad.onnx"
    if target.is_file():
        return target

    logger.info("Скачиваю модель Silero VAD (около 2 МБ)")
    with httpx.Client() as client:
        _download(client, _SILERO_VAD_URL, target, timeout=30.0)
    return target

#: Имя модели Vosk дублировать нельзя — берём то же, что знает движок.
#: Импорт ленивый: `assets` работает и там, где numpy не установлен.
def _vosk_model() -> str:
    """Имя модели Vosk-TTS из движка."""
    from jarvis.core.tts.backends import VOSK_MODEL

    return VOSK_MODEL

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

#: Дикторы Vosk: все в одной модели, имена из её ``speaker_id_map``.
VOSK_VOICES = ("male_0", "male_1", "female_0", "female_1", "female_2")

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
    "vosk:male_0": "русский мужской, интонация по смыслу фразы",
    "vosk:male_1": "русский мужской, второй диктор",
    "vosk:female_0": "русский женский",
    "vosk:female_1": "русский женский, второй диктор",
    "vosk:female_2": "русский женский, третий диктор",
}

#: Фраза для эталона XTTS: нужно 10–20 секунд связной чистой речи.
REFERENCE_TEXT = {
    "ru": (
        "Добрый вечер. В студии двадцать два градуса, всё работает штатно. "
        "Сессия записи готова, свет выставлен как вы обычно любите. "
        "Включить игровой режим, или вы ещё поработаете?"
    ),
    "en": (
        "Good evening, sir. The studio is at twenty two degrees and everything is "
        "running smoothly. I have prepared the recording session, and the lights are "
        "set to your usual preference. Shall I switch to game mode, or would you "
        "rather continue working for a while longer?"
    ),
}


def _is_russian(engine: str, voice: str) -> bool:
    """Русский ли это голос — по движку и по имени.

    XTTS сюда не попадает намеренно: он говорит на языке, который ему задали,
    а тембр берёт с эталона. Языка у него своего нет.
    """
    return (
        engine in ("silero", "vosk")
        or voice.startswith("ru_")
        or voice.startswith("ru-")
    )


def _write_wav(target: Path, audio: bytes, rate: int) -> Path:
    """Сохранить моно-PCM 16 бит в WAV."""
    import wave

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(audio)
    return target


def _reference_from_file(source: Path, models_dir: Path) -> Path:
    """Взять эталон из готовой записи — хоть с микрофона.

    Это лучший вариант из возможных: клон делается с живого голоса, без потери
    качества на промежуточном синтезе.
    """
    import wave

    target = models_dir / "xtts" / f"{source.stem}.wav"

    if source.suffix.lower() == ".wav":
        with wave.open(str(source)) as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise JarvisError(
                    f"Нужен моно WAV 16 бит, а в {source.name} "
                    f"{handle.getnchannels()} канала по {handle.getsampwidth() * 8} бит"
                )
            audio = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
    else:
        try:
            import av
        except ImportError as exc:
            raise JarvisError(
                f"Чтобы читать {source.suffix}, нужен пакет av: pip install -e '.[edge]'. "
                f"Либо сохрани запись как моно WAV 16 бит."
            ) from exc

        rate = 22050
        container = av.open(str(source))
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
        pcm = bytearray()
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                pcm += bytes(resampled.planes[0])[: resampled.samples * 2]
        audio = bytes(pcm)

    seconds = len(audio) / 2 / rate
    if seconds < 6:
        print(f"⚠ Запись короткая ({seconds:.0f} с) — XTTS хочет 10–20 секунд речи")
    return _write_wav(target, audio, rate)


def make_reference(source: str, models_dir: Path) -> Path:
    """Создать эталон голоса для XTTS.

    XTTS синтезирует по образцу, поэтому им можно договорить то, чего основной
    движок не умеет: русскому голосу — английские вставки, английскому —
    русские. Тембр остаётся тем же, и переключение языка не слышно как смена
    диктора.

    :param source: голос-источник (``vosk:male_0``, ``kokoro:bm_george``)
        либо путь к записи живого голоса — так клон получается чище всего.
    :param models_dir: каталог из конфига ``tts.models_dir``.
    """
    from jarvis.core.tts.backends import build_backend

    path = Path(source)
    if path.is_file():
        target = _reference_from_file(path, models_dir)
        print(f"Готово: {target}")
        print(f"Теперь в config.yaml можно писать:  en: xtts:{target.stem}")
        return target

    engine, voice = _split(source)
    if engine == "xtts":
        raise JarvisError("Эталон нужно снимать с обычного голоса, а не с xtts")

    download_voice(source, models_dir)
    backend = build_backend(engine, models_dir)

    # Читать образец лучше на родном языке голоса: чужой алфавит движок
    # проговаривает по буквам, и эталон получается из мусора.
    language = "ru" if _is_russian(engine, voice) else "en"
    text = REFERENCE_TEXT[language]

    print(f"Снимаю эталон с {source} — {len(text.split())} слов на {language}")
    audio, rate = backend.synthesize(text, voice, language)  # type: ignore[attr-defined]

    target = _write_wav(models_dir / "xtts" / f"{voice}.wav", audio, rate)
    seconds = len(audio) / 2 / rate
    print(f"Готово: {target} ({seconds:.0f} с, {rate} Гц)")
    other = "en" if language == "ru" else "ru"
    print(f"Теперь в config.yaml можно писать:  {other}: xtts:{voice}")
    return target


#: Подборки для быстрого прослушивания.
GROUPS: dict[str, tuple[str, ...]] = {
    "jarvis": ("kokoro:bm_george", "kokoro:bm_daniel", "kokoro:bm_lewis", "kokoro:bm_fable"),
    "en": tuple(f"kokoro:{v}" for v in KOKORO_VOICES)
    + ("piper:en_GB-alan-medium", "piper:en_US-ryan-high"),
    # Русские: сначала непрослушанное — Vosk. Остальное уже отбраковано на слух,
    # но оставлено, чтобы было с чем сравнивать.
    "ru": tuple(f"vosk:{v}" for v in VOSK_VOICES)
    + ("edge:ru-RU-DmitryNeural",)
    + tuple(f"silero:{v}" for v in SILERO_VOICES)
    + ("piper:ru_RU-denis-medium", "piper:ru_RU-ruslan-medium"),
    "clone": ("xtts:bm_george",),
    "vosk": tuple(f"vosk:{v}" for v in VOSK_VOICES),
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


def _download(client: httpx.Client, url: str, target: Path, *, timeout: float = 180.0) -> int:
    """Скачать файл потоком, показывая прогресс для крупных файлов.

    :param timeout: голоса весят сотни мегабайт и ждут долго, но мелкие файлы
        столько ждать не должны: загрузка модели VAD идёт при старте, и глухая
        сеть подвесила бы запуск на три минуты.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    written = 0

    with client.stream("GET", url, follow_redirects=True, timeout=timeout) as response:
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

            if engine == "vosk":
                name = _vosk_model()
                target = models_dir / "vosk" / name
                if (target / "model.onnx").is_file():
                    return target
                print(f"Скачиваю модель Vosk {name} (750 МБ, одна на всех дикторов)")
                archive = models_dir / "vosk" / f"{name}.zip"
                _download(client, f"{_VOSK_REPO}/{name}.zip", archive)
                print("  распаковываю")
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(models_dir / "vosk")
                archive.unlink()
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
                    f"Нет эталона голоса {voice}. Сними его с понравившегося голоса "
                    f"или с записи: python -m jarvis --make-reference vosk:{voice}"
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

        # Клон говорит на обоих языках, и слушать его нужно тоже на обоих:
        # смысл клонирования в том, чтобы вставка не звучала другим диктором.
        if engine == "xtts":
            languages = ("ru", "en")
        else:
            languages = ("ru",) if _is_russian(engine, voice) else ("en",)

        print(f"\n▶ {spec}  ({VOICE_NOTES.get(spec, '')})")
        backend = backends.get(engine) or build_backend(engine, models_dir)
        backends[engine] = backend

        for language in languages:
            sample = text or SAMPLE_TEXT[language]
            print(f"  {sample}")
            try:
                audio, rate = backend.synthesize(sample, voice, language)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — один голос не должен рвать перебор
                print(f"  ✗ не удалось: {type(exc).__name__}: {exc}")
                continue

            with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as stream:
                stream.write(audio)

    print("\nПонравившийся впиши в config.yaml:")
    print("  tts:")
    print("    voices:")
    print("      ru: vosk:male_0")
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

    lines += [
        "",
        "Vosk — русский, интонацию расставляет BERT по смыслу фразы, поэтому",
        "звучит живее остальных локальных. Модель 750 МБ, все дикторы в ней:",
    ]
    for voice in VOSK_VOICES:
        spec = f"vosk:{voice}"
        lines.append(f"  {spec:<22} {VOICE_NOTES.get(spec, '')}")

    lines += ["", "Silero — 48 кГц, русский, модель 39 МБ (нужен torch):"]
    for voice in SILERO_VOICES:
        spec = f"silero:{voice}"
        lines.append(f"  {spec:<22} {VOICE_NOTES.get(spec, '')}")

    lines += [
        "",
        "XTTS — говорит любым голосом с образца, на любом из языков (модель ~1.8 ГБ,",
        "нужна видеокарта; лицензия Coqui — некоммерческое использование).",
        "Нужен, чтобы вставки на втором языке звучали тем же голосом:",
        "  сначала:  python -m jarvis --make-reference vosk:male_0",
        "  или с записи живого голоса:  python -m jarvis --make-reference голос.wav",
        "  потом в config.yaml:  en: xtts:male_0",
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
        "  python -m jarvis --try-voice vosk     # русские дикторы Vosk",
        "  python -m jarvis --try-voice ru       # русские",
        "  python -m jarvis --try-voice en       # английские",
        "  python -m jarvis --try-voice kokoro:bm_george silero:eugene",
        "  python -m jarvis --try-voice cloud    # облачные нейроголоса",
        "  python -m jarvis --try-voice clone    # русский голосом bm_george",
    ]
    return "\n".join(lines)
