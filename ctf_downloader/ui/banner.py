"""PHOSPHOR FIELD KIT — banner & AppHeader (design-system spec §2, §4.1).

Banner phương án B "half-block": bitmap 5 pixel-row nén thành 3 dòng bằng
glyph ``█▀▄`` — cùng bộ glyph hình học với meter. 56 cột × 3 hàng, vừa mọi
terminal ≥ 60 col. Phương án A (slab đặc) giữ làm easter egg ``--banner big``.

Render thuần qua rich Text với token màu từ :mod:`ui.theme` — không hardcode
hex ngoài hằng spec.
"""
from __future__ import annotations

from rich.text import Text

from .theme import ACCENT, FG_BASE, FG_FAINT, FG_MUTED, INFO

# Bitmap chữ vẽ tay ('#'/'.', 5 hàng) — KHÔNG dùng figlet font có sẵn.
_PIX: dict[str, list[str]] = {
    "C": ["###..", "#....", "#....", "#....", "###.."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#.."],
    "F": ["#####", "#....", "####.", "#....", "#...."],
    "-": [".....", ".....", ".###.", ".....", "....."],
    "O": [".###.", "#...#", "#...#", "#...#", ".###."],
    "L": ["#....", "#....", "#....", "#....", "#####"],
    "K": ["#...#", "#..#.", "#.#..", "#..#.", "#...#"],
    "I": ["###", ".#.", ".#.", ".#.", "###"],
}

#: Tagline in ngay dưới banner (italic fg.muted, căn trái).
TAGLINE = "bộ kit tác chiến capture-the-flag — ngay trong terminal"


def _halfblock_rows(bits: list[str]) -> list[str]:
    """5 pixel-rows -> 3 display rows bằng half-block ``(r1,r2)(r3,r4)(r5,-)``."""
    out = []
    for top, bot in ((0, 1), (2, 3), (4, None)):
        line = ""
        for x in range(len(bits[0])):
            t = bits[top][x] == "#"
            b = bits[bot][x] == "#" if bot is not None else False
            line += "█" if t and b else "▀" if t else "▄" if b else " "
        out.append(line)
    return out


def _word_rows(word: str, compress: bool) -> list[str]:
    rows = [""] * (3 if compress else 5)
    for i, ch in enumerate(word):
        bits = _PIX[ch]
        piece = _halfblock_rows(bits) if compress else [
            "".join("█" if b == "#" else " " for b in bits[y]) for y in range(5)
        ]
        for r in range(len(rows)):
            rows[r] += piece[r]
            if i < len(word) - 1:
                rows[r] += " "
    return [r.rstrip() for r in rows]


def banner_b() -> Text:
    """Banner chính — half-block 3 hàng, amber."""
    t = Text()
    for row in _word_rows("CTF-TOOLKIT", compress=True):
        t.append(row + "\n", style=ACCENT)
    return t


def banner_a() -> Text:
    """Phương án A slab đặc 5 hàng (easter egg ``ctf --banner big``)."""
    t = Text()
    for row in _word_rows("CTF-TOOLKIT", compress=False):
        t.append(row + "\n", style=ACCENT)
    return t


def tagline_text() -> Text:
    return Text(TAGLINE, style=f"italic {FG_MUTED}")


def app_header(command: str, context: str = "", timestamp: str = "",
               width: int | None = None) -> Text:
    """AppHeader 1 dòng cho lệnh thường (spec §4.1).

    Brand block amber + tên tool bold, separator ``│`` faint, lệnh ``info``,
    context ``fg.muted``, timestamp faint đẩy sát mép phải.
    """
    from rich.cells import cell_len

    t = Text()
    t.append("▐██", style=ACCENT)
    t.append(" CTF·TOOLKIT", style=f"bold {FG_BASE}")
    t.append("  │  ", style=FG_FAINT)
    t.append(command, style=INFO)
    if context:
        t.append(" · ", style=FG_FAINT)
        t.append(context, style=FG_MUTED)
    if timestamp:
        import shutil as _shutil
        try:
            cols = width or _shutil.get_terminal_size().columns
        except Exception:
            cols = width or 80
        pad = max(1, cols - cell_len(t.plain) - cell_len(timestamp))
        t.append(" " * pad, style="")
        t.append(timestamp, style=FG_FAINT)
    return t


__all__ = ["banner_a", "banner_b", "tagline_text", "app_header", "TAGLINE"]
