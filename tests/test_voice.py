"""Голосовой конвейер: обращение по имени, VAD, тайминги захвата."""

from __future__ import annotations

import time

import pytest

from pathlib import Path

from jarvis.core.audio import AudioFrame, EnergyVAD, SileroVAD, frame_rms
from jarvis.core.bus import LocalEventBus
from jarvis.core.config import AudioConfig, VADConfig, WakeWordConfig
from jarvis.core.contracts import ToolResult, Utterance
from jarvis.core.persona import DONE, FAILED, FAREWELL, GREETING, LISTENING, Persona
from jarvis.core.router import Dispatcher, PhraseResolver, Router
from jarvis.core.tools import ToolRegistry, collect_tools, tool
from jarvis.core.tts import NullTTS
from jarvis.core.voice import VoicePipeline


class Lights:
    """Скилл-заглушка для проверки маршрутизации."""

    @tool(phrases=["включи свет"])
    async def on(self) -> ToolResult:
        """Включить свет."""
        return ToolResult.success(True, speech="Свет включён.")


class RecordingTTS(NullTTS):
    """Заглушка синтеза, которая помнит всё сказанное."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []

    async def say(self, text: str, *, language: str | None = None) -> None:
        """Запомнить реплику вместо озвучки."""
        self.said.append(text)


def _pipeline(
    registry: ToolRegistry,
    events: LocalEventBus,
    *,
    persona: Persona | None = None,
    tts: NullTTS | None = None,
    **wake: object,
) -> VoicePipeline:
    """Собрать конвейер с заглушками вместо звука."""
    from jarvis.core.audio import NullAudioSink, NullAudioSource, PassthroughVAD
    from jarvis.core.audio.null import AlwaysActiveWakeWord
    from jarvis.core.stt import NullSTT

    config = AudioConfig(
        wake_word=WakeWordConfig(
            mode=str(wake.get("mode", "text")),
            phrases=tuple(wake.get("phrases", ("джарвис", "jarvis"))),
            aliases=tuple(wake.get("aliases", ("жарвис",))),
            similarity=float(wake.get("similarity", 0.7)),
            follow_up_s=float(wake.get("follow_up_s", 10.0)),
        )
    )
    router = Router([PhraseResolver(registry)], threshold=0.6)
    return VoicePipeline(
        source=NullAudioSource(),
        sink=NullAudioSink(),
        vad=PassthroughVAD(),
        wake_word=AlwaysActiveWakeWord("джарвис"),
        stt=NullSTT(),
        tts=tts or NullTTS(),
        dispatcher=Dispatcher(router=router, registry=registry),
        events=events,
        config=config,
        persona=persona,
    )


@pytest.fixture
def pipeline(registry: ToolRegistry, events: LocalEventBus) -> VoicePipeline:
    """Конвейер с одним зарегистрированным скиллом."""
    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)
    return _pipeline(registry, events)


def test_name_is_stripped_from_command(pipeline: VoicePipeline) -> None:
    """Имя отделяется от команды."""
    called, command = pipeline._strip_wake("Джарвис, включи свет")
    assert called
    assert command == "включи свет"


def test_alias_counts_as_name(pipeline: VoicePipeline) -> None:
    """Известный вариант ослышки засчитывается как обращение."""
    called, command = pipeline._strip_wake("жарвис включи свет")
    assert called
    assert command == "включи свет"


def test_unrelated_phrase_is_not_a_call(pipeline: VoicePipeline) -> None:
    """Обычная реплика не считается обращением, текст не портится."""
    called, command = pipeline._strip_wake("передай отвёртку")
    assert not called
    assert command == "передай отвёртку"


def test_command_without_name_ignored(pipeline: VoicePipeline) -> None:
    """Без обращения по имени команда не выполняется."""
    assert pipeline._extract_command("включи свет") is None


def test_bare_name_returns_empty_command(pipeline: VoicePipeline) -> None:
    """Голое «Джарвис» — это обращение без команды."""
    assert pipeline._extract_command("Джарвис") == ""


def test_follow_up_window_accepts_command_without_name(pipeline: VoicePipeline) -> None:
    """В окне ответа команда принимается без повторного обращения."""
    pipeline._follow_up_until = time.time() + 10
    assert pipeline._extract_command("включи свет") == "включи свет"


def test_follow_up_window_still_strips_name(pipeline: VoicePipeline) -> None:
    """В окне ответа имя всё равно вырезается, а не уезжает в роутер.

    Иначе «Джарвис» → «Джарвис, включи свет» приводило к тому, что роутер
    видел фразу вместе с именем и не находил команду.
    """
    pipeline._follow_up_until = time.time() + 10
    assert pipeline._extract_command("Джарвис, включи свет") == "включи свет"


async def test_handle_strips_name_for_text_input(pipeline: VoicePipeline) -> None:
    """Текстовая команда с именем маршрутизируется так же, как голосовая."""
    result = await pipeline.handle(Utterance(text="Джарвис, включи свет", source="text"))
    assert result.ok
    assert result.tool == "lights.on"


async def test_reply_lands_in_the_log(
    pipeline: VoicePipeline, caplog: pytest.LogCaptureFixture
) -> None:
    """Сказанное ассистентом видно в логе.

    Иначе по логу восстанавливается только команда и её результат, а
    произнесённая фраза — нет. Разбирать же приходится ровно расхождение
    между ними: «сказал, что включил» против «ничего не включилось».
    """
    with caplog.at_level("INFO", logger="jarvis.core.voice.pipeline"):
        await pipeline.handle(Utterance(text="Джарвис, включи свет", source="text"))

    assert "Отвечаю: Свет включён." in caplog.text


async def test_chat_never_pretends_to_act() -> None:
    """Свободный разговор обязан честно признаваться, что ничего не делает.

    Сюда попадают только реплики, которые не удалось выполнить командой. Модель
    же охотно отвечает «включаю видео» — и получается ассистент, который
    отчитывается о работе, которой не было. Поймано на живой демонстрации.
    """
    from jarvis.core.builtin import _DIALOG_SYSTEM

    assert "не отвечай «включаю»" in _DIALOG_SYSTEM["ru"]
    assert "no actions" in _DIALOG_SYSTEM["en"] or "perform no actions" in _DIALOG_SYSTEM["en"]


async def test_mode_none_reacts_without_name(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """При mode=none обращение по имени не требуется."""
    for item in collect_tools(Lights(), namespace="lights"):
        registry.register(item)
    pipeline = _pipeline(registry, events, mode="none")

    assert pipeline._extract_command("включи свет") == "включи свет"


# --- своя речь --------------------------------------------------------------


async def test_own_speech_is_not_captured(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Пока Jarvis говорит, микрофон не слушает.

    Без этого получается петля: колонки произносят ответ, микрофон его слышит,
    Whisper расшифровывает, и ассистент разбирает собственную реплику как
    команду.
    """
    import asyncio

    from jarvis.core.audio import AudioFrame

    pipeline = _pipeline(registry, events)

    class TalkingSource:
        """Источник, который отдаёт громкие кадры без остановки."""

        @property
        def service_name(self) -> str:
            return "fake"

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

        async def frames(self):
            while True:
                yield _frame(20000)
                await asyncio.sleep(0)

    pipeline._source = TalkingSource()
    pipeline._speaking = True

    task = asyncio.create_task(pipeline._listen())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert pipeline._pending.empty(), "своя речь не должна попадать на распознавание"


