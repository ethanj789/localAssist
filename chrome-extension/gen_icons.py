"""Generate simple solid-color rounded icons for the extension."""
import struct
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).parent / "icons"
ICON_DIR.mkdir(exist_ok=True)

BG = (124, 140, 255)      # accent (#7c8cff)
DOT = (240, 242, 255)     # near-white


def _rounded(x, y, w, h, r):
    """Return True if pixel (x,y) is inside a rounded rect of radius r."""
    if r <= 0:
        return True
    cx = min(max(x, r), w - 1 - r)
    cy = min(max(y, r), h - 1 - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def make_png(size: int) -> bytes:
    r = max(2, size // 5)
    dot_cx, dot_cy = size * 0.5, size * 0.5
    dot_r = size * 0.17
    rows = []
    for y in range(size):
        row = bytearray()
        row.append(0)  # filter type 0 (none)
        for x in range(size):
            if not _rounded(x, y, size, size, r):
                row += bytes((0, 0, 0, 0))
                continue
            ddx = x + 0.5 - dot_cx
            ddy = y + 0.5 - dot_cy
            if ddx * ddx + ddy * ddy <= dot_r * dot_r:
                row += bytes((*DOT, 255))
            else:
                row += bytes((*BG, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


for s in (16, 32, 48, 128):
    (ICON_DIR / f"icon{s}.png").write_bytes(make_png(s))
    print(f"wrote icons/icon{s}.png")
