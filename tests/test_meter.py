"""Учёт процессорного времени по звеньям.

Жалоба «ноутбук греется» сама по себе не указывает на виновного: в голосовом
круге непрерывно работают четыре вещи сразу. Здесь проверяется, что счёт
раскладывается по звеньям честно и что выключенный счётчик не стоит ничего.
"""

from __future__ import annotations

import threading
import time

from jarvis.core.meter import Load, Meter


def burn(seconds: float) -> None:
    """Занять процессор на заданное время, а не поспать."""
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


# --- счёт -------------------------------------------------------------------


def test_work_lands_in_its_own_stage() -> None:
    """Потраченное записывается тому звену, под которым считалось."""
    meter = Meter()
    with meter.stage("имя"):
        burn(0.02)

    load = meter.peek()
    assert load.stages["имя"] >= 0.01
    assert "речь" not in load.stages


def test_stages_accumulate_across_calls() -> None:
    """Звено вызывается тысячи раз за минуту, и счёт копится."""
    meter = Meter()
    for _ in range(3):
        with meter.stage("речь"):
            burn(0.005)

    assert meter.peek().stages["речь"] >= 0.01


def test_waiting_is_not_counted() -> None:
    """Ожидание не греет, и в счёт идти не должно.

    Звено, которое полсекунды ждёт сеть, ноутбук не греет; звено, которое
    полсекунды считает, греет. Поэтому меряется процессорное время, а не время
    по часам.
    """
    meter = Meter()
    with meter.stage("сеть"):
        time.sleep(0.05)

    assert meter.peek().stages.get("сеть", 0.0) < 0.01


def test_other_threads_do_not_leak_into_the_stage() -> None:
    """Чужая работа в чужом потоке в счёт звена не попадает.

    Ради этого и берётся время потока, а не процесса: петлевой захват живёт
    отдельным потоком, и без такого разделения он приписывался бы голосовому
    кругу.
    """
    meter = Meter()
    noisy = threading.Thread(target=burn, args=(0.05,))

    noisy.start()
    with meter.stage("имя"):
        burn(0.01)
    noisy.join()

    assert meter.peek().stages["имя"] < 0.04


def test_taking_resets_the_window() -> None:
    """Снятые показания начинают отрезок заново, а подсмотренные — нет."""
    meter = Meter()
    with meter.stage("имя"):
        burn(0.01)

    assert meter.take().stages["имя"] > 0
    assert meter.take().stages == {}


def test_disabled_meter_counts_nothing() -> None:
    """Выключенный счётчик не считает и не стоит ничего.

    Проверка флага дешевле обращения к часам, а включают его не всегда.
    """
    meter = Meter(enabled=False)
    with meter.stage("имя"):
        burn(0.01)

    assert not meter.enabled
    assert meter.peek().stages == {}


def test_external_measurements_can_be_added() -> None:
    """Замеренное снаружи тоже учитывается."""
    meter = Meter()
    meter.add("петля", 0.5)
    meter.add("петля", 0.25)

    assert meter.peek().stages["петля"] == 0.75


def test_negative_time_is_ignored() -> None:
    """Отрицательное время — признак сбоя часов, а не работы."""
    meter = Meter()
    meter.add("петля", -1.0)

    assert meter.peek().stages == {}


# --- как это читается -------------------------------------------------------


def test_shares_are_sorted_from_greediest() -> None:
    """Первым называют главного виновника: за этим и смотрят."""
    load = Load(wall=10.0, total=1.0, stages={"речь": 0.1, "имя": 0.5, "эхо": 0.05})

    assert [name for name, _ in load.shares()] == ["имя", "речь", "эхо"]
    assert load.shares()[0][1] == 0.05


def test_description_shows_total_and_the_unaccounted_rest() -> None:
    """Сумма звеньев меньше общего расхода, и остаток надо видеть.

    В него попадает всё, что звеньями не размечено: цикл событий, разбор JSON,
    сам Python. Подгонять его к нулю незачем — важно знать, что он есть.
    """
    load = Load(wall=10.0, total=1.0, stages={"имя": 0.5})
    said = load.describe()

    assert "всего 10.0% ядра" in said
    assert "имя 5.0%" in said
    assert "прочее 5.0%" in said


def test_description_without_a_rest_says_nothing_about_it() -> None:
    """Когда всё размечено, лишнего в строке нет."""
    load = Load(wall=10.0, total=0.5, stages={"имя": 0.5})
    assert "прочее" not in load.describe()


def test_empty_window_does_not_divide_by_zero() -> None:
    """Нулевой отрезок — не повод падать."""
    load = Load(wall=0.0, total=0.0, stages={})
    assert load.share == 0.0
    assert load.shares() == []
