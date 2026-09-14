"""Окно панели ставится на сохранённое место само, а не флагами Edge.

Флаги `--window-size` и `--window-position` Edge игнорирует, если уже запущен
как обычный браузер (живой запуск 14.09.2026). Здесь WinAPI подменён: проверяется
логика — какое окно ставить, когда, и что делать, если Edge его передвинул.
"""

from __future__ import annotations

from typing import Any

from jarvis.core.gui.window import place_new_window

SAVED = (13, 13, 1773, 894)
SCREEN = (0, 0, 2400, 1500)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _place(windows: list[list[int]], rects: dict[int, Any] | None = None, **kwargs: Any) -> tuple[bool, list[Any]]:
    clock = Clock()
    placed: list[Any] = []
    polls = iter(windows)
    last = windows[-1]
    ok = place_new_window(
        SAVED, {1},
        find=lambda: next(polls, last),
        measure=lambda hwnd: (rects or {}).get(hwnd, SAVED),
        place=lambda hwnd, geometry: placed.append((hwnd, geometry)),
        screen=lambda: SCREEN,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )
    return ok, placed


def test_only_the_new_window_is_placed() -> None:
    # Окно 1 было открыто до запуска; новое (2) появляется со второго опроса.
    ok, placed = _place([[1], [1, 2]])
    assert ok and placed == [(2, SAVED)]


def test_window_moved_by_edge_is_placed_again() -> None:
    ok, placed = _place([[1, 2]], rects={2: (0, 0, 800, 600)})
    assert ok and len(placed) > 1 and all(item == (2, SAVED) for item in placed)


def test_no_new_window_means_nothing_moves() -> None:
    ok, placed = _place([[1]], timeout=1.0)
    assert not ok and placed == []


def test_position_from_a_disconnected_monitor_is_not_used() -> None:
    clock = Clock()
    placed: list[Any] = []
    ok = place_new_window(
        (5000, 40, 1400, 900), set(),
        find=lambda: [2], measure=lambda hwnd: None,
        place=lambda hwnd, geometry: placed.append(hwnd),
        screen=lambda: SCREEN, sleep=clock.sleep, clock=clock,
    )
    assert not ok and placed == []
