"""Учёт процессорного времени по звеньям.

Жалоба «ноутбук греется» сама по себе не указывает на виновного: в голосовом
круге непрерывно работают четыре вещи сразу. Здесь проверяется, что счёт
раскладывается по звеньям честно и что выключенный счётчик не стоит ничего.
"""

from __future__ import annotations

import logging
import threading
import time

from jarvis.core.meter import SUSPICIOUS_S, Load, Meter


def burn(seconds: float) -> None:
    """Занять процессор на заданное время, а не поспать."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
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


def test_wrapped_waiting_is_complained_about(caplog) -> None:
    """Ожидание внутри звена не запретить, но и молчать о нём нельзя.

    Раньше защитой была сама механика: процессорное время потока ожидание не
    считало. Но у тех часов на Windows шаг 15.6 мс, и звено в доли миллисекунды
    измерялось нулём — счёт занижался в 2.4 раза (замер 12.09.2026). Настоящие
    часы это чинят, но ожидание теперь засчитают, поэтому правило «оборачивать
    только счёт» обзавелось предохранителем: слишком долгий замер виден в логе.
    """
    meter = Meter()
    with caplog.at_level(logging.WARNING, logger="jarvis.core.meter"):
        with meter.stage("сеть"):
            time.sleep(SUSPICIOUS_S + 0.05)
        with meter.stage("сеть"):
            time.sleep(SUSPICIOUS_S + 0.05)

    assert "сеть" in caplog.text and "ожидание" in caplog.text
    assert caplog.text.count("похоже") == 1, "о каждом звене предупреждаем один раз"


def test_short_stages_are_silent(caplog) -> None:
    """Обычное звено работает доли миллисекунды и в лог не лезет."""
    meter = Meter()
    with caplog.at_level(logging.WARNING, logger="jarvis.core.meter"):
        for _ in range(50):
            with meter.stage("имя"):
                burn(0.001)

    assert not caplog.text


def test_other_threads_do_not_leak_into_the_stage() -> None:
    """Чужая работа в чужом потоке в счёт звена не попадает.

    Звенья названы по отдельности и меряются каждое у себя, поэтому петлевой
    захват, живущий своим потоком, голосовому кругу не приписывается.
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


# --- сеанс целиком ----------------------------------------------------------


def test_session_survives_taking_the_window() -> None:
    """Снятый отрезок не обнуляет счёт сеанса.

    Пока их не различали, прощальная строка «Нагрузка за сеанс» показывала
    последнюю минуту: отчёт раз в минуту звал `take`, а на выходе спрашивали
    текущий отрезок — то есть остаток после последнего сброса.
    """
    meter = Meter()
    for _ in range(3):
        with meter.stage("имя"):
            burn(0.01)
        meter.take()

    assert meter.peek().stages == {}, "отрезок обязан обнуляться"
    assert meter.session().stages["имя"] >= 0.025, "а сеанс — копиться"


def test_session_counts_the_open_window_too() -> None:
    """Незакрытый отрезок входит в сеанс: иначе теряется последняя минута.

    А это ровно та минута, после которой обычно и выключают.
    """
    meter = Meter()
    with meter.stage("имя"):
        burn(0.01)
    meter.take()
    with meter.stage("имя"):
        burn(0.01)

    assert meter.session().stages["имя"] >= 0.018


def test_peak_remembers_the_heaviest_window() -> None:
    """Средняя по вечеру прячет всплески, а греется ноутбук на них.

    Проверяется само свойство «пик не убывает», а не конкретное число: общий
    расход считается по всему процессу, и чужие потоки в тихий отрезок могут
    попасть какие угодно.
    """
    meter = Meter()
    assert meter.peak == 0.0

    # Пик — это максимум по закрытым отрезкам, и проверяется он точно, без
    # опоры на часы: у процессорного времени на Windows шаг 15.6 мс, и «сколько
    # намерил короткий отрезок» — величина случайная.
    shares = []
    for _ in range(3):
        burn(0.03)
        shares.append(meter.take().share)
        time.sleep(0.01)
        shares.append(meter.take().share)

    assert meter.peak == max(shares)


# --- ядро против процессора --------------------------------------------------


def test_core_share_is_not_the_machine_share() -> None:
    """Доля ядра и доля процессора — разные числа, и путать их нельзя.

    Спор про нагрев решается именно этим: семь процентов ядра на двенадцати
    ядрах — меньше процента машины, то есть заведомо не источник жара.
    """
    load = Load(wall=10.0, total=0.7, stages={}, cores=12)

    assert round(load.share * 100) == 7
    assert round(load.machine_share * 100, 1) == 0.6
    assert "процессора" in load.describe()


def test_single_core_machine_says_nothing_extra() -> None:
    """На одном ядре два числа совпадают, и второе только мешало бы."""
    load = Load(wall=10.0, total=0.7, stages={}, cores=1)

    assert load.machine_share == load.share
    assert "процессора" not in load.describe()


def test_recent_falls_back_to_the_last_finished_window() -> None:
    """Спросили сразу после сводки — отвечаем последней закрытой минутой.

    Сеанс тут не годится: в него входит запуск с загрузкой моделей, и он один
    перевешивает часы тихой работы (замер 12.09.2026: 27% против 7%).
    """
    meter = Meter()
    with meter.stage("имя"):
        burn(0.02)
    finished = meter.take()

    recent = meter.recent(least=10.0)
    assert recent.stages == finished.stages
    assert recent.wall == finished.wall


def test_recent_prefers_the_window_it_has() -> None:
    """Набралось достаточно — отвечаем текущим отрезком, он свежее."""
    meter = Meter()
    with meter.stage("имя"):
        burn(0.01)

    recent = meter.recent(least=0.0)
    assert recent.stages["имя"] >= 0.008


def test_recent_without_history_uses_the_session() -> None:
    """В первую минуту закрытых отрезков ещё нет — берём что есть."""
    meter = Meter()
    with meter.stage("имя"):
        burn(0.01)

    assert meter.recent(least=1000.0).stages["имя"] >= 0.008
