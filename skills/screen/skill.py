"""Зрение: снимок экрана и вопрос к зрячей модели.

До этого скилла всё, что Jarvis знал о происходящем, приходило через
инструменты, написанные заранее под конкретный случай. Зрение тем и ценно, что
не требует придумывать сценарии вперёд: «какая там ошибка», «что это за окно»,
«прочитай, что написано» — один инструмент на целый класс вопросов, которых
иначе просто не существовало бы.

**Снимок уходит в чужое облако, и это не мелочь.** На экране бывает переписка,
открытый пароль, ключ в терминале. Поэтому правило жёсткое: снимок делается
**только по прямой команде**, никогда сам и никогда в фоне, нигде не сохраняется
и живёт в памяти ровно на время запроса. Факт отправки пишется в лог — чтобы
всегда можно было посмотреть, что и когда уходило.

**Зависимостей не добавляет.** Pillow умеет снимать экран сам
(`ImageGrab.grab`), а прямоугольник окна и монитора берётся через ctypes тем же
приёмом, каким скилл `windows` уже перечисляет окна.

**Что здесь чистые функции, а что нет.** Экрана на сервере нет, поэтому логика
разложена как в `windows`: выбор области, сжатие, кодирование и сборка запроса
живут отдельными функциями под тестами, а сам захват остаётся тонкой обёрткой.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import sys
import time
from pathlib import Path
from typing import Any

from jarvis.core.contracts import ToolResult, detect_language
from jarvis.core.errors import LLMError, LLMNotConfigured
from jarvis.core.llm import Message
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool

#: Прямоугольник на виртуальном рабочем столе: слева, сверху, справа, снизу.
Box = tuple[int, int, int, int]

#: Профиль задачи в конфиге. Модель обязана уметь картинки, а это не та же
#: модель, что разбирает команды, — поэтому профиль свой.
VISION_TASK = "vision"

#: Предел длинной стороны снимка в пикселях.
#:
#: **Сжатие тут страховка, а не экономия**, и это ровно наоборот тому, с чего
#: скилл начинался. Замер (11.09.2026, `gemini-2.5-flash`) показал, что цена
#: картинки растёт не плавно, а ступенями, и на всём участке от 768 до 2200
#: пикселей она не меняется вовсе — 1879 входных токенов и там, и там:
#:
#:     256 -> 331   512 -> 1363   768..2200 -> 1879   2400 -> 2395   3200 -> 3427
#:
#: Зато читаемость от сжатия страдает по-настоящему. Тот же снимок, ужатый до
#: 768, дал **уверенно неверный** ответ: «KeyError: 'ru'» вместо «'de'». Это
#: худший исход из возможных — не отказ, который слышно, а ошибка, которую не
#: отличить от правды.
#:
#: Отсюда предел ровно в родную ширину экрана: обычный снимок не пересчитывается
#: вовсе, текст остаётся резким и не мылится ресемплингом. Ужимается только то,
#: что заметно больше, — 4K и панорамы из нескольких мониторов.
LIMIT = 1920

#: PNG, а не JPEG, и это выбор, а не привычка. Читать с экрана чаще всего надо
#: **текст**, а JPEG портит именно мелкие буквы — то есть экономил бы на том,
#: ради чего зрение и заводилось.
FORMAT = "PNG"

#: Больше этого снимок в запрос не пустим. Не ограничение модели, а страховка от
#: нелепого: четыре монитора в одном кадре всё равно нечитаемы после сжатия, а
#: заплатить за них пришлось бы полностью.
MAX_BYTES = 4 * 1024 * 1024

#: Что снимать. Названия приходят от модели аргументом, поэтому короткие.
TARGETS = ("screen", "window", "all")

_SYSTEM = {
    "ru": (
        "Ты смотришь на снимок экрана и отвечаешь на вопрос о том, что на нём "
        "видно. Отвечай коротко, одним-двумя предложениями: ответ произносится "
        "вслух. Если на экране текст ошибки или сообщение, приведи его дословно. "
        "Не описывай оформление и расположение окон, если об этом не спросили."
    ),
    "en": (
        "You are looking at a screenshot and answering a question about it. "
        "Answer in one or two sentences: the answer is read aloud. If there is "
        "an error message on screen, quote it verbatim. Do not describe the "
        "layout or styling unless asked."
    ),
}

_DEFAULT_QUESTION = {
    "ru": "Что сейчас на экране?",
    "en": "What is on the screen right now?",
}

_NO_VISION = {
    "ru": "Не могу посмотреть на экран: зрение не настроено.",
    "en": "I cannot look at the screen: vision is not configured.",
}

_NO_SCREEN = {
    "ru": "Не получилось снять экран.",
    "en": "Could not capture the screen.",
}


# --- чистые функции: считаются одинаково на любой машине --------------------


def shrink(size: tuple[int, int], *, limit: int = LIMIT) -> tuple[int, int]:
    """Во сколько ужать снимок, чтобы длинная сторона влезла в предел.

    Маленькое не растягивается: увеличение не добавляет разборчивости, а платить
    за лишние пиксели пришлось бы по полной.
    """
    width, height = size
    if width <= 0 or height <= 0:
        return (0, 0)
    longest = max(width, height)
    if longest <= limit:
        return (width, height)
    scale = limit / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def to_data_uri(image: bytes, *, kind: str = "png") -> str:
    """Картинка в виде ``data:``-URI, как её ждёт протокол."""
    return f"data:image/{kind};base64,{base64.b64encode(image).decode('ascii')}"


def question_for(question: str, language: str) -> str:
    """О чём спрашивать модель: сказанное вслух либо описание по умолчанию."""
    asked = question.strip()
    if asked:
        return asked
    return _DEFAULT_QUESTION.get(language, _DEFAULT_QUESTION["ru"])


def build_messages(question: str, image: str, language: str) -> list[Message]:
    """Собрать запрос к зрячей модели.

    Вопрос и картинка идут **одной репликой пользователя**: они про одно и то же,
    и разносить их по сообщениям значит заставлять модель догадываться, к чему
    относится вопрос.
    """
    return [
        Message.system(_SYSTEM.get(language, _SYSTEM["ru"])),
        Message.user(question, images=(image,)),
    ]


#: Как называют номер монитора вслух. Падежи нужны все: номер приходит прямо из
#: речи — «что на втором мониторе», «покажи второй».
_ORDINALS: dict[str, int] = {
    "первый": 1, "первом": 1, "первого": 1, "первому": 1, "first": 1,
    "второй": 2, "втором": 2, "второго": 2, "вторым": 2, "second": 2,
    "третий": 3, "третьем": 3, "третьего": 3, "третьим": 3, "third": 3,
    "четвёртый": 4, "четвёртом": 4, "четвертый": 4, "четвертом": 4, "fourth": 4,
}

#: Слова названия: «2-ом» и «второй» одинаково годятся, а разделители — нет.
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def monitor_number(text: str) -> int:
    """Какой монитор назвали. Ноль — номера в речи не было.

    Разбирается и цифрой, и словом: распознавание пишет «2-ом мониторе» и
    «втором мониторе» вперемешку, причём в одном и том же разговоре.
    """
    for word in _WORDS.findall(text.strip().lower()):
        if word in _ORDINALS:
            return _ORDINALS[word]
        if word.isdigit():
            number = int(word)
            # Двузначные — это не номер монитора, а что-то из соседней фразы.
            if 1 <= number <= 9:
                return number
    return 0


def normalize_target(target: str) -> str:
    """Привести к известному значению: область либо номер монитора строкой.

    Значение приходит от модели, а она склонна изобретать синонимы. Падать из-за
    «monitor» вместо «screen» было бы обидно.
    """
    value = target.strip().lower()
    if value in TARGETS:
        return value
    if value in {"окно", "window", "active", "foreground"}:
        return "window"
    if value in {"всё", "все", "everything", "desktop", "virtual", "мониторы"}:
        return "all"
    number = monitor_number(value)
    if number:
        return str(number)
    return "screen"


def describe(size: tuple[int, int], payload: int) -> str:
    """Строка для лога: что именно уходит в облако."""
    return f"{size[0]}x{size[1]}, {payload // 1024} КБ"


# --- то, что умеет только Windows -------------------------------------------


def claim_dpi_awareness() -> bool:
    """Сказать Windows, что координаты нужны в настоящих пикселях.

    Без этого система врёт масштабированному процессу: `GetWindowRect` вернёт
    логические координаты, а снимок делается в физических, и на экране со
    масштабом 150% вырезался бы не тот кусок. У Jarvis своего окна нет, так что
    объявить осведомлённость безопасно.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2. Повторный вызов возвращает отказ,
        # и это нормально: значит осведомлённость уже объявлена.
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return True
    except (OSError, AttributeError):
        return False


