r"""AIMP: своя музыка голосом — что играет, поиск по фонотеке, переключение.

Владелец 24.09.2026: «можем написать свой мод для него? Так мы сможем и искать
среди моей музыки и понимать что играет». Плагин не понадобился — всё нужное
AIMP отдаёт сам, и оба пути измерены на живой машине:

* **фонотека** лежит текстом в `%AppData%\AIMP\PLS\*.aimppl4` — 1972 трека у
  владельца, все пути живые (`library.py`);
* **управление** идёт через окно `Winamp v1.x`, которое AIMP держит для
  совместимости, — невидимое, то есть работает и из трея (`remote.py`).

**Трек включается номером в открытом списке, а не файлом.** Это главное
решение скилла: так в фонотеку владельца ничего не добавляется. Плата —
включить можно лишь то, что лежит в открытом плейлисте; остальное скилл честно
называет вместо того, чтобы молча заиграть не то.

В каталог модели ничего не идёт: фразы про музыку объявлены у `page`, и второй
набор тех же слов спорил бы с ним. Зовёт этот скилл `page`, когда расширение
недоступно, — и зовёт по имени инструмента, как и положено.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jarvis.core.contracts import Choice, Intent, ToolResult, numbered
from jarvis.core.skills import HealthStatus, Skill, SkillMeta
from jarvis.core.text import best_match, rank
from jarvis.core.tools import tool

_LIBRARY: Any = None
_REMOTE: Any = None


def _sibling(filename: str, name: str) -> Any:
    """Загрузить модуль рядом со скиллом: скилл грузится по файлу, без пакета."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def library() -> Any:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = _sibling("library.py", "jarvis_skills.aimp_library")
    return _LIBRARY


def remote() -> Any:
    global _REMOTE
    if _REMOTE is None:
        _REMOTE = _sibling("remote.py", "jarvis_skills.aimp_remote")
    return _REMOTE


#: Что просили сделать → кнопка плеера.
BUTTONS = {"next": "NEXT", "previous": "PREVIOUS", "pause": "PAUSE", "play": "PLAY"}

#: Сколько похожих треков предлагать на выбор, если точного совпадения нет.
CHOICES = 5

#: Насколько услышанное должно совпасть с названием, чтобы включать без вопроса.
#: Порог высокий: включить не тот трек — мелочь, но раздражающая, а переспросить
#: стоит одной реплики.
SURE = 0.72


