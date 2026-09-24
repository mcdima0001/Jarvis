r"""Сколько на самом деле длится блютуз-подключение и что возвращают службы.

Повод 24.09.2026: «включи Bluetooth и подключись к JBL» ответило «JBL Flip 6 не
отозвалось», а колонка подключилась через три-четыре секунды. Скилл судил об
успехе по коду возврата `BluetoothSetServiceState`, и код этот соврал.

    C:\Python314\python.exe tools/bluetooth_bench.py --device "JBL Flip 6"
    C:\Python314\python.exe tools/bluetooth_bench.py --device "JBL Flip 6" --runs 3

Стенд делает ровно то, что делает скилл, и рядом с этим **смотрит на правду**:
опрашивает список устройств, пока `fConnected` не сменится. Печатает три вещи —
что вернули службы, через сколько устройство действительно подключилось и
сколько времени скилл потерял бы, отвечая по коду возврата.

Устройство приходится **разрывать и восстанавливать**: измерить подключение, не
отключившись, нельзя. Исходное состояние возвращается в конце в любом случае —
и когда замер не удался, и когда его прервали.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
#: Предел ожидания в замере — заведомо больше того, что ищем.
LIMIT_S = 20.0
#: Как часто спрашивать состояние. Опрос дешёвый: перечисление сопряжённых.
EVERY_S = 0.25


def _module() -> Any:
    """Тот же модуль, которым пользуется скилл, — без поднятия всей системы."""
    spec = importlib.util.spec_from_file_location(
        "jarvis_bench.bluetooth", ROOT / "skills" / "windows" / "bluetooth.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("Не нашёл skills/windows/bluetooth.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _find(bt: Any, name: str) -> Any:
    for device in bt.devices():
        if device.name.strip().lower() == name.strip().lower():
            return device
    known = ", ".join(repr(device.name) for device in bt.devices())
    raise SystemExit(f"Среди сопряжённых нет {name!r}. Есть: {known}")


def _await_state(bt: Any, name: str, wanted: bool) -> float | None:
    """Сколько прошло до нужного состояния; None — не дождались."""
    started = time.monotonic()
    while time.monotonic() - started < LIMIT_S:
        device = next((item for item in bt.devices() if item.name == name), None)
        if device is not None and device.connected == wanted:
            return time.monotonic() - started
        time.sleep(EVERY_S)
    return None


def _switch(bt: Any, device: Any, connect: bool) -> tuple[int, float]:
    """Дёрнуть службы, как это делает скилл: сколько приняло и за сколько."""
    started = time.monotonic()
    accepted = bt.set_connected(device.address, connect)
    return accepted, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="название сопряжённого устройства")
    parser.add_argument("--runs", type=int, default=1, help="сколько раз повторить")
    args = parser.parse_args()

    bt = _module()
    if bt.radio() != "On":
        return int(print("Радио выключено — включи блютуз и повтори.") or 1)

    device = _find(bt, args.device)
    was = device.connected
    print(f"{device.name}: сейчас {'подключено' if was else 'отключено'}")

    connects: list[float] = []
    try:
        for run in range(1, args.runs + 1):
            if next(item for item in bt.devices() if item.name == device.name).connected:
                accepted, spent = _switch(bt, device, False)
                gone = _await_state(bt, device.name, False)
                print(f"\nзаход {run}: отключение — служб приняло {accepted}, вызов {spent:.2f} с, "
                      f"отключилось через {gone:.2f} с" if gone is not None
                      else f"\nзаход {run}: отключение не случилось за {LIMIT_S:.0f} с")
                time.sleep(1.0)  # дать стеку улечься, иначе меряем хвост отключения

            accepted, spent = _switch(bt, device, True)
            came = _await_state(bt, device.name, True)
            if came is None:
                print(f"заход {run}: подключение — служб приняло {accepted}, "
                      f"за {LIMIT_S:.0f} с так и не подключилось")
                continue
            connects.append(came)
            print(f"заход {run}: подключение — служб приняло {accepted}, вызов вернулся за {spent:.2f} с, "
                  f"устройство подключилось через {came:.2f} с")
            if accepted == 0:
                print("           ↑ вот она, ошибка: служб приняло ноль, а устройство подключилось")
    finally:
        now = next((item for item in bt.devices() if item.name == device.name), None)
        if now is not None and now.connected != was:
            print(f"\nвозвращаю как было: {'подключаю' if was else 'отключаю'} {device.name}")
            bt.set_connected(device.address, was)
            _await_state(bt, device.name, was)

    if connects:
        connects.sort()
        middle = connects[len(connects) // 2]
        print(f"\nПодключение: медиана {middle:.2f} с, худшее {connects[-1]:.2f} с, заходов {len(connects)}")
        print("Столько скилл и обязан ждать, прежде чем говорить «не отозвалось».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