def monitors() -> list[Box]:
    """Все мониторы, слева направо.

    **Порядок именно по расположению, а не по тому, в каком система их
    перечислила.** «Второй монитор» человек считает глазами: тот, что правее.
    Системный порядок зависит от того, в каком гнезде кабель, и с видом на стол
    не связан никак.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[Box] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    def visit(monitor: int, dc: int, rect: Any, data: int) -> bool:
        """Запомнить очередной монитор."""
        area = rect.contents
        found.append((area.left, area.top, area.right, area.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, visit, 0)
    return sorted(found, key=lambda box: (box[0], box[1]))


def region(target: str) -> Box | None:
    """Какой прямоугольник снимать. ``None`` — весь виртуальный стол."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    if target.isdigit():
        screens = monitors()
        index = int(target) - 1
        # Выход за список сюда не доходит: инструмент проверяет номер раньше и
        # честно говорит, сколько мониторов есть. Здесь только страховка.
        return screens[index] if 0 <= index < len(screens) else None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handle = user32.GetForegroundWindow()
    if not handle:
        return None

    if target == "window":
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    if target == "screen":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        monitor = user32.MonitorFromWindow(wintypes.HWND(handle), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        area = info.rcMonitor
        return (area.left, area.top, area.right, area.bottom)

    return None


def capture(target: str, *, limit: int = LIMIT) -> tuple[bytes, tuple[int, int]]:
    """Снять экран и вернуть готовый PNG вместе с итоговым размером.

    Синхронная и небыстрая: зовётся только через `asyncio.to_thread`, иначе
    сотня миллисекунд заморозила бы голосовой конвейер вместе с микрофоном.
    """
    from PIL import ImageGrab

    box = region(target)
    image = ImageGrab.grab(bbox=box, all_screens=True)
    size = shrink(image.size, limit=limit)
    if size != image.size:
        from PIL import Image

        image = image.resize(size, Image.LANCZOS)
    # Снимок приходит с альфа-каналом, который PNG честно сохранит, а пользы от
    # него ноль: экран непрозрачен.
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format=FORMAT, optimize=True)
    return buffer.getvalue(), image.size


