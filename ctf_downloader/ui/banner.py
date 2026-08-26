"""PHOSPHOR FIELD KIT — banner & AppHeader (design-system spec §2, §4.1).

Banner phương án B "half-block": bitmap 5 pixel-row nén thành 3 dòng bằng
glyph ``█▀▄`` — cùng bộ glyph hình học với meter. 56 cột × 3 hàng, vừa mọi
terminal ≥ 60 col. Phương án A (slab đặc) giữ làm easter egg ``--banner big``.

AppHeader (combo B — Phosphor Radar): dải scanline ``░░▒▒▓▓`` full-width +
title ``CTF·TOOLKIT v3◢`` căn giữa trên nền dot faint + dòng trạng thái
``▍ <lệnh> · <context>`` + strip ``▸ … timestamp`` mép phải. Tối đa 4 dòng,
chiều rộng tự theo terminal; mọi màu lấy token :mod:`ui.theme` (amber CRT),
không hardcode hex ngoài hằng spec.
"""
from __future__ import annotations

from rich.text import Text

from .theme import ACCENT, ACCENT_DEEP, ACCENT_HI, FG_BASE, FG_FAINT, \
    FG_MUTED, SOLVED

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
    """AppHeader "Phosphor Radar" tối đa 4 dòng (combo B, redesign 08/2026).

    Dòng 1: dải scanline ``░░▒▒▓▓`` full-width, accent.deep (gradient đến từ
    ramp glyph nên KHÔNG cần màu mới). Dòng 2: title `` CTF·TOOLKIT v{major}◢``
    căn giữa giữa hai cánh dot faint. Dòng 3: ``▍`` solved + lệnh bold +
    context muted. Dòng 4: ``▸`` accent trái, timestamp faint sát mép phải
    (bỏ khi không có timestamp). Chiều rộng tự theo terminal qua
    :func:`shutil.get_terminal_size` trừ khi truyền ``width``.
    """
    from rich.cells import cell_len

    import shutil as _shutil

    try:
        from .. import __version__ as _ver_full
    except Exception:  # pragma: no cover - standalone import
        _ver_full = "3.0.0"
    ver = f"v{_ver_full.split('.')[0]}"

    try:
        cols = int(width or _shutil.get_terminal_size().columns)
    except Exception:
        cols = int(width or 80)
    cols = max(cols, 20)

    out = Text()

    # L1 — scanline gradient full-width (một màu deep, texture từ glyph).
    grad = "░░▒▒▓▓"
    out.append((grad * (cols // len(grad) + 1))[:cols] + "\n",
               style=ACCENT_DEEP)

    # L2 — title căn giữa theo cell_len, cánh dot faint hai bên.
    core = Text()
    core.append(" CTF·TOOLKIT ", style=f"bold {ACCENT}")
    core.append(ver, style=f"bold {ACCENT_HI}")
    core.append("◢", style=ACCENT_DEEP)
    wing = max(0, cols - cell_len(core.plain))
    left = wing // 2
    out.append("·" * left, style=FG_FAINT)
    out.append_text(core)
    out.append("·" * (wing - left), style=FG_FAINT)

    # L3 — dòng trạng thái: ▍ solved + lệnh bold + context muted.
    out.append("\n▍", style=SOLVED)
    out.append(command, style=f"bold {FG_BASE}")
    if context:
        out.append("  ·  ", style=FG_FAINT)
        out.append(context, style=FG_MUTED)

    # L4 — strip context: ▸ accent, timestamp faint đẩy sát mép phải.
    if timestamp:
        gap = max(1, cols - 1 - cell_len(timestamp))
        out.append("\n▸", style=ACCENT)
        out.append(" " * gap, style="")
        out.append(timestamp, style=FG_FAINT)
    return out


__all__ = ["banner_a", "banner_b", "tagline_text", "app_header", "TAGLINE"]
