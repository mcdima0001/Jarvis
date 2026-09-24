"""Копия интерпретатора: «Jarvis» вместо «Python» и свой значок.

Повод 24.09.2026: владелец прислал снимок диспетчера задач — подпись уже
«Jarvis», а картинка рядом с ней питоновская. Подпись и значок берутся из
**ресурсов одного и того же файла**, и переписывали мы только первую.

Правится ресурс на Windows, а проверяется здесь то, что от Windows не зависит:
пересборка файла значков в опись, по которой система выбирает размер. Ошибка в
ней не падает, а тихо показывает не тот значок или ни одного — то есть ровно
то, что заметит только глаз.
"""

from __future__ import annotations

import struct

import pytest

from launcher.host import GRPICONDIRENTRY, ICONDIR, ICONDIRENTRY, icon_group


def _ico(images: list[bytes]) -> bytes:
    """Собрать файл значков из готовых картинок — как это делает Pillow."""
    head = ICONDIR.pack(0, 1, len(images))
    offset = len(head) + len(images) * ICONDIRENTRY.size
    entries = b""
    for number, image in enumerate(images):
        side = 16 * (number + 1)
        entries += ICONDIRENTRY.pack(side, side, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return head + entries + b"".join(images)


def test_the_group_points_at_every_picture_by_number() -> None:
    """Опись ссылается на ресурсы номерами, а файл — смещениями: в этом и разница."""
    images = [b"\x01" * 40, b"\x02" * 70, b"\x03" * 100]
    group, found = icon_group(_ico(images))

    assert found == images, "картинки вынуты целиком и по порядку"
    _, kind, count = ICONDIR.unpack_from(group)
    assert (kind, count) == (1, 3)
    for number in range(count):
        width, _, _, _, _, _, size, resource = GRPICONDIRENTRY.unpack_from(
            group, ICONDIR.size + number * GRPICONDIRENTRY.size
        )
        assert width == 16 * (number + 1), "размер значка переносится как есть"
        assert size == len(images[number])
        assert resource == number + 1, "нумерация с единицы — под ней ресурс и пишется"


def test_a_group_entry_is_two_bytes_shorter_than_a_file_entry() -> None:
    """Смещение картинки занимает четыре байта, её номер — два.

    Перепутать легко, а последствие тихое: система прочитает опись со сдвигом и
    покажет мусор вместо значка.
    """
    assert ICONDIRENTRY.size - GRPICONDIRENTRY.size == 2


def test_something_that_is_not_an_icon_file_is_refused() -> None:
    """Лучше отказ при сборке, чем exe без значка."""
    with pytest.raises(ValueError):
        icon_group(b"")
    with pytest.raises(ValueError):
        icon_group(struct.pack("<HHH", 0, 2, 1) + b"\x00" * 16)  # тип 2 — это курсор
    with pytest.raises(ValueError):
        icon_group(struct.pack("<HHH", 0, 1, 0))  # ни одной картинки


def test_a_truncated_picture_is_noticed() -> None:
    """Обрезанный значок молча уехал бы в ресурс и не нарисовался."""
    with pytest.raises(ValueError):
        icon_group(_ico([b"\x01" * 40])[:-10])
