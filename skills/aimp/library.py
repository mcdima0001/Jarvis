r"""Фонотека AIMP: его собственные плейлисты, разобранные с диска.

Владелец спросил, не написать ли для AIMP свой плагин, чтобы искать по его
музыке и знать, что играет. Плагин не понадобился: AIMP хранит плейлисты
**обычным текстом** в `%AppData%\AIMP\PLS\*.aimppl4` — UTF-16, строка на трек:

    D:\Music\Любимое\NCS, buckeye - Ark.mp3|Ark|NCS;buckeye|Ark|...

Замер 24.09.2026 на машине владельца: 10 плейлистов, **1972 уникальных файла**,
все пути живые (проверено 300 из 300), у большинства заполнены теги. Этого
хватает на поиск по названию и исполнителю — без плагина, без сети и без
обращения к модели.

Модуль чистый: ни Windows, ни AIMP тут нет, только разбор текста. Поэтому он
проверяется тестами на сервере, как и подбор программ в скилле `windows`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Где AIMP держит плейлисты.
PLAYLISTS = Path.home() / "AppData" / "Roaming" / "AIMP" / "PLS"

#: Разделитель шапки и списка треков внутри файла плейлиста.
CONTENT = "#-----CONTENT-----#"

#: Поля строки трека, которые нам нужны. Остальные — битрейт, длительность,
#: размер и прочее — не трогаем: они меняются от версии к версии.
PATH, TITLE, ARTIST, ALBUM = 0, 1, 2, 3


@dataclass(frozen=True)
class Track:
    """Трек так, как о нём знает плейлист."""

    path: str
    title: str
    artist: str
    album: str
    #: Номер в своём плейлисте, с нуля: именно им AIMP и переключается.
    number: int

    @property
    def said(self) -> str:
        """Как трек называют вслух.

        Теги бывают пустыми — у владельца это треть фонотеки, — и тогда
        остаётся имя файла. Оно почти всегда и есть «Исполнитель - Название»:
        так их сохраняют качалки.
        """
        if self.title and self.artist:
            return f"{self.artist.replace(';', ', ')} - {self.title}"
        return self.title or Path(self.path).stem


@dataclass(frozen=True)
class Playlist:
    """Плейлист целиком: имя, треки и порядок, в котором их видит AIMP."""

    name: str
    tracks: tuple[Track, ...]

    def __len__(self) -> int:
        return len(self.tracks)


def parse(text: str, *, name: str = "") -> Playlist:
    """Разобрать содержимое `.aimppl4`.

    Порядок строк — это и есть порядок в AIMP, и на нём всё держится: трек
    включается **номером**, а не путём, чтобы ничего не добавлять в плейлист
    владельца и не трогать его фонотеку.
    """
    tracks: list[Track] = []
    if CONTENT in text:
        body = text.split(CONTENT, 1)[1].splitlines()[1:]
        for line in body:
            if not line.strip() or "|" in line[:1] or "|" not in line:
                continue
            fields = line.split("|")
            if len(fields) <= ALBUM or not fields[PATH].strip():
                continue
            tracks.append(Track(
                path=fields[PATH].strip(),
                title=fields[TITLE].strip(),
                artist=fields[ARTIST].strip(),
                album=fields[ALBUM].strip(),
                number=len(tracks),
            ))
    return Playlist(name=name, tracks=tuple(tracks))


def read(path: Path) -> Playlist:
    """Прочитать файл плейлиста. Пустой плейлист — не ошибка.

    Кодировка **UTF-16 с меткой порядка байтов**: AIMP пишет так всегда, а
    кириллица в именах файлов у владельца в каждом втором пути.
    """
    try:
        text = path.read_text("utf-16", errors="replace")
    except OSError as exc:
        logger.debug("Плейлист %s не прочитался: %s", path.name, exc)
        return Playlist(name=path.stem, tracks=())
    return parse(text, name=path.stem)


def playlists(folder: Path = PLAYLISTS) -> list[Playlist]:
    """Все плейлисты AIMP, от больших к маленьким.

    Порядок не косметика: искать трек разумнее в «Любимых» на полторы тысячи
    треков, чем в служебном «Default» на одну строку.
    """
    try:
        files = sorted(folder.glob("*.aimppl4"))
    except OSError:
        return []
    found = [read(path) for path in files]
    return sorted((item for item in found if item.tracks), key=len, reverse=True)


def guess_loaded(found: list[Playlist], *, size: int, playing: str = "", number: int = -1) -> Playlist | None:
    """Какой из плейлистов сейчас открыт в AIMP.

    Спросить его об этом напрямую нечем: наружу он отдаёт только длину списка и
    номер текущего трека. Этого, однако, достаточно — длина у плейлистов разная,
    а совпадение подтверждается названием играющего трека на его номере.

    Знать открытый плейлист нужно ровно затем, чтобы **включать трек номером**.
    Иначе пришлось бы добавлять файл в список, то есть править фонотеку
    владельца ради одной команды.
    """
    fitting = [item for item in found if len(item) == size]
    if not fitting:
        return None
    if len(fitting) == 1 or not playing or not 0 <= number < size:
        return fitting[0]
    said = _bare(playing)
    for item in fitting:
        if _bare(item.tracks[number].said) == said:
            return item
    return fitting[0]


def _bare(text: str) -> str:
    """Название без мелочей, мешающих сравнению: регистр, пробелы, знаки."""
    keep = [character.lower() for character in text if character.isalnum() or character.isspace()]
    return " ".join("".join(keep).split())