class ScreenSkill(Skill):
    """Глаза: посмотреть на экран и ответить, что там."""

    meta = SkillMeta(
        name="screen",
        description="Зрение: смотрит на экран и отвечает на вопросы о том, что видно",
        version="0.1.0",
        platforms=("windows",),
        spoken=("экран", "зрение", "screen", "vision"),
    )

    async def on_setup(self) -> None:
        """Объявить осведомлённость о масштабе, пока ничего не снимали."""
        if claim_dpi_awareness():
            self.context.logger.debug("Координаты экрана — в физических пикселях")

    async def health(self) -> HealthStatus:
        """Готово ли зрение: есть Pillow, профиль и настроенная модель."""
        try:
            from PIL import ImageGrab  # noqa: F401  # проверяем наличие, не зовём
        except ImportError:
            return HealthStatus.degraded("нет Pillow: pip install pillow")
        if not self.context.llm.available:
            return HealthStatus.degraded("модель не настроена: нет ключа")
        try:
            self.context.llm.profiles.get(VISION_TASK)
        except LLMNotConfigured:
            return HealthStatus.degraded(
                f"нет профиля {VISION_TASK!r} в llm.profiles конфига"
            )
        return HealthStatus.healthy()

    @tool(
        phrases=[
            "что на экране",
            "что сейчас на экране",
            "что там на экране",
            "посмотри на экран",
            "глянь на экран",
            "посмотри на экран и скажи {question}",
            "что на экране {question}",
            "что на {target} мониторе",
            "что на {target} экране",
            "посмотри на {target} монитор",
            "покажи {target} монитор",
            "what is on the screen",
            "what's on my screen",
            "look at the screen",
        ],
        reversible=False,
    )
    async def look(self, question: str = "", target: str = "screen") -> ToolResult:
        """Посмотреть на экран и ответить на вопрос о том, что там видно.

        Годится на всё, что человек видит глазами: текст ошибки, содержимое
        окна, что за программа открыта, что написано в сообщении.

        :param question: о чём спросить; пусто — просто описать экран.
        :param target: что снимать. ``screen`` — монитор с активным окном,
            ``window`` — только активное окно, ``all`` — все мониторы сразу,
            номер (``1``, ``2``, ``3``) — конкретный монитор слева направо.
        """
        language = detect_language(question, default="ru")
        asked = question_for(question, language)
        where = normalize_target(target)

        if where.isdigit():
            # Номер проверяется до снимка: сказать «у тебя один монитор» честнее,
            # чем молча показать не тот. Первое человек поправит, второго не
            # заметит.
            count = len(await asyncio.to_thread(monitors))
            if int(where) > count:
                return ToolResult.failure(
                    f"монитора {where} нет, всего {count}",
                    speech={
                        "ru": f"Столько мониторов нет, их {count}.",
                        "en": f"There is no such monitor, you have {count}.",
                    },
                )

        try:
            payload, size = await asyncio.to_thread(capture, where)
        except Exception as exc:  # снять экран может помешать что угодно
            self.context.logger.warning("Снимок экрана не удался: %s", exc)
            return ToolResult.failure(f"снимок экрана не удался: {exc}", speech=_NO_SCREEN)

        if len(payload) > MAX_BYTES:
            self.context.logger.warning(
                "Снимок слишком велик (%s), в модель не отправляю",
                describe(size, len(payload)),
            )
            return ToolResult.failure("снимок слишком велик", speech=_NO_SCREEN)

        # Обещание из шапки модуля: каждый уход картинки наружу виден в логе.
        self.context.logger.info(
            "Снимок экрана уходит в облако: %s, вопрос: %r",
            describe(size, len(payload)),
            asked,
        )

        messages = build_messages(asked, to_data_uri(payload), language)
        try:
            response = await self.context.llm.complete(messages, task=VISION_TASK)
        except (LLMError, LLMNotConfigured) as exc:
            self.context.logger.warning("Зрячая модель не ответила: %s", exc)
            return ToolResult.failure(f"модель не ответила: {exc}", speech=_NO_VISION)

        answer = response.text.strip()
        if not answer:
            return ToolResult.failure("пустой ответ модели", speech=_NO_VISION)
        return ToolResult.success(answer, speech=answer)

    @tool(routable=False, reversible=False)
    async def snapshot(self, path: str = "", target: str = "screen") -> ToolResult:
        """Сохранить снимок экрана в файл.

        Служебное: отладка и будущий агентный цикл, которому надо посмотреть на
        результат собственного действия. Голосом не зовут, поэтому в каталог
        модели не идёт — каталог оплачивается в каждом неузнанном запросе.

        :param path: куда сохранить; пусто — в ``models/screenshots``.
        :param target: что снимать, как у `look`.
        """
        try:
            payload, size = await asyncio.to_thread(capture, normalize_target(target))
        except Exception as exc:  # снять экран может помешать что угодно
            return ToolResult.failure(f"снимок экрана не удался: {exc}")

        destination = self._destination(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, payload)
        return ToolResult.success(
            {"path": str(destination), "size": list(size), "bytes": len(payload)}
        )

    def _destination(self, path: str) -> Path:
        """Куда класть снимок: явный путь либо каталог по умолчанию."""
        if path.strip():
            return Path(path)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self.context.root / "models" / "screenshots" / f"{stamp}.png"
