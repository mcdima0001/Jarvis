"""Страж: решения о том, когда говорить, — на чистых функциях, без WinAPI."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    path = _ROOT / "skills" / "sentinel" / "skill.py"
    spec = importlib.util.spec_from_file_location("skill_sentinel_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sentinel = _load()


def test_battery_speaks_once_for_low_once_for_critical_and_resets_on_charge() -> None:
    watch = sentinel.BatteryWatch(low=20, critical=10)
    assert watch.check(50, False) is None
    low = watch.check(19, False)
    assert low and low[1] == sentinel.NORMAL and "19 процентов" in low[0]
    assert watch.check(15, False) is None, "о низком — один раз"
    critical = watch.check(9, False)
    assert critical and critical[1] == sentinel.URGENT
    assert watch.check(7, False) is None
    assert watch.check(40, True) is None
    assert watch.check(18, False), "зарядили и снова сел — снова говорим"


def test_battery_jumping_straight_to_critical_is_one_line() -> None:
    watch = sentinel.BatteryWatch(low=20, critical=10)
    assert watch.check(8, False)[1] == sentinel.URGENT
    assert watch.check(8, False) is None


def test_cpu_share_counts_kernel_time_as_including_idle() -> None:
    # Простой 25 из 100 единиц всего (ядро 60 с простоем, пользователь 40).
    assert sentinel.cpu_share((0, 0, 0), (25, 60, 40)) == 0.75
    assert sentinel.cpu_share((5, 5, 5), (5, 5, 5)) is None


def test_cpu_speaks_after_the_streak_and_once_until_it_calms_down() -> None:
    watch = sentinel.CpuWatch(busy=0.9, minutes=10)
    assert watch.check(0.95, 0) is None
    assert watch.check(0.95, 9 * 60) is None
    line = watch.check(0.96, 10 * 60)
    assert line and "10 минут" in line
    assert watch.check(0.97, 20 * 60) is None, "пока не отпустило — молчим"
    assert watch.check(0.3, 21 * 60) is None
    assert watch.check(0.95, 22 * 60) is None, "новая серия начинается с нуля"


def _folder(now: float, **files: int) -> dict[str, tuple[int, float]]:
    """Папка «Загрузки»: имя → размер и свежее время правки."""
    return {name.replace("__", "."): (size, now) for name, size in files.items()}


def test_downloads_skip_existing_partial_and_growing_files() -> None:
    watch = sentinel.DownloadWatch()
    now = 1_000_000.0
    assert watch.check(_folder(now, old__pdf=10), now) == [], "что лежало до запуска — не новость"
    assert watch.check(_folder(now, old__pdf=10, movie__mkv__crdownload=5), now) == []
    assert watch.check(_folder(now, old__pdf=10, movie__mkv=500), now) == [], "размер ещё не проверен дважды"
    assert watch.check(_folder(now, old__pdf=10, movie__mkv=800), now) == [], "файл ещё растёт"
    assert watch.check(_folder(now, old__pdf=10, movie__mkv=800), now) == ["movie.mkv"]
    assert watch.check(_folder(now, old__pdf=10, movie__mkv=800), now) == [], "о готовом — один раз"


def test_a_file_put_back_is_not_a_new_download() -> None:
    """21.09.2026: один и тот же .mrpack объявлялся загруженным трижды за час."""
    watch = sentinel.DownloadWatch()
    now = 1_000_000.0
    old = {"pack.mrpack": (6525, now - sentinel.FRESH_S - 1)}
    assert watch.check(old, now) == []
    assert watch.check({}, now) == [], "файл унесли — забыли"
    assert watch.check(old, now) == [], "вернули тот же файл: время правки старое, это не загрузка"
    assert watch.check(old, now) == []
    # Размер за эти проходы не менялся, значит устоявшимся он уже считается:
    # как только время правки стало свежим, это настоящая загрузка.
    assert watch.check({"pack.mrpack": (6525, now)}, now) == ["pack.mrpack"]


def test_ignored_names_are_not_even_seen(tmp_path: Any) -> None:
    """Просьба владельца 21.09.2026: пусть на что-то не смотрит вовсе."""
    (tmp_path / "photo_1.jpg").write_bytes(b"x")
    (tmp_path / "report.pdf").write_bytes(b"y")
    assert sorted(sentinel.scan(tmp_path)) == ["photo_1.jpg", "report.pdf"]
    assert sorted(sentinel.scan(tmp_path, ("photo_*.jpg",))) == ["report.pdf"]


def test_download_line_does_not_read_file_names() -> None:
    """Просьба владельца 15.09.2026: имя вроде «6ec697122191d32398a6…» вслух не нужно."""
    assert sentinel.download_line(["6ec697122191d32398a60275159ce5cf4f00440a.bin"]) == "Файл загрузился."
    assert sentinel.download_line(["a.bin", "b.zip"]) == "Загрузилось 2 файла."


def test_download_line_says_what_arrived() -> None:
    """Просьба владельца 17.09.2026: «.mp3 — трек загрузился, .jpg — фотка скачалась»."""
    assert sentinel.download_line(["Song.MP3"]) == "Трек загрузился."
    assert sentinel.download_line(["IMG_0042.jpg"]) == "Фотка скачалась."
    assert sentinel.download_line(["a.png", "b.jpg", "c.webp", "d.heic", "e.jpg"]) == "Загрузилось 5 фоток."
    assert sentinel.download_line(["one.mp3", "two.flac"]) == "Загрузилось 2 трека."


async def test_downloads_are_said_together_when_the_pause_ends_or_never(monkeypatch: Any) -> None:
    """19.09.2026: «фотка скачалась» прозвучала через десять минут, досказанной из придержанного."""
    import logging
    import time
    from types import SimpleNamespace

    decisions = ["drop", "drop", "say"]
    offered: list[tuple[str, bool]] = []

    def offer(text: str, **kwargs: Any) -> str:
        offered.append((text, kwargs["hold"]))
        return decisions.pop(0)

    skill = sentinel.SentinelSkill()
    skill._context = SimpleNamespace(  # type: ignore[assignment]
        announcer=SimpleNamespace(offer=offer), logger=logging.getLogger("test.sentinel")
    )
    skill._downloads = sentinel.DownloadWatch()
    skill._unsaid, skill._unsaid_since, skill._last_said, skill._repeat_s = [], 0.0, {}, 3600.0
    skill._ignore = ()
    folder: dict[str, tuple[int, float]] = {}
    monkeypatch.setattr(sentinel, "scan", lambda path, ignore=(): dict(folder))

    await skill._check_downloads(Path("."))  # первый проход только запоминает
    folder["a.jpg"] = (10, time.time())
    await skill._check_downloads(Path("."))  # размер ещё не устоялся
    await skill._check_downloads(Path("."))  # готово, но пауза — не сказано
    folder["b.jpg"] = (20, time.time())
    await skill._check_downloads(Path("."))
    await skill._check_downloads(Path("."))  # вторая готова — обе одной репликой
    assert offered == [
        ("Фотка скачалась.", False),  # пауза: не придерживается, а пробуется снова
        ("Фотка скачалась.", False),
        ("Загрузилось 2 фотки.", False),
    ]
    assert skill._unsaid == []
