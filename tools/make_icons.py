"""Иконки приложения — рисуются кодом ОДИН раз: python3 -m tools.make_icons.

Тёмный скруглённый квадрат, золотая растущая ломаная (как на графике) и
точка на конце. Рисуется со сглаживанием (4×4 подвыборки на пиксель) и
кодируется в PNG вручную: Pillow в зависимостях нет, а тащить его ради
трёх картинок незачем. Результат кэшируется.
"""
import struct
import zlib
from functools import lru_cache
from typing import List, Tuple

BG = (13, 17, 23)
GOLD = (240, 185, 11)
_PTS = [(0.18, 0.70), (0.38, 0.52), (0.52, 0.62), (0.80, 0.30)]


def _seg_dist(px: float, py: float, a: Tuple[float, float], b: Tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    qx, qy = ax + t * dx, ay + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def _coverage(u: float, v: float) -> Tuple[float, float]:
    """(внутри квадрата, на линии) для точки в долях от размера."""
    r = 0.18                                  # радиус скругления
    cx = min(max(u, r), 1 - r)
    cy = min(max(v, r), 1 - r)
    inside = ((u - cx) ** 2 + (v - cy) ** 2) ** 0.5 <= r
    line = min(_seg_dist(u, v, a, b) for a, b in zip(_PTS, _PTS[1:])) <= 0.045
    dot = ((u - _PTS[-1][0]) ** 2 + (v - _PTS[-1][1]) ** 2) ** 0.5 <= 0.08
    return float(inside), float(inside and (line or dot))


def _png(width: int, rows: List[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack("!I", len(data)) + tag + data
                + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", width, len(rows), 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


@lru_cache(maxsize=8)
def icon_png(size: int, opaque: bool = True) -> bytes:
    """PNG size×size, RGBA. opaque=True заливает углы фоном: iOS не
    поддерживает прозрачность в иконке на экране «Домой» и рисует её чёрной."""
    n = 4
    rows: List[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            box = ln = 0.0
            for sy in range(n):
                for sx in range(n):
                    a, b = _coverage((x + (sx + 0.5) / n) / size,
                                     (y + (sy + 0.5) / n) / size)
                    box += a
                    ln += b
            box /= n * n
            ln /= n * n
            col = [BG[i] * (1 - ln / max(box, 1e-9)) + GOLD[i] * (ln / max(box, 1e-9))
                   if box else BG[i] for i in range(3)]
            alpha = 255 if opaque else int(round(255 * box))
            row += bytes(int(round(c)) for c in col) + bytes([alpha])
        rows.append(bytes(row))
    return _png(size, rows)


SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}

if __name__ == "__main__":
    import os
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    for name, size in SIZES.items():
        with open(os.path.join(out, name), "wb") as f:
            f.write(icon_png(size))
        print(name, size)
