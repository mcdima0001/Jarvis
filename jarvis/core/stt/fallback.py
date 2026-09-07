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
"""

from __future__ import annotations

import logging
import time

from jarvis.core.errors import STTError

from .protocol import STT, Transcript

logger = logging.getLogger(__name__)

#: Сколько не трогать облако после отказа, прежде чем попробовать снова.
#: Минута — компромисс: быстрее значит платить таймаутом почти на каждой фразе,
#: дольше — сидеть на медленной модели, когда сеть уже вернулась.
RETRY_AFTER_S = 60.0


class FallbackSTT:
    """Основной распознаватель с подстраховкой на случай отказа."""

    def __init__(
        self,
        primary: STT,
        backup: STT,
        *,
        retry_after_s: float = RETRY_AFTER_S,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self._retry_after = retry_after_s
        #: До какого момента не трогать основной путь.
        self._blocked_until = 0.0
        #: Поднимали ли уже запасной. Он тяжёлый, и поднимается один раз.
        self._backup_ready = False

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

    async def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> Transcript:
        """Распознать: сперва основным путём, при отказе — запасным."""
        now = time.monotonic()
        if now >= self._blocked_until:
            if self._blocked_until:
                logger.info("Пробую снова основное распознавание")
            try:
                return await self._primary.transcribe(audio, sample_rate=sample_rate)
            except STTError as exc:
                self._blocked_until = now + self._retry_after
                logger.warning(
                    "Основное распознавание отказало (%s) — перехожу на запасное "
                    "на %.0f с",
                    exc,
                    self._retry_after,
                )

        await self._wake_backup()
        return await self._backup.transcribe(audio, sample_rate=sample_rate)
