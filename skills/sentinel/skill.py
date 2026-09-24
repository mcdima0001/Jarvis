"""Страж: сам замечает, что с компьютером неладно, и говорит об этом.

Как в фильме: «Сэр, заряд десять процентов». Просьба владельца 14.09.2026.
До этого Jarvis существовал ровно те секунды, пока к нему обращались, и узнать
о севшей батарее или забитом диске можно было только самому.

Что замечает: заряд (дважды — при низком и при критическом), свободное место на
системном диске, процессор, загруженный подряд дольше заданного, и законченные
загрузки. Пороги и частота — в config.yaml скилла, **замеры каждого прохода
пишутся в лог**: порог, который нельзя проверить, не ставится.

Говорит не сам, а через политику речи без вопроса (`Announcer`): ночью и во
время «не слушаю» придержит, чаще раза в минуту не заговорит. Одно и то же
предупреждение повторяется не чаще `repeat_after_min`.

Зависимостей ноль: заряд и процессор — через WinAPI, диск и загрузки —
стандартной библиотекой. Только Windows.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from jarvis.core.attention import LOW, NORMAL, URGENT
from jarvis.core.contracts import ToolResult
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.tools import tool
from jarvis.core.tts.normalize import plural_form

#: Недокачанные файлы браузеров и качалок: о таких рано говорить «загрузилось».
PARTIAL = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut")
#: Насколько свежей должна быть правка файла, чтобы считать его только что
#: скачанным, сек. Перекладывание файла туда-сюда время правки не меняет.
FRESH_S = 120.0
PERCENT = ("процент", "процента", "процентов")
GIGABYTE = ("гигабайт", "гигабайта", "гигабайт")
#: Сколько секунд придержанное замечание стража остаётся правдой (16.09.2026:
#: «7 гигабайт» прозвучало через три часа, когда было уже 18).
STALE_AFTER_S = 600.0
#: Сколько секунд новость о загрузке ждёт паузы между репликами, прежде чем устареть.
DOWNLOAD_NEWS_S = 90.0
MINUTE = ("минуту", "минуты", "минут")


# --- замеры ---------------------------------------------------------------


class _POWER(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def battery() -> tuple[int, bool] | None:
    """Заряд в процентах и «на зарядке ли»; ``None`` — батареи нет или не узнать."""
    try:
        status = _POWER()
        if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(status)):
            return None
    except (OSError, AttributeError):
        return None
    # 128 — «батареи нет», 255 — «состояние неизвестно»: настольный компьютер.
    if status.BatteryFlag & 128 or status.BatteryLifePercent == 255:
        return None
    return int(status.BatteryLifePercent), status.ACLineStatus == 1


def cpu_times() -> tuple[int, int, int] | None:
    """Суммарные времена процессора: простой, ядро (с простоем), пользователь."""
    try:
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.WinDLL("kernel32").GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
    except (OSError, AttributeError):
        return None

    def value(stamp: wintypes.FILETIME) -> int:
        return (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime

    return value(idle), value(kernel), value(user)


def cpu_share(before: tuple[int, int, int], after: tuple[int, int, int]) -> float | None:
    """Доля занятости всего процессора между двумя замерами, 0…1.

    Время ядра у Windows включает простой, поэтому занятость — это всё время
    минус простой, делённое на всё время.
    """
    idle = after[0] - before[0]
    total = (after[1] - before[1]) + (after[2] - before[2])
    if total <= 0:
        return None
    return max(0.0, min(1.0, 1 - idle / total))


def system_drive() -> str:
    """Системный диск: там живут программы и временные файлы."""
    return (os.environ.get("SystemDrive") or "C:") + "\\"


def downloads_dir() -> Path | None:
    """Папка загрузок профиля."""
    home = os.environ.get("USERPROFILE")
    if not home:
        return None
    for name in ("Downloads", "Загрузки"):
        path = Path(home) / name
        if path.is_dir():
            return path
    return None


def scan(folder: Path, ignore: Sequence[str] = ()) -> dict[str, tuple[int, float]]:
    """Файлы папки загрузок: имя → (размер, время правки). Подпапки не смотрим.

    :param ignore: маски имён, которые пропускать совсем (`fnmatch`).
    """
    found: dict[str, tuple[int, float]] = {}
    try:
        for entry in os.scandir(folder):
            if not entry.is_file():
                continue
            if any(fnmatch(entry.name.lower(), mask) for mask in ignore):
                continue
            stat = entry.stat()
            found[entry.name] = (stat.st_size, stat.st_mtime)
    except OSError:
        return {}
    return found


# --- решения (чистые функции) ---------------------------------------------


@dataclass
class BatteryWatch:
    """Когда говорить о заряде: по разу на низкий и на критический.

    На зарядке всё сбрасывается: сел, зарядил, снова сел — снова скажем.
    """

    low: int = 20
    critical: int = 10
    said: set[str] = field(default_factory=set)

    def check(self, percent: int, charging: bool) -> tuple[str, str] | None:
        """Реплика и важность; ``None`` — молчать."""
        if charging:
            self.said.clear()
            return None
        words = plural_form(percent, PERCENT)
        if percent <= self.critical and "critical" not in self.said:
            self.said.update({"critical", "low"})
            return f"Сэр, заряд {percent} {words}. Пора на зарядку, скоро ноутбук выключится.", URGENT
        if percent <= self.low and "low" not in self.said:
            self.said.add("low")
            return f"Сэр, заряд {percent} {words}.", NORMAL
        return None


@dataclass
class CpuWatch:
    """Процессор занят подряд дольше заданного — сказать один раз, пока не отпустит."""

    busy: float = 0.9
    minutes: float = 10.0
    since: float | None = None
    said: bool = False

    def check(self, share: float, now: float) -> str | None:
        if share < self.busy:
            self.since, self.said = None, False
            return None
        if self.since is None:
            self.since = now
        lasted = (now - self.since) / 60
        if not self.said and lasted >= self.minutes:
            self.said = True
            minutes = int(lasted)
            return (
                f"Сэр, процессор уже {minutes} {plural_form(minutes, MINUTE)} загружен на "
                f"{round(share * 100)} {plural_form(round(share * 100), PERCENT)}. "
                "Кто грузит, скажу по просьбе «что грузит процессор»."
            )
        return None


@dataclass
class DownloadWatch:
    """Какие загрузки закончились.

    Первый проход только запоминает, что уже лежит: иначе при запуске Jarvis
    перечислил бы всю папку. Готовой считается загрузка, которая не выглядит
    недокачанной и чей размер не изменился между двумя проходами — браузер
    дописывает файл кусками и под своим, и под итоговым именем.
    """

    known: set[str] | None = None
    sizes: dict[str, tuple[int, float]] = field(default_factory=dict)
    #: Насколько свежей должна быть правка файла, чтобы это считалось загрузкой.
    fresh_s: float = FRESH_S

    def check(self, files: dict[str, tuple[int, float]], now: float | None = None) -> list[str]:
        moment = time.time() if now is None else now
        if self.known is None:
            self.known = set(files)
            self.sizes = dict(files)
            return []
        finished = [
            name for name, (size, changed) in files.items()
            if name not in self.known
            and not name.lower().endswith(PARTIAL)
            and self.sizes.get(name, (None, 0.0))[0] == size
            and size > 0
            # Файл, который лежит тут давно, не «только что скачался», даже если
            # в списке он новый: программы перекладывают файлы туда-обратно, и
            # 21.09.2026 один и тот же .mrpack объявлялся загруженным трижды —
            # в 17:12, 17:13 и 18:15. Время правки при переносе сохраняется, а
            # при настоящей загрузке оно свежее.
            and moment - changed <= self.fresh_s
        ]
        self.known.update(finished)
        # Удалённые забываем: скачали тот же файл заново — снова скажем.
        self.known.intersection_update(files)
        self.sizes = dict(files)
        return sorted(finished)


#: Что именно скачалось — по расширению (просьба владельца 17.09.2026: «.mp3 —
#: трек загрузился, .jpg — фотка скачалась»). Одно: реплика; много одного рода:
#: формы слова для числа. Чего нет в таблице — просто «файл».
DOWNLOAD_KINDS: tuple[tuple[tuple[str, ...], str, tuple[str, str, str]], ...] = (
    ((".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".wma", ".aiff"),
     "Трек загрузился.", ("трек", "трека", "треков")),
    ((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"),
     "Фотка скачалась.", ("фотка", "фотки", "фоток")),
    ((".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"),
     "Видео скачалось.", ("видео", "видео", "видео")),
    ((".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"),
     "Архив скачался.", ("архив", "архива", "архивов")),
    ((".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"),
     "Документ скачался.", ("документ", "документа", "документов")),
    ((".exe", ".msi", ".msix", ".appx"),
     "Установщик скачался.", ("установщик", "установщика", "установщиков")),
    ((".iso", ".img"), "Образ диска скачался.", ("образ диска", "образа диска", "образов диска")),
    ((".flp",), "Проект FL Studio скачался.", ("проект FL Studio", "проекта FL Studio", "проектов FL Studio")),
    ((".torrent",), "Торрент скачался.", ("торрент", "торрента", "торрентов")),
)
_FILE = ("файл", "файла", "файлов")


def _kind(name: str) -> tuple[str, tuple[str, str, str]]:
    """Реплика об одном файле и формы слова для счёта."""
    suffix = Path(name).suffix.lower()
    for suffixes, line, forms in DOWNLOAD_KINDS:
        if suffix in suffixes:
            return line, forms
    return "Файл загрузился.", _FILE


def download_line(names: list[str]) -> str:
    """Реплика о законченных загрузках: что это было, но без названий.

    Имя файла вслух бесполезно (просьба владельца 15.09.2026): у загрузок оно
    часто хеш вроде «6ec697122191d32398a6…», и синтез читал его целиком.
    """
    kinds = [_kind(name) for name in names]
    if len(names) == 1:
        return kinds[0][0]
    forms = kinds[0][1] if all(kind[1] == kinds[0][1] for kind in kinds) else _FILE
    return f"Загрузилось {len(names)} {plural_form(len(names), forms)}."


# --- скилл ------------------------------------------------------------------


class SentinelSkill(Skill):
    """Слежение за машиной: заряд, диск, процессор, загрузки."""

    meta = SkillMeta(
        name="sentinel",
        description="Страж: сам говорит о заряде, диске, нагрузке и загрузках",
        version="0.1.6",
        platforms=("windows",),
        spoken=("страж", "слежение", "sentinel"),
    )

    async def on_setup(self) -> None:
        """Прочитать пороги."""
        setting = self.context.setting
        self._every = max(5.0, float(setting("every_s", 30)))
        self._battery = BatteryWatch(low=int(setting("battery_low", 20)), critical=int(setting("battery_critical", 10)))
        self._disk_free_gb = float(setting("disk_free_gb", 10))
        self._cpu = CpuWatch(busy=float(setting("cpu_busy", 90)) / 100, minutes=float(setting("cpu_busy_minutes", 10)))
        self._downloads = DownloadWatch() if bool(setting("downloads", True)) else None
        #: Загрузки смотрятся чаще прочего: о готовом файле хотят слышать сразу, а
        #: не через полминуты (просьба владельца 17.09.2026). Проход — список одной папки.
        self._downloads_every = max(1.0, float(setting("downloads_every_s", 2)))
        #: Маски имён, о которых не сообщать вовсе (нижний регистр, `fnmatch`).
        self._ignore = tuple(
            str(mask).lower() for mask in (setting("downloads_ignore", []) or ())
        )
        #: Докачавшееся, о чём ещё не сказали (пауза между репликами), и с какого момента.
        self._unsaid: list[str] = []
        self._unsaid_since = 0.0
        self._repeat_s = float(setting("repeat_after_min", 60)) * 60
        self._last_said: dict[str, float] = {}
        self._cpu_before: tuple[int, int, int] | None = None
        self._last = ""

    async def on_start(self) -> None:
        """Начать смотреть."""
        self.context.scope.spawn(self._watch(), name="sentinel-watch")
        folder = downloads_dir() if self._downloads is not None else None
        if folder is not None:
            self.context.scope.spawn(self._watch_downloads(folder), name="sentinel-downloads")

    async def health(self) -> HealthStatus:
        return HealthStatus.healthy(self._last) if self._last else HealthStatus.healthy()

    @tool(
        phrases=["как там ноутбук", "как там компьютер", "состояние компьютера", "состояние ноутбука",
                 "сколько заряда", "сколько места на диске", "how is the computer"],
        reversible=True, routable=False)
    async def status(self) -> ToolResult:
        """Рассказать о машине: заряд, место на диске, загрузка процессора."""
        power, free, share = await asyncio.to_thread(self._measure)
        parts: list[str] = []
        if power is not None:
            percent, charging = power
            parts.append(f"заряд {percent} {plural_form(percent, PERCENT)}{', заряжается' if charging else ''}")
        if free is not None:
            gigabytes = round(free)
            parts.append(f"на диске свободно {gigabytes} {plural_form(gigabytes, GIGABYTE)}")
        if share is not None:
            parts.append(f"процессор занят на {round(share * 100)} {plural_form(round(share * 100), PERCENT)}")
        text = "; ".join(parts) or "замерить не получилось"
        return ToolResult.success(
            {"battery": power, "disk_free_gb": free, "cpu": share},
            speech={"ru": f"{text[:1].upper()}{text[1:]}.", "en": text},
        )

    # --- наблюдение ----------------------------------------------------------

    def _measure(self) -> tuple[tuple[int, bool] | None, float | None, float | None]:
        power = battery()
        try:
            free: float | None = shutil.disk_usage(system_drive()).free / 1024**3
        except OSError:
            free = None
        now = cpu_times()
        share = cpu_share(self._cpu_before, now) if self._cpu_before and now else None
        self._cpu_before = now or self._cpu_before
        return power, free, share

    async def _watch(self) -> None:
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — страж не имеет права умереть от одного замера
                self.log.warning("Страж: проход не удался: %s", exc)
            await asyncio.sleep(self._every)

    async def _watch_downloads(self, folder: Path) -> None:
        """Загрузки — своим, частым проходом: готовым файл считается через два прохода."""
        while True:
            try:
                await self._check_downloads(folder)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — страж не имеет права умереть от одного замера
                self.log.warning("Страж: проход по загрузкам не удался: %s", exc)
            await asyncio.sleep(self._downloads_every)

    async def _check_downloads(self, folder: Path) -> None:
        if self._downloads is None:
            return
        finished = self._downloads.check(await asyncio.to_thread(scan, folder, self._ignore))
        now = time.monotonic()
        if finished:
            if not self._unsaid:
                self._unsaid_since = now
            self._unsaid.extend(finished)
        if not self._unsaid:
            return
        if now - self._unsaid_since > DOWNLOAD_NEWS_S:
            # Про загрузку говорят сразу или никогда: «фотка скачалась» через
            # десять минут никому не нужна (19.09.2026: придержанное прозвучало
            # в 15:47 про фотку из 15:37).
            self.log.debug("Страж: новость о %d загрузках устарела, не говорю", len(self._unsaid))
            self._unsaid.clear()
            return
        # Не придерживаем, а повторяем попытку на следующем проходе: пауза между
        # репликами пройдёт — скажем обо всех накопившихся одной репликой.
        decision = self._say(
            f"download:{self._unsaid[-1]}", download_line(self._unsaid), LOW,
            repeat=False, allow_repeat=True, hold=False,
        )
        if decision == "say":
            self._unsaid.clear()

    async def _check_once(self) -> None:
        power, free, share = await asyncio.to_thread(self._measure)
        self._last = (
            f"заряд {power[0] if power else '—'}%, диск {free:.1f} ГБ, процессор "
            f"{round(share * 100) if share is not None else '—'}%" if free is not None else ""
        )
        # Замер каждого прохода — в лог: по нему проверяются пороги.
        self.log.debug("Страж: %s", self._last or "замер не удался")

        if power is not None:
            said = self._battery.check(*power)
            if said:
                self._say("battery", said[0], said[1], repeat=False)
        if free is not None and free < self._disk_free_gb:
            gigabytes = max(0, round(free))
            self._say("disk", f"Сэр, на системном диске осталось {gigabytes} {plural_form(gigabytes, GIGABYTE)}.", NORMAL)
        if share is not None:
            line = self._cpu.check(share, time.monotonic())
            if line:
                self._say("cpu", line, NORMAL, repeat=False)

    def _say(
        self,
        key: str,
        text: str,
        importance: str,
        *,
        repeat: bool = True,
        allow_repeat: bool = False,
        hold: bool = True,
    ) -> str:
        """Предложить реплику политике речи; одно и то же — не чаще `repeat_after_min`.

        :return: решение политики: ``say``, ``hold`` или ``drop``; пусто — не предлагали.
        """
        now = time.monotonic()
        if repeat and now - self._last_said.get(key, -1e9) < self._repeat_s:
            return ""
        self._last_said[key] = now
        # Замер правдив, пока свеж: придержанное дольше `STALE_AFTER_S` не говорим.
        decision = self.context.announcer.offer(
            text, importance=importance, language="ru", expires_s=STALE_AFTER_S,
            allow_repeat=allow_repeat, hold=hold,
        )
        # Отложенная загрузка переспрашивается каждые пару секунд — в лог только итог.
        (self.log.info if decision != "drop" or hold else self.log.debug)(
            "Страж (%s): %s → %s", importance, text, decision
        )
        return decision
