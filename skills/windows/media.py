"""Видео на паузу, музыку — приглушить.

Просьба владельца 23.09.2026: «поставить на паузу видео, особенно если
скачанное и особенно если VLC, когда он начинает говорить, вместо того чтобы
занижать громкость: он перебивает кучу всего, и приходится отлистывать
обратно. А в музыке пусть так и будет, пусть приглушает».

Разница между музыкой и видео тут не вкусовая, а по цене ошибки. Приглушённая
музыка **ничего не теряет**: слышно тише, но играет то же самое. Приглушённое
видео теряет кусок сюжета, и вернуть его можно только руками — отмотав назад.
Поэтому музыка приглушается, как и раньше, а видео останавливается.

**Способов два, и это не запас прочности, а замер.**

Первый — то же сообщение, которым до плеера доходит кнопка «пауза»
мультимедийной клавиатуры (`WM_APPCOMMAND`), посланное **конкретному окну**, а
не в систему: глобальная кнопка уходит тому, кого Windows сочтёт текущим
проигрывателем, и на паузу вместо фильма встала бы музыка. Зависимостей ноль,
доходит мгновенно. Работает у плееров, которые слушают это сообщение окном, —
MPC-HC, PotPlayer.

Второй — **веб-интерфейс самого VLC**, и он появился не от хорошей жизни.
Замер 23.09.2026 (`tools/video_pause_bench.py`, пять чистых попыток с
`--play-and-exit`: пауза видна по тому, доиграл плеер файл или нет): VLC не
встал на паузу **ни разу**. Системный пульт мультимедиа (SMTC), из которого
Windows останавливает музыку с панели, VLC третьей версии тоже не показывает —
проверено, список сессий при играющем VLC пуст. Остаётся его собственный
HTTP: `pl_forcepause` и `pl_forceresume` — команды **раздельные**, а не
переключатель, и состояние он тоже отдаёт. Плата — разовая настройка в самом
VLC (интерфейс «Веб» и пароль), без неё этот путь молча не используется.

Команды везде берутся раздельные — «пауза» и «играй», а не «пауза/играй» одной
кнопкой. Переключатель рассинхронизируется на первой же осечке: не дошло —
дальше всё наоборот, и «возврат» останавливает то, что играло.
"""

from __future__ import annotations

import logging
from collections.abc import Container, Iterable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: Кнопки мультимедийной клавиатуры, как их видит приложение.
WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_PAUSE = 47
APPCOMMAND_MEDIA_PLAY = 46

#: Кто считается видеоплеером. Имена процессов, регистр не важен.
#:
#: Браузера тут нет намеренно, и это не забывчивость: во вкладке одинаково
#: бывает и фильм, и музыка, а различить их снаружи нечем. Владелец просил
#: именно про скачанное — то есть про то, что открывают отдельной программой.
#: Имя процесса VLC. Нужно отдельно от списка плееров: у него свой путь — не
#: звуковые сессии, а собственный веб-интерфейс, — и спросить, запущен ли он,
#: надо **до** стука по HTTP.
VLC_IMAGE = "vlc.exe"

VIDEO_PLAYERS: tuple[str, ...] = (
    "vlc",
    "mpc-hc", "mpc-hc64", "mpc-be", "mpc-be64",
    "mpv",
    "potplayer", "potplayermini", "potplayermini64",
    "video.ui",          # «Кино и ТВ» в Windows
    "wmplayer",
    "smplayer",
    "kmplayer",
    "gom",
    "qbittorrent",       # встроенный просмотр
)


def is_video_player(name: str, players: Iterable[str] = VIDEO_PLAYERS) -> bool:
    """Видеоплеер ли это по имени процесса («vlc.exe» → да)."""
    low = name.strip().lower().removesuffix(".exe")
    if not low:
        return False
    return any(low == known or low.startswith(known) for known in players)


def plan_pausing(
    sessions: Sequence["object"],
    *,
    own_pids: Container[int],
    players: Iterable[str] = VIDEO_PLAYERS,
) -> tuple[int, ...]:
    """Кого ставить на паузу: видеоплееры, которые сейчас звучат.

    :param sessions: описания звуковых сессий (`SoundSession`): нужны имя,
        номер процесса и играет ли он прямо сейчас.
    :return: номера процессов без повторов, в порядке появления.

    Молчащий плеер не трогается: он либо уже на паузе, либо это открытое окно
    без воспроизведения — и «возврат» потом запустил бы его сам, без просьбы.
    """
    found: list[int] = []
    for session in sessions:
        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0 or pid in own_pids or pid in found:
            continue
        if not getattr(session, "playing", True):
            continue
        if is_video_player(str(getattr(session, "name", "")), players):
            found.append(pid)
    return tuple(found)


