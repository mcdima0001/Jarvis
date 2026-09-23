"""Распознавание с запасным путём: облако, а без сети — местная модель.

Облако быстрее местной модели на порядок и не занимает процессор, но живёт в
интернете, и интернет когда-нибудь кончится. Ассистент, который в этот момент
перестаёт слышать вовсе, — плохой ассистент: команды «выключи музыку» и
«заблокируй компьютер» интернета не требуют по своей сути.

**Местная модель поднимается лениво, при первом же отказе облака.** Держать её
загруженной всегда было бы проще, но тогда теряется ровно то, ради чего облако и
затевалось: Whisper занимает полгигабайта и оба ядра. Плата за это — первая
фраза после обрыва: пока модель грузится (полминуты), ответить нечем. Так честнее,
чем платить памятью каждую секунду за случай, который бывает раз в месяц.

**Обрыв определяется по отказу запроса, а не опросом сети.** Пинг врёт в обе
стороны: сеть бывает жива, когда сервис лежит, и наоборот. Единственный надёжный
признак — то, что запрос не прошёл.

**После отказа облако не спрашивают некоторое время.** Иначе каждая фраза
начиналась бы с ожидания таймаута, и обрыв связи превращался бы в «ассистент
задумывается на пять секунд перед каждым ответом».

**Оба слушают наперегонки** (замысел владельца 23.09.2026: «пусть в два потока
слушает: если Deepgram ответил — опираемся на него, а если Whisper первее,
слышим его»). Жалоба была ровно на это: без интернета ассистент «очень долго
думает», потому что сперва целиком выжидался отказ облака и только потом
начиналась местная работа.

Местная модель при этом стартует **не сразу, а если облако задержалось**
(`race_after_s`, по умолчанию полторы секунды при медиане ответа Deepgram 1.2 с).
Разница в цене велика: Whisper на этой машине идёт медленнее реального времени
и занимает оба ядра, и гонять его на каждой фразе при живом интернете — значит
греть ноутбук ради случая, которого нет. При живой сети он не стартует вовсе, при
мёртвой — начинает через полторы секунды вместо минуты. Ноль в настройке означает
буквально «оба сразу», как и было сказано.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from jarvis.core.errors import STTError

from .protocol import STT, Transcript
from .stream import STTStream

logger = logging.getLogger(__name__)

#: Сколько не трогать облако после отказа, прежде чем попробовать снова.
#: Минута — компромисс: быстрее значит платить таймаутом почти на каждой фразе,
#: дольше — сидеть на медленной модели, когда сеть уже вернулась.
RETRY_AFTER_S = 60.0

#: Сколько ждать облако, прежде чем будить местную модель. Ноль — будить сразу.
RACE_AFTER_S = 1.5


class FallbackSTT:
    """Основной распознаватель с подстраховкой на случай отказа."""

    def __init__(
        self,
        primary: STT,
        backup: STT,
        *,
        retry_after_s: float = RETRY_AFTER_S,
        race_after_s: float = RACE_AFTER_S,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self._retry_after = retry_after_s
        self._race_after = max(0.0, race_after_s)
        #: До какого момента не трогать основной путь.
        self._blocked_until = 0.0
        #: Поднимали ли уже запасной. Он тяжёлый, и поднимается один раз.
        self._backup_ready = False
        #: Кого известить, когда облако отказало (раз на обрыв). Ставит сборка
        #: приложения: живой запуск 16.09.2026 молча ждал 40 секунд Whisper.
        self.on_outage: Callable[[], None] | None = None
        self._outage = False

    @property
    def service_name(self) -> str:
        """Имя сервиса для логов."""
        return "stt"

    @property
    def ready(self) -> bool:
        """Готов ли хоть один из двух."""
        return self._primary.ready or self._backup_ready

    async def start(self) -> None:
        """Поднять основной путь. Запасной ждёт своего часа."""
        await self._primary.start()

    async def stop(self) -> None:
        """Погасить оба, если поднимались."""
        await self._primary.stop()
        if self._backup_ready:
            await self._backup.stop()
            self._backup_ready = False

    async def _wake_backup(self) -> None:
        """Поднять местную модель — впервые и надолго."""
        if self._backup_ready:
            return
        logger.warning(
            "Перехожу на местное распознавание — это займёт полминуты, "
            "модель поднимается впервые"
        )
        await self._backup.start()
        self._backup_ready = True

    def open_stream(self, *, sample_rate: int = 16000) -> STTStream | None:
        """Поток — только у основного и только пока он не в блокировке после отказа.

        Иначе каждая фраза после обрыва связи начиналась бы с ожидания таймаута
        потока — ровно то, от чего блокировка и заведена.
        """
        if time.monotonic() < self._blocked_until:
            return None
        opener = getattr(self._primary, "open_stream", None)
        return opener(sample_rate=sample_rate) if callable(opener) else None

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Распознать: облако и местная модель наперегонки.

        Побеждает облако, если ответило: оно точнее и знает подсказанные слова.
        Но ждать его до победного нельзя — без сети это и есть те самые «очень
        долго думает», на которые жаловался владелец.
        """
        if time.monotonic() < self._blocked_until:
            # Облако только что отказало: спрашивать снова — значит платить
            # таймаутом на каждой фразе. Слушаем местной моделью.
            return await self._local(audio, sample_rate)

        if self._blocked_until:
            logger.info("Пробую снова основное распознавание")
        cloud = asyncio.create_task(
            self._primary.transcribe(audio, sample_rate=sample_rate), name="stt-cloud"
        )
        local: asyncio.Task[Transcript] | None = None
        try:
            while True:
                if local is None:
                    # Ждём облако столько, сколько оно обычно и отвечает; не
                    # ответило — будим местную модель, и дальше кто первый.
                    done, _ = await asyncio.wait({cloud}, timeout=self._race_after)
                    if not done:
                        local = asyncio.create_task(
                            self._local(audio, sample_rate), name="stt-local"
                        )
                        continue
                else:
                    await asyncio.wait({cloud, local}, return_when=asyncio.FIRST_COMPLETED)

                if cloud.done():
                    try:
                        result = cloud.result()
                    except STTError as exc:
                        self._note_outage(exc)
                        return await (local if local is not None else self._local(audio, sample_rate))
                    if self._outage:
                        logger.info("Основное распознавание снова работает")
                        self._outage = False
                    return result

                assert local is not None  # иначе ждать было бы нечего
                try:
                    heard = local.result()
                except Exception as exc:  # noqa: BLE001 — местная модель тоже падает
                    logger.warning("Местное распознавание не справилось: %s", exc)
                    local = None
                    # Облако осталось единственным — ждём его, сколько понадобится.
                    return await cloud
                logger.info("Местная модель ответила раньше облака")
                return heard
        finally:
            # Проигравшего не держим: запрос в облако отменяется бесплатно, а
            # местная модель доработает в своём потоке — ждать её уже некому.
            for task in (cloud, local):
                if task is not None and not task.done():
                    task.cancel()

    def _note_outage(self, exc: BaseException) -> None:
        """Запомнить отказ облака и сказать о нём — один раз на обрыв."""
        self._blocked_until = time.monotonic() + self._retry_after
        logger.warning(
            "Основное распознавание отказало (%s) — перехожу на запасное на %.0f с",
            exc,
            self._retry_after,
        )
        if not self._outage:
            self._outage = True
            if self.on_outage is not None:
                self.on_outage()

    async def _local(self, audio: bytes, sample_rate: int) -> Transcript:
        """Местная модель: поднять, если ещё не поднята, и распознать."""
        await self._wake_backup()
        return await self._backup.transcribe(audio, sample_rate=sample_rate)