async def test_mute_released_after_speaking(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """После реплики микрофон глохнет ещё на хвост и потом включается."""
    pipeline = _pipeline(registry, events)
    assert not pipeline._muted

    await pipeline._say("Готово.")

    assert pipeline._muted, "сразу после речи микрофон ещё заглушен"
    pipeline._mute_until = 0.0
    assert not pipeline._muted


# --- VAD --------------------------------------------------------------------


def _frame(amplitude: int, samples: int = 480) -> AudioFrame:
    """Кадр с постоянной амплитудой."""
    import array

    data = array.array("h", [amplitude] * samples)
    return AudioFrame(data=data.tobytes(), sample_rate=16000)


def test_rms_scales_with_amplitude() -> None:
    """Громкость считается и нормируется к диапазону 0..1."""
    assert frame_rms(_frame(0)) == 0.0
    assert 0.0 < frame_rms(_frame(1000)) < frame_rms(_frame(10000)) < 1.0


def test_fixed_threshold_separates_speech_from_silence() -> None:
    """С заданным порогом тишина отсекается, речь проходит."""
    vad = EnergyVAD(threshold=0.05)

    assert not vad.is_speech(_frame(100))
    assert vad.is_speech(_frame(8000))


def test_calibration_learns_room_noise() -> None:
    """При нулевом пороге VAD замеряет фон и выставляет порог сам."""
    vad = EnergyVAD(threshold=0.0, calibrate_frames=10)

    for _ in range(10):
        assert not vad.is_speech(_frame(300)), "во время калибровки речи нет"

    assert vad.threshold > frame_rms(_frame(300))
    assert not vad.is_speech(_frame(300)), "фон не должен считаться речью"
    assert vad.is_speech(_frame(9000)), "речь громче фона — должна пройти"


# --- тайминги ---------------------------------------------------------------


def test_audio_config_derives_frame_math() -> None:
    """Производные величины считаются из частоты и длины кадра."""
    config = AudioConfig(sample_rate=16000, frame_ms=30, silence_ms=900, max_utterance_s=10.0)

    assert config.frame_bytes == 960
    assert config.silence_frames == 30
    assert config.max_utterance_bytes == 320000


# --- два языка --------------------------------------------------------------


def test_latin_name_recognised(pipeline: VoicePipeline) -> None:
    """Обращение «Jarvis» латиницей засчитывается наравне с «Джарвис».

    Нечёткое сравнение тут бессильно: у слов разные алфавиты и похожесть
    близка к нулю, поэтому сравнение идёт с каждым написанием отдельно.
    """
    called, command = pipeline._strip_wake("Jarvis, turn on the light")
    assert called
    assert command == "turn on the light"


def test_reply_follows_question_language(pipeline: VoicePipeline) -> None:
    """Реплика выбирается по языку вопроса."""
    result = ToolResult.success(True, speech={"ru": "Свет включён.", "en": "Light on."})

    assert result.speech_for("ru") == "Свет включён."
    assert result.speech_for("en") == "Light on."
    assert result.speech_for("en-US") == "Light on."


def test_single_string_speech_used_as_is() -> None:
    """Одна строка остаётся строкой: не всякая реплика нуждается в переводе."""
    result = ToolResult.success(1, speech="42")
    assert result.speech_for("en") == "42"


def test_unknown_language_falls_back() -> None:
    """Для языка без варианта берётся основной."""
    result = ToolResult.success(True, speech={"ru": "Готово.", "en": "Done."})
    assert result.speech_for("de") == "Готово."


def test_speech_may_be_several_variants() -> None:
    """Реплика бывает набором: выбирать из него — дело персоны, не инструмента."""
    result = ToolResult.success(
        True, speech={"ru": ("Пауза.", "Остановил."), "en": ("Paused.",)}
    )

    assert result.speech_options("ru") == ("Пауза.", "Остановил.")
    assert result.speech_options("en-GB") == ("Paused.",)
    # Без персоны берётся первый — так текстовый ввод и тесты остаются
    # предсказуемыми.
    assert result.speech_for("ru") == "Пауза."


def test_variants_without_languages() -> None:
    """Набор без языков — тоже набор, а строка не рассыпается на буквы."""
    assert ToolResult.success(1, speech=("Есть.", "Готово.")).speech_options("ru") == (
        "Есть.",
        "Готово.",
    )
    assert ToolResult.success(1, speech="Готово.").speech_options("ru") == ("Готово.",)
    assert ToolResult.success(1).speech_options("ru") == ()


async def test_pipeline_varies_skill_replies(pipeline: VoicePipeline) -> None:
    """Одна и та же команда не должна звучать одинаково два раза подряд."""
    said = [
        pipeline._persona.choose("page.pause", ("Пауза.", "Остановил.", "Тишина."))
        for _ in range(2)
    ]
    assert said[0] != said[1]


def test_voice_chosen_per_language() -> None:
    """Голос подбирается под язык, при отсутствии — язык по умолчанию."""
    from jarvis.core.config import TTSConfig

    config = TTSConfig(
        voices={"ru": "ru_RU-denis-medium", "en": "en_US-ryan-high"},
        default_language="ru",
    )

    assert config.voice_for("en") == ("en", "en_US-ryan-high")
    assert config.voice_for("en-US") == ("en", "en_US-ryan-high")
    assert config.voice_for("ru") == ("ru", "ru_RU-denis-medium")
    # Немецкого голоса нет — лучше прочитать русским, чем промолчать.
    assert config.voice_for("de") == ("ru", "ru_RU-denis-medium")


def test_language_detected_by_alphabet() -> None:
    """У текстового ввода язык определяется по алфавиту."""
    from jarvis.core.contracts import detect_language

    assert detect_language("какая температура") == "ru"
    assert detect_language("what's the temperature") == "en"
    assert detect_language("Джарвис, включи свет") == "ru"
    assert detect_language("Jarvis, turn on the light") == "en"
    # Смешанное: побеждает преобладающий алфавит.
    assert detect_language("открой OBS") == "ru"
    # Без букв — берётся значение по умолчанию.
    assert detect_language("42", default="en") == "en"


# --- окно ответа ------------------------------------------------------------


def test_follow_up_measured_from_speech_not_from_parsing(
    pipeline: VoicePipeline,
) -> None:
    """Окно отсчитывается от момента, когда фразу произнесли.

    Настоящий сбой: между речью и разбором лежит распознавание — несколько
    секунд. Если сверяться с часами после Whisper, окно успевает закрыться,
    пока фраза ещё расшифровывается, и ответ на «Слушаю» теряется.
    """
    now = time.time()
    pipeline._follow_up_until = now + 1.0

    # Фраза прозвучала внутри окна, а разбирается уже после его закрытия.
    assert pipeline._extract_command("включи свет", spoken_at=now) == "включи свет"
    assert pipeline._extract_command("включи свет", spoken_at=now + 5) is None


async def test_window_opens_after_the_reply_is_spoken(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Отсчёт начинается, когда «Слушаю» отзвучало и стих хвост.

    Иначе собственная реплика и распознавание съедают часть обещанного
    времени, и «шесть секунд» на деле оказываются короче.
    """
    import asyncio

    from jarvis.core.stt import Transcript

    class SlowNameSTT:
        """Слышит одно имя и делает это не мгновенно, как настоящий Whisper."""

        @property
        def service_name(self) -> str:
            return "fake-stt"

        @property
        def ready(self) -> bool:
            return True

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

        async def transcribe(self, audio: bytes, *, sample_rate: int) -> Transcript:
            await asyncio.sleep(0.05)
            return Transcript(text="Джарвис", language="ru", confidence=1.0)

    pipeline = _pipeline(registry, events, follow_up_s=6.0)
    pipeline._stt = SlowNameSTT()

    spoken_at = time.time()
    await pipeline._process(b"\x00" * 32000, spoken_at)

    # Окно открыто от «сейчас», а не от момента речи, случившегося раньше.
    assert pipeline._follow_up_until >= pipeline._mute_until + 6.0
    assert pipeline._follow_up_until > spoken_at + 6.0


# --- звук активации ---------------------------------------------------------


def test_silence_trimmed_from_sound() -> None:
    """Хвост тишины в файле — это время, когда микрофон уже не слушает.

    В activation.mp3 из корня проекта звук занимает 0.8 секунды из четырёх.
    """
    import array

    from jarvis.core.audio import trim_silence

    rate = 24000
    quiet = array.array("h", [0] * rate)          # секунда тишины
    loud = array.array("h", [8000, -8000] * 2400)  # 0.2 с звука
    audio = (quiet + loud + quiet).tobytes()

    trimmed = trim_silence(audio, rate)

    # Остаётся сам звук плюс небольшой запас по краям, но не секунды тишины.
    assert 0.2 <= len(trimmed) / 2 / rate <= 0.4


def test_pure_silence_gives_nothing() -> None:
    """Файл из одной тишины воспроизводить нечего."""
    import array

    from jarvis.core.audio import trim_silence

    assert trim_silence(array.array("h", [0] * 24000).tobytes(), 24000) == b""


def test_missing_sound_file_is_not_an_error(tmp_path) -> None:
    """Отсутствие звука не должно мешать запуску: он необязателен."""
    from jarvis.core.audio import load_sound

    assert load_sound(tmp_path / "нет-такого.mp3") is None


# --- манера речи ------------------------------------------------------------


class NameSTT:
    """Слышит только имя — как будто позвали и замолчали."""

    @property
    def service_name(self) -> str:
        return "fake-stt"

    @property
    def ready(self) -> bool:
        return True

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def transcribe(self, audio: bytes, *, sample_rate: int):
        from jarvis.core.stt import Transcript

        return Transcript(text="Джарвис", language="ru", confidence=1.0)


async def test_bare_name_answered_from_persona(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Отклик на имя берётся из набора персоны, а не из зашитой строки."""
    tts = RecordingTTS()
    persona = Persona()
    pipeline = _pipeline(registry, events, persona=persona, tts=tts)
    pipeline._stt = NameSTT()

    await pipeline._process(b"\x00" * 32000, time.time())

    pool = {line.format(address="сэр") for line in persona.variants(LISTENING, "ru")}
    assert tts.said == [tts.said[0]] and tts.said[0] in pool


async def test_repeated_calls_are_answered_differently(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Позвали трижды — услышали три разных отклика.

    Ради этого всё и затевалось: одна фраза на каждое обращение за неделю
    перестаёт восприниматься как ответ.
    """
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts)
    pipeline._stt = NameSTT()

    for _ in range(3):
        await pipeline._process(b"\x00" * 32000, time.time())

    assert len(set(tts.said)) == 3


async def test_greeting_depends_on_the_time_of_day(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Приветствие начинается с «Доброе утро» или «Добрый вечер»."""
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts)

    await pipeline.announce(GREETING)
    await pipeline.announce(FAREWELL)

    assert tts.said[0].startswith(("Доброе", "Добрый", "Доброй"))
    assert len(tts.said) == 2


async def test_start_and_stop_stay_silent(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Подъём сервиса ничего не произносит.

    Иначе `--check` и одиночная команда `--say` каждый раз здоровались бы и
    прощались: две лишних реплики на односекундную операцию.
    """
    tts = RecordingTTS()
    pipeline = _pipeline(registry, events, tts=tts)

    await pipeline.start()
    await pipeline.stop()

    assert tts.said == []


def test_silent_result_is_described_in_character(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """Команда выполнилась молча — отвечает персона, а не «Готово.»."""
    persona = Persona()
    pipeline = _pipeline(registry, events, persona=persona)

    reply = pipeline._describe(ToolResult.success(None), "ru")
    assert reply in {line.format(address="сэр") for line in persona.variants(DONE, "ru")}


def test_own_error_text_beats_the_polite_refusal(
    registry: ToolRegistry, events: LocalEventBus
) -> None:
    """«Не нашёл такой программы» полезнее, чем «Не вышло, сэр»."""
    persona = Persona()
    pipeline = _pipeline(registry, events, persona=persona)

    assert pipeline._describe(ToolResult.failure("Не нашёл такую программу.")) == (
        "Не нашёл такую программу."
    )
    fallback = pipeline._describe(ToolResult.failure(""), "ru")
    assert fallback in {
        line.format(address="сэр") for line in persona.variants(FAILED, "ru")
    }


async def test_out_of_memory_explains_itself() -> None:
    """Не хватило памяти на модель — человеку нужен ответ, а не стек.

    Живой случай: на ноуте владельца свободным остался гигабайт, Whisper не
    загрузился, и в консоль уехало два трейсбека ctranslate2. Первый вопрос
    после такого — «это я что-то сломал?».
    """
    from jarvis.core.config import STTConfig
    from jarvis.core.errors import STTError
    from jarvis.core.stt.faster_whisper import FasterWhisperSTT, out_of_memory

    assert out_of_memory(RuntimeError("mkl_malloc: failed to allocate memory"))
    assert out_of_memory(MemoryError())
    # Чужая ошибка так не подписывается — её глушить нельзя.
    assert not out_of_memory(RuntimeError("model not found"))

    class Boom:
        """Пул задач, в котором загрузка падает от нехватки памяти."""

        async def run(self, call, *args):
            raise RuntimeError("mkl_malloc: failed to allocate memory")

    stt = FasterWhisperSTT(STTConfig(model="small"), Boom())  # type: ignore[arg-type]

    with pytest.raises(STTError) as failure:
        await stt.start()

    said = str(failure.value)
    assert "памяти" in said and "stt.model: base" in said, said


async def test_foreign_error_keeps_its_stack() -> None:
    """А чужая ошибка пробрасывается как есть: по ней нужен стек."""
    from jarvis.core.config import STTConfig
    from jarvis.core.stt.faster_whisper import FasterWhisperSTT

    class Broken:
        async def run(self, call, *args):
            raise RuntimeError("model not found")

    stt = FasterWhisperSTT(STTConfig(), Broken())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="model not found"):
        await stt.start()


# --- что поднимается в каком режиме ------------------------------------------


class _Counted:
    """Сервис, который только запоминает, что его подняли."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.started = 0

    @property
    def service_name(self) -> str:
        return self._name

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass


async def test_services_are_started_by_purpose() -> None:
    """Уши и голос поднимаются отдельно, потому что нужны не всем режимам.

    Одного флага «тяжёлый» не хватало: `--say` получает команду текстом, и
    Whisper ему не нужен — а грузился он полторы минуты вместе с микрофоном.
    """
    from jarvis.core.lifecycle import EARS, VOICE, ServiceRunner

    always, ears, voice = _Counted("шина"), _Counted("whisper"), _Counted("голос")
    runner = ServiceRunner()
    runner.add(always)
    runner.add(ears, needs=EARS)
    runner.add(voice, needs=VOICE)

    await runner.start_all(without={EARS})

    assert (always.started, ears.started, voice.started) == (1, 0, 1)
    await runner.stop_all()


async def test_check_starts_neither_half() -> None:
    """Отчёту о сборке не нужны ни уши, ни голос: ему важен каталог."""
    from jarvis.core.lifecycle import EARS, VOICE, ServiceRunner

    ears, voice = _Counted("whisper"), _Counted("голос")
    runner = ServiceRunner()
    runner.add(ears, needs=EARS)
    runner.add(voice, needs=VOICE)

    await runner.start_all(without={EARS, VOICE})

    assert (ears.started, voice.started) == (0, 0)
    await runner.stop_all()


# --- Silero VAD -------------------------------------------------------------


class _FakeInput:
    """Описание входа модели: у настоящего интерфейса важно только имя."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSession:
    """Модель, которая отдаёт заранее заданные вероятности речи."""

    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = probabilities
        self.calls = 0

    def get_inputs(self) -> list[_FakeInput]:
        """Пятая версия Silero: звук, состояние и частота."""
        return [_FakeInput("input"), _FakeInput("state"), _FakeInput("sr")]

    def run(self, _outputs, feed):  # type: ignore[no-untyped-def]
        """Вернуть очередную вероятность и то же состояние."""
        assert feed["input"].shape == (1, 512), "модель принимает ровно 512 сэмплов"
        index = min(self.calls, len(self._probabilities) - 1)
        self.calls += 1
        return [[[self._probabilities[index]]], feed["state"]]


def _silero(monkeypatch, tmp_path, probabilities: list[float], threshold: float = 0.5):
    """Собрать SileroVAD на подставной модели."""
    import sys
    import types

    session = _FakeSession(probabilities)
    fake = types.ModuleType("onnxruntime")
    fake.SessionOptions = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        inter_op_num_threads=0, intra_op_num_threads=0
    )
    fake.InferenceSession = lambda *a, **kw: session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)

    model = tmp_path / "silero_vad.onnx"
    model.write_bytes(b"")
    return SileroVAD(model, sample_rate=16000, threshold=threshold), session


def test_silero_regroups_frames_into_its_own_chunks(monkeypatch, tmp_path) -> None:
    """Кадр 30 мс — это 480 сэмплов, а модель берёт 512.

    Пересборка живёт внутри детектора: `audio.frame_ms` — настройка захвата, и
    подгонять её под чужую модель неправильно. Побочное следствие: часть кадров
    вывода не даёт, и ответ на них — прежнее решение.
    """
    vad, session = _silero(monkeypatch, tmp_path, [0.9])

    assert not vad.is_speech(_frame(3000)), "480 сэмплов модели ещё мало"
    assert session.calls == 0
    assert vad.is_speech(_frame(3000)), "960 сэмплов — есть полный кусок"
    assert session.calls == 1


def _chunk_frame() -> AudioFrame:
    """Кадр ровно в один кусок модели — чтобы кадры и вероятности совпадали."""
    import array

    return AudioFrame(data=array.array("h", [3000] * 512).tobytes(), sample_rate=16000)


def test_silero_holds_speech_through_short_pauses(monkeypatch, tmp_path) -> None:
    """Гистерезис: пауза между словами не считается концом фразы.

    Без него одна фраза разваливалась бы на куски по числу вдохов, и каждый
    кусок уезжал бы в Whisper отдельно.
    """
    vad, _ = _silero(monkeypatch, tmp_path, [0.9, 0.4, 0.1], threshold=0.5)

    frames = [vad.is_speech(_chunk_frame()) for _ in range(3)]

    assert frames[0], "громкая уверенность — речь началась"
    assert frames[1], "0.4 ниже порога, но выше отпускания — держим"
    assert not frames[2], "0.1 — речь кончилась"


def test_silero_forgets_everything_between_phrases(monkeypatch, tmp_path) -> None:
    """После фразы состояние сети и недобранный хвост сбрасываются."""
    vad, session = _silero(monkeypatch, tmp_path, [0.9])

    vad.is_speech(_frame(3000))
    vad.reset()

    assert not vad.is_speech(_frame(3000)), "хвост прошлой фразы не копится"
    assert session.calls == 0


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "models/vad/silero_vad.onnx").is_file(),
    reason="модель Silero VAD не скачана",
)
def test_silero_does_not_hear_speech_in_noise() -> None:
    """Главное отличие от энергетического: громкое ещё не значит «речь».

    Белый шум такой громкости энергетический пропускает как речь — именно так
    музыка и набивала очередь распознавания. Тест идёт на настоящей модели,
    поэтому пропускается, пока её не скачали.
    """
    import array
    import random

    model = Path(__file__).resolve().parent.parent / "models/vad/silero_vad.onnx"
    silero = SileroVAD(model, sample_rate=16000)
    energy = EnergyVAD(threshold=0.05)
    random.seed(1)

    def noise() -> AudioFrame:
        data = array.array("h", [random.randint(-6000, 6000) for _ in range(480)])
        return AudioFrame(data=data.tobytes(), sample_rate=16000)

    frames = [noise() for _ in range(40)]

    assert any(energy.is_speech(f) for f in frames), "громкость шума высокая"
    assert not any(silero.is_speech(f) for f in frames), "но речи в нём нет"
