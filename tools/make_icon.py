"""Нарисовать значки Jarvis: голубое кольцо «готов» и янтарное «загружается».

    python tools/make_icon.py

Рисуются кодом, а не берутся картинкой: так цвет и толщина правятся одной
строкой, а в репозитории нет чужого рисунка. Каждый размер рисуется отдельно
с запасом в четыре раза и уменьшается — иначе на 16 точках трея тонкое кольцо
рассыпается в мусор.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "jarvis" / "core" / "tray"
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
SUPERSAMPLE = 4

#: (кольцо, свечение и ядро)
PALETTES = {
    "jarvis.ico": ((64, 196, 255), (190, 240, 255)),
    "jarvis-busy.ico": ((255, 164, 40), (255, 222, 150)),
}


def _box(center: float, radius: float) -> tuple[float, float, float, float]:
    return (center - radius, center - radius, center + radius, center + radius)


def draw(size: int, ring: tuple[int, int, int], glow: tuple[int, int, int]) -> Image.Image:
    """Один размер значка: тёмный диск, кольцо, дуга и светящееся ядро."""
    big = size * SUPERSAMPLE
    c = big / 2
    small = size <= 24
    # Толщина растёт на мелких размерах: иначе кольцо тоньше точки.
    width = max(1, round(big * (0.12 if small else 0.075)))

    lines = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(lines)
    pen.ellipse(_box(c, big * 0.40), outline=(*ring, 255), width=width)
    if not small:
        pen.arc(_box(c, big * 0.27), start=200, end=340, fill=(*glow, 255), width=round(width * 0.9))
        pen.arc(_box(c, big * 0.27), start=20, end=110, fill=(*ring, 200), width=round(width * 0.6))
    if size >= 48:
        for tick in range(48):
            angle = 2 * math.pi * tick / 48
            inner, outer = big * 0.445, big * 0.475
            pen.line(
                (c + inner * math.cos(angle), c + inner * math.sin(angle),
                 c + outer * math.cos(angle), c + outer * math.sin(angle)),
                fill=(*ring, 170), width=max(1, round(big * 0.008)),
            )
    pen.ellipse(_box(c, big * (0.15 if small else 0.11)), fill=(*glow, 255))

    # Свечение — размытая копия линий под ними.
    halo = lines.filter(ImageFilter.GaussianBlur(big * 0.04))

    picture = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(picture).ellipse(_box(c, big * 0.49), fill=(6, 16, 26, 235))
    picture.alpha_composite(halo)
    picture.alpha_composite(halo)
    picture.alpha_composite(lines)
    return picture.resize((size, size), Image.Resampling.LANCZOS)


#: PNG для окна панели. Edge берёт значок окна и кнопки на панели задач из
#: страницы, и 16 точек из ICO он растягивал в мыло — нужны крупные.
PNG_SIZES = (32, 48, 64, 128, 256)


def main() -> None:
    ring, glow = PALETTES["jarvis.ico"]
    for size in PNG_SIZES:
        draw(size, ring, glow).save(OUT / f"jarvis-{size}.png", format="PNG", optimize=True)
    print(f"{OUT}: jarvis-{{{','.join(map(str, PNG_SIZES))}}}.png")
    for name, (ring, glow) in PALETTES.items():
        frames = [draw(size, ring, glow) for size in SIZES]
        largest = frames[-1]
        largest.save(
            OUT / name,
            format="ICO",
            sizes=[(size, size) for size in SIZES],
            append_images=frames[:-1],
        )
        print(f"{OUT / name}: {', '.join(map(str, SIZES))}")


if __name__ == "__main__":
    main()
