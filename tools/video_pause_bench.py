r"""Слушается ли плеер: ставится ли видео на паузу и снимается ли обратно.

Просьба владельца 23.09.2026 — останавливать видео на время реплики, а не
приглушать. Механизм держится на том, что плеер понимает кнопку «пауза»
мультимедийной клавиатуры, посланную его окну. Понимают её не все, поэтому
стенд проверяет это делом, а не на слово:

    C:\Python314\python.exe tools/video_pause_bench.py

Запускать **при играющем видео**. Стенд находит звучащие плееры, ставит паузу,
смотрит, замолчала ли звуковая сессия, снимает паузу и смотрит снова.

Замер 23.09.2026: VLC не понял ни разу (пять чистых попыток с
``--play-and-exit``: пауза видна по тому, доиграл он файл или нет), и в
системном пульте мультимедиа Windows третьей версией не показывается вовсе.
Для него есть второй путь — его собственный веб-интерфейс (`vlc_http` в
настройках скилла); стенд проверяет и его, если пароль задан в переменной
``VLC_HTTP_PASSWORD``.

Плеер стенд **не закрывает принудительно**: убитый VLC при следующем запуске
показывает окно «VLC just crashed» и вообще не играет — а замер принимал это
за вставшую паузу и врал (пойман на себе же 23.09.2026).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


skill = _load("windows_skill_bench", ROOT / "skills" / "windows" / "skill.py")
media = _load("windows_media_bench", ROOT / "skills" / "windows" / "media.py")

#: Сколько ждать, пока плеер отреагирует. Пауза доходит мгновенно, но звуковая
#: сессия Windows гаснет не в ту же миллисекунду.
SETTLE_S = 1.5


def playing() -> dict[int, tuple[str, bool]]:
    """Кто сейчас звучит: процесс -> (имя, идёт ли звук)."""
    return {
        described.pid: (described.name, described.playing)
        for _, described in skill.sound_sessions()
        if described.pid > 0
    }


def check_windows() -> bool:
    """Первый путь: понимают ли плееры кнопку «пауза», посланную окну."""
    before = playing()
    print("Звуковые сессии сейчас:")
    for pid, (name, active) in sorted(before.items(), key=lambda item: item[1][0]):
        mark = "играет" if active else "молчит"
        video = " — видеоплеер" if media.is_video_player(name) else ""
        print(f"  {name or pid:24} {mark}{video}")

    sessions = [described for _, described in skill.sound_sessions()]
    found = media.plan_pausing(sessions, own_pids=set())
    if not found:
        print("")
        print("Играющих видеоплееров нет — включи видео и запусти снова.")
        return False

    names = {pid: before.get(pid, ("?", False))[0] for pid in found}
    sent = media.pause(set(found))
    print("")
    print(f"Послал «паузу» {sent} окнам: {', '.join(sorted(names.values()))}")
    time.sleep(SETTLE_S)
    paused = playing()
    for pid, name in names.items():
        stopped = not paused.get(pid, (name, False))[1]
        print(f"  {name:24} {'понял паузу' if stopped else 'НЕ ПОНЯЛ — играет дальше'}")

    media.play(set(found))
    time.sleep(SETTLE_S)
    back = playing()
    print("")
    print("После «играй»:")
    for pid, name in names.items():
        resumed = back.get(pid, (name, False))[1]
        print(f"  {name:24} {'играет снова' if resumed else 'НЕ ВЕРНУЛСЯ — остался на паузе'}")
    return True


def check_vlc_http() -> None:
    """Второй путь: отвечает ли VLC по своему HTTP и слушается ли он."""
    vlc = media.Vlc(password=os.environ.get("VLC_HTTP_PASSWORD", ""))
    print("")
    if not vlc.ready:
        print("Веб-интерфейс VLC не настроен (нет VLC_HTTP_PASSWORD) — пропускаю.")
        return
    state = vlc.state()
    if not state:
        print("VLC по HTTP не ответил: включён ли интерфейс «Веб» и верен ли пароль?")
        return
    print(f"VLC по HTTP отвечает, сейчас: {state}")
    print("  пауза:", "понял" if vlc.pause() else "НЕ понял")
    time.sleep(SETTLE_S)
    print("  играй:", "понял" if vlc.play() else "НЕ понял")


def main() -> int:
    ok = check_windows()
    check_vlc_http()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
