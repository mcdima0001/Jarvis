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


def test_downloads_skip_existing_partial_and_growing_files() -> None:
    watch = sentinel.DownloadWatch()
    assert watch.check({"old.pdf": 10}) == [], "что лежало до запуска — не новость"
    assert watch.check({"old.pdf": 10, "movie.mkv.crdownload": 5}) == []
    assert watch.check({"old.pdf": 10, "movie.mkv": 500}) == [], "размер ещё не проверен дважды"
    assert watch.check({"old.pdf": 10, "movie.mkv": 800}) == [], "файл ещё растёт"
    assert watch.check({"old.pdf": 10, "movie.mkv": 800}) == ["movie.mkv"]
    assert watch.check({"old.pdf": 10, "movie.mkv": 800}) == [], "о готовом — один раз"


def test_download_line_names_the_file_not_the_path() -> None:
    assert sentinel.download_line(["Отчёт за сентябрь.pdf"]) == "Загрузилось: Отчёт за сентябрь."
    assert sentinel.download_line(["a.zip", "b.zip"]).startswith("Загрузилось файлов: 2")