def windows_of(pids: Container[int]) -> list[int]:
    """Видимые окна этих процессов — кому слать команду."""
    # Внутри функции, а не наверху файла: `wintypes` есть только в Windows, а
    # разбор фраз из этого модуля проверяется тестами и на сервере.
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(handle):
            found.append(int(handle))
        return True

    user32.EnumWindows(visit, 0)
    return found


def tell(pids: Container[int], command: int) -> int:
    """Послать окнам этих процессов команду мультимедийной кнопки.

    :return: скольким окнам послали. Ноль — окон нет: плеер свёрнут в трей или
        играет без окна, и остановить его этим способом нечем.
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    sent = 0
    for handle in windows_of(pids):
        # PostMessage, а не SendMessage: ответа нам не нужно, а ждать зависший
        # плеер посреди реплики нельзя — голос важнее.
        if user32.PostMessageW(handle, WM_APPCOMMAND, handle, command << 16):
            sent += 1
    return sent


def pause(pids: Container[int]) -> int:
    """Поставить на паузу."""
    return tell(pids, APPCOMMAND_MEDIA_PAUSE)


def play(pids: Container[int]) -> int:
    """Снять с паузы."""
    return tell(pids, APPCOMMAND_MEDIA_PLAY)


# --- VLC через его собственный веб-интерфейс ---------------------------------


class Vlc:
    """Пауза в VLC по HTTP: единственный способ, который он слушает.

    Включается разово в самом VLC (Инструменты → Настройки → Все → Интерфейс →
    Основные интерфейсы → «Веб», и пароль в разделе Lua → Lua HTTP). Без пароля
    VLC интерфейс не поднимает вовсе, поэтому пустой пароль означает «не
    настроено» — молча работаем прежним способом.
    """

    def __init__(self, *, url: str = "http://127.0.0.1:8080", password: str = "",
                 timeout: float = 1.5) -> None:
        self._url = url.rstrip("/")
        self._password = password
        self._timeout = timeout
        #: Соединение держим одно. Замер 23.09.2026: запрос по готовому
        #: соединению идёт 0.05 с, а с созданием клиента на каждый раз — целую
        #: секунду. Пауза ставится посреди начинающейся реплики, и секунда тут
        #: — это ровно тот кусок фильма, ради которого всё и затевалось.
        self._client: Any | None = None

    @property
    def ready(self) -> bool:
        """Настроен ли путь вообще."""
        return bool(self._url and self._password)

    def _ask(self, command: str = "") -> str:
        """Дёрнуть VLC и вернуть ответ; пустая строка — не ответил."""
        import httpx

        try:
            if self._client is None:
                # trust_env=False: локальному VLC системный прокси не нужен, а
                # разбор переменных окружения стоит времени на каждом клиенте.
                self._client = httpx.Client(
                    auth=("", self._password), timeout=self._timeout, trust_env=False
                )
            response = self._client.get(
                f"{self._url}/requests/status.xml",
                params={"command": command} if command else None,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — чужая служба, своя работа важнее
            logger.debug("VLC не ответил по HTTP: %s: %s", type(exc).__name__, exc)
            self.close()
            return ""
        return response.text

    def close(self) -> None:
        """Отпустить соединение: следующий запрос откроет новое."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — закрываем на всякий случай
                pass

    @staticmethod
    def _state_of(text: str) -> str:
        """Состояние из ответа VLC. Разбираем поле, а не ищем слово в тексте:
        название файла вполне может содержать «paused»."""
        start = text.find("<state>")
        if start < 0:
            return ""
        return text[start + 7 : text.find("</state>", start)].strip()

    def state(self) -> str:
        """«playing», «paused», «stopped» или пусто, если не достучались."""
        return self._state_of(self._ask())

    def pause(self) -> bool:
        """Остановить. ``False`` — не вышло, пусть работает прежний способ."""
        return self.ready and self._state_of(self._ask("pl_forcepause")) == "paused"

    def play(self) -> bool:
        """Продолжить."""
        return self.ready and self._state_of(self._ask("pl_forceresume")) == "playing"