class AimpSkill(Skill):
    """Музыка из AIMP: что играет, включить по названию, переключить."""

    meta = SkillMeta(
        name="aimp",
        description="Своя музыка в AIMP: что играет, поиск по фонотеке, переключение треков.",
        version="0.1.0",
        spoken=("аимп", "aimp", "музыка", "music"),
    )

    async def on_setup(self) -> None:
        """Прочитать настройки. Фонотека читается лениво — при первом вопросе."""
        folder = str(self.context.setting("playlists", "") or "")
        self._folder = Path(folder) if folder else library().PLAYLISTS
        self._sure = float(self.context.setting("sure", SURE))
        self._cached: list[Any] | None = None
        self._cached_at = 0.0

    async def health(self) -> HealthStatus:
        """Здоровье — это «есть ли что искать» и «запущен ли плеер»."""
        found = await self._playlists()
        tracks = sum(len(item) for item in found)
        if not found:
            return HealthStatus.degraded(f"плейлистов AIMP не нашёл в {self._folder}")
        return HealthStatus.ok(
            f"плейлистов {len(found)}, треков {tracks}, "
            f"плеер {'запущен' if remote().running() else 'закрыт'}"
        )

    async def _playlists(self) -> list[Any]:
        """Плейлисты с диска. Перечитываются, когда файлы меняются.

        Читать их на каждую команду незачем — это десяток файлов и полтора
        мегабайта, — но и держать вечно нельзя: владелец добавляет треки, и
        ассистент не должен об этом узнавать только после перезапуска.
        """
        try:
            stamp = max(
                (path.stat().st_mtime for path in self._folder.glob("*.aimppl4")), default=0.0
            )
        except OSError:
            stamp = 0.0
        if self._cached is None or stamp > self._cached_at:
            self._cached = await asyncio.to_thread(library().playlists, self._folder)
            self._cached_at = stamp
            self.log.info(
                "Фонотека AIMP: плейлистов %d, треков %d",
                len(self._cached), sum(len(item) for item in self._cached),
            )
        return self._cached

    @tool(routable=False, reversible=True,
          phrases=["что играет в аимпе", "что играет в aimp", "what is playing in aimp"])
    async def now_playing(self) -> ToolResult:
        """Что играет в AIMP прямо сейчас."""
        now = await asyncio.to_thread(remote().playing)
        if now is None:
            return ToolResult.failure(
                "AIMP не запущен",
                speech={"ru": "AIMP не запущен.", "en": "AIMP isn't running."},
            )
        if not now.said or (not now.playing and not now.paused):
            return ToolResult.failure(
                "в AIMP ничего не играет",
                speech={"ru": "В AIMP ничего не играет.", "en": "AIMP isn't playing anything."},
            )
        said = now.said if now.playing else f"{now.said}, на паузе"
        return ToolResult.success(
            {"track": now.said, "number": now.number, "total": now.total, "playing": now.playing},
            speech={"ru": f"{said}.", "en": f"{said}."},
        )

    @tool(routable=False, reversible=True)
    async def control(self, action: str) -> ToolResult:
        """Переключить музыку в AIMP.

        Окно у плеера невидимое, поэтому команда доходит и когда он свёрнут в
        трей — в отличие от мультимедийной кнопки, которая шлётся видимым окнам.

        :param action: «next», «previous», «pause» или «play».
        """
        button = BUTTONS.get(action)
        if button is None:
            return ToolResult.failure(f"не знаю действие {action!r}")
        if not remote().running():
            return ToolResult.failure("AIMP не запущен")
        if not await asyncio.to_thread(remote().press, getattr(remote(), button)):
            return ToolResult.failure("AIMP не ответил")
        self.log.info("AIMP: %s", action)
        return ToolResult.success({"action": action, "player": "AIMP"})

    @tool(routable=False, reversible=True,
          phrases=["включи в аимпе {track}", "включи в aimp {track}",
                   "поставь в аимпе {track}", "play {track} in aimp"])
    async def play_track(self, track: str) -> ToolResult:
        """Найти трек в своей фонотеке и включить его.

        :param track: название или исполнитель, как их произносят.
        """
        if not remote().running():
            return ToolResult.failure(
                "AIMP не запущен",
                speech={"ru": "AIMP не запущен.", "en": "AIMP isn't running."},
            )
        now = await asyncio.to_thread(remote().playing)
        found = await self._playlists()
        opened = library().guess_loaded(
            found, size=now.total if now else 0,
            playing=now.said if now else "", number=now.number if now else -1,
        )
        if opened is None:
            return ToolResult.failure(
                "не понял, какой плейлист открыт в AIMP",
                speech={"ru": "Не понял, какой список открыт в AIMP.",
                        "en": "I can't tell which AIMP playlist is open."},
            )
        names = {item.said: item for item in opened.tracks}
        exact = best_match(track, list(names), similarity=self._sure)
        if exact is not None:
            return await self._start(names[exact], opened)
        close = rank(track, {name: (name,) for name in names}, limit=CHOICES)
        if not close:
            return ToolResult.failure(
                f"в списке {opened.name!r} нет {track!r}",
                speech={"ru": f"В списке {opened.name} такого не нашёл.",
                        "en": f"Not found in {opened.name}."},
            )
        # Один близкий — считаем, что он и нужен: переспрашивать об
        # единственном похожем значит тратить реплику на ровном месте.
        if len(close) == 1:
            return await self._start(names[close[0]], opened)
        return ToolResult.choosing(
            [self._choice(name) for name in close],
            question={"ru": f"Что включить? {numbered(close)}.",
                      "en": f"Which one? {numbered(close)}."},
        )

    @staticmethod
    def _choice(name: str) -> Choice:
        return Choice(name, Intent(tool="aimp.play_track", arguments={"track": name}))

    async def _start(self, track: Any, playlist: Any) -> ToolResult:
        """Включить найденный трек номером в открытом списке."""
        if not await asyncio.to_thread(remote().play_number, track.number):
            return ToolResult.failure(
                "AIMP не переключился",
                speech={"ru": "AIMP не переключился.", "en": "AIMP didn't switch."},
            )
        self.log.info("AIMP: включаю %r (%s, №%d)", track.said, playlist.name, track.number + 1)
        return ToolResult.success(
            {"track": track.said, "playlist": playlist.name, "number": track.number},
            speech={"ru": f"Включаю {track.said}.", "en": f"Playing {track.said}."},
        )
