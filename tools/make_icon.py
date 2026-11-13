"""Generate tools/ser2netweb.ico, the Windows executable icon (ser2netweb.spec).

Pure standard library, no image libraries, so the icon is reproducible from source. It
writes a classic BMP-format .ico (32-bit BGRA + 1-bpp AND mask) with the sizes Windows
asks for; PyInstaller embeds it into ser2netweb.exe. The mark is a rounded tile in the UI's
accent blue with two rounded "data line" bars — a serial-to-network bridge.

    python3 tools/make_icon.py [tools/ser2netweb.ico]
"""
from __future__ import annotations

import os
import struct
import sys

SIZES = (16, 24, 32, 48, 64, 128, 256)
# the admin UI's accent blue and panel/background (ser2netweb/web/static/app.css)
ACCENT = (0x2F, 0x81, 0xF7)
DARK = (0x12, 0x18, 0x1F)
WHITE = (0xF2, 0xF6, 0xFB)


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rounded(nx, ny, half, radius):
    """Signed-ish coverage of a rounded square centred at 0.5; >0 inside."""
    dx = abs(nx - 0.5) - (half - radius)
    dy = abs(ny - 0.5) - (half - radius)
    dx = max(dx, 0.0)
    dy = max(dy, 0.0)
    return radius - (dx * dx + dy * dy) ** 0.5


def _pixel(nx, ny):
    """RGBA for a point in the unit square (nx, ny in [0,1])."""
    tile = _rounded(nx, ny, 0.5, 0.22)
    if tile <= -0.01:
        return (0, 0, 0, 0)  # outside the tile: transparent
    # a subtle top-to-bottom shade on the tile
    base = _mix(ACCENT, DARK, 0.35 * ny)
    r, g, b = base
    a = 255
    if -0.01 < tile < 0.015:  # soft edge
        a = int(255 * (tile + 0.01) / 0.025)
        a = max(0, min(255, a))
    # two rounded "data line" bars across the middle
    for cy in (0.40, 0.60):
        if 0.20 <= nx <= 0.80 and abs(ny - cy) <= 0.052:
            end = _rounded(nx, ny, 0.30, 0.052)  # round the bar ends
            if 0.24 <= nx <= 0.76 or end > 0:
                r, g, b = WHITE
    # a node dot at each end of the top bar
    for cx in (0.24, 0.76):
        if ((nx - cx) ** 2 + (ny - 0.40) ** 2) ** 0.5 <= 0.065:
            r, g, b = WHITE
    return (r, g, b, a)


def _image(size):
    """BGRA pixel rows (top-to-bottom) for one square image."""
    rows = []
    ss = 2  # supersample for smoother edges
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    nx = (x + (sx + 0.5) / ss) / size
                    ny = (y + (sy + 0.5) / ss) / size
                    px = _pixel(nx, ny)
                    for i in range(4):
                        acc[i] += px[i]
            r, g, b, a = (c // (ss * ss) for c in acc)
            row += bytes((b, g, r, a))  # BMP is BGRA
        rows.append(bytes(row))
    return rows


def _bmp_entry(size):
    """A BITMAPINFOHEADER DIB (32-bit XOR bitmap + 1-bpp AND mask), as an .ico stores it."""
    rows = _image(size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = b"".join(rows[::-1])  # bottom-up
    and_stride = ((size + 31) // 32) * 4
    mask = b"\x00" * (and_stride * size)  # 32-bit alpha carries transparency; mask all-visible
    return header + xor + mask


def build(path):
    images = [(size, _bmp_entry(size)) for size in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))  # ICONDIR: reserved, type=icon, count
    offset = 6 + 16 * len(images)
    for size, data in images:
        w = h = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    with open(path, "wb") as fh:
        fh.write(out)
    print(f"wrote {path}: {len(images)} images, {len(out)} bytes")


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__),
                                                              "ser2netweb.ico")
    build(dest)
