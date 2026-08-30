"""UCS_ExOdia brand renderer for terminal surfaces.

The brand layer is dependency-free beyond Rich. The large logo is embedded
as approved text art; FIGlet is never invoked at runtime. Color is applied
per display cell so non-TTY output keeps the same readable glyphs with ANSI
stripped automatically.
"""
from __future__ import annotations

import shutil

from rich.cells import cell_len
from rich.text import Text

from .widgets import gradient

BRAND_NAME = "UCS_ExOdia"
BRAND_TAGLINE = "CTF OPERATIONS FRAMEWORK"
BRAND_SUBTITLE = "capture · workspace · submit · monitor · automate"

OPERATIONS: tuple[str, ...] = (
    "detect",
    "pull",
    "workspace",
    "submit",
    "watch",
    "sniper",
    "rank",
    "automate",
)

# The O in ExOdia is the canonical round letter form, never the slashed zero.
FULL_LOGO = (
    "██╗   ██╗ ██████╗███████╗        ███████╗██╗  ██╗ ██████╗ ██████╗ ██╗ █████╗",
    "██║   ██║██╔════╝██╔════╝        ██╔════╝╚██╗██╔╝██╔═══██╗██╔══██╗██║██╔══██╗",
    "██║   ██║██║     ███████╗        █████╗   ╚███╔╝ ██║   ██║██║  ██║██║███████║",
    "██║   ██║██║     ╚════██║        ██╔══╝   ██╔██╗ ██║   ██║██║  ██║██║██╔══██║",
    "╚██████╔╝╚██████╗███████║███████╗███████╗██╔╝ ██╗╚██████╔╝██████╔╝██║██║  ██║",
    " ╚═════╝  ╚═════╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝╚═╝  ╚═╝",
)
FULL_LOGO_WIDTH = max(cell_len(line) for line in FULL_LOGO)
WIDE_THRESHOLD = 80

# Brand-only spectral palette. Product semantic colors remain in ui.theme.
LOGO_START = (0x5E, 0xEA, 0xD4)
LOGO_MID = (0x60, 0xA5, 0xFA)
LOGO_END = (0xC0, 0x84, 0xFC)

# Every operation owns its own micro-gradient. Adjacent endpoints overlap or
# approach one another so all eight segments still read as one transition.
OPERATION_RAMPS: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((0x5E, 0xEA, 0xD4), (0x22, 0xD3, 0xEE)),
    ((0x22, 0xD3, 0xEE), (0x38, 0xBD, 0xF8)),
    ((0x38, 0xBD, 0xF8), (0x60, 0xA5, 0xFA)),
    ((0x60, 0xA5, 0xFA), (0x81, 0x8C, 0xF8)),
    ((0x81, 0x8C, 0xF8), (0xA7, 0x8B, 0xFA)),
    ((0xA7, 0x8B, 0xFA), (0xC0, 0x84, 0xFC)),
    ((0xC0, 0x84, 0xFC), (0xE8, 0x79, 0xF9)),
    ((0xE8, 0x79, 0xF9), (0xFB, 0xBF, 0x24)),
)


def _style(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def terminal_width(width: int | None) -> int:
    if width is not None:
        return max(20, int(width))
    try:
        return max(20, int(shutil.get_terminal_size().columns))
    except Exception:
        return WIDE_THRESHOLD


def _append_centered(out: Text, value: Text | str, width: int) -> None:
    """Center one renderable with Rich's display-cell aware alignment."""
    text = value.copy() if isinstance(value, Text) else Text(str(value))
    text.align("center", width)
    text.rstrip()
    out.append_text(text)


def _append_centered_block_line(
    out: Text,
    value: Text | str,
    *,
    block_width: int,
    viewport_width: int,
) -> None:
    """Center a fixed-width block while keeping every row on one left edge.

    Rich performs both alignment passes: first normalize this row to the
    logo's intrinsic block width, then center that whole block in the current
    terminal viewport.  This avoids the classic 76-vs-77-cell first-row drift.
    """
    text = value.copy() if isinstance(value, Text) else Text(str(value))
    text.align("left", block_width)
    text.align("center", viewport_width)
    text.rstrip()
    out.append_text(text)


def gradient_text(
    value: str,
    start: tuple[int, int, int] = LOGO_START,
    mid: tuple[int, int, int] = LOGO_MID,
    end: tuple[int, int, int] = LOGO_END,
) -> Text:
    """Apply a smooth horizontal gradient to visible cells."""
    out = Text()
    width = max(1, cell_len(value))
    colors = gradient(start, mid, end, steps=max(2, width))
    pos = 0
    for ch in value:
        if ch.isspace():
            out.append(ch)
        else:
            out.append(ch, style=f"bold {_style(colors[min(len(colors) - 1, pos)])}")
        pos += max(1, cell_len(ch))
    return out


def operation_rail(
    *,
    cells_per_stage: int = 4,
    separator: str = "  ",
    glyph: str = "▰",
) -> Text:
    """Eight-stage brand rail with an independent gradient in every stage."""
    cells = max(1, int(cells_per_stage))
    out = Text()
    for index, (start, end) in enumerate(OPERATION_RAMPS):
        colors = gradient(start, None, end, steps=max(2, cells))
        for i in range(cells):
            out.append(glyph, style=f"bold {_style(colors[min(i, len(colors) - 1)])}")
        if index < len(OPERATION_RAMPS) - 1:
            out.append(separator)
    return out


def operation_labels(width: int = FULL_LOGO_WIDTH) -> Text:
    line = "   ".join(OPERATIONS)
    if cell_len(line) > width:
        line = "  ".join(("DET", "PULL", "WORK", "SUB", "WATCH", "SNP", "RANK", "AUTO"))
    return Text(line, style="dim")


def compact_brand(width: int | None = None, *, command: str = "", version: str = "") -> Text:
    """Compact three-line brand for ordinary subcommands and narrow terminals."""
    cols = terminal_width(width)
    out = Text()

    left = Text(BRAND_NAME, style=f"bold {_style(LOGO_START)}")
    if command:
        left.append(" // ", style="dim")
        left.append(command, style="bold")
    right = str(version or "")
    gap = max(1, cols - cell_len(left.plain) - cell_len(right))
    out.append_text(left)
    out.append(" " * gap)
    if right:
        out.append(right, style=f"bold {_style(LOGO_END)}")
    out.append("\n")

    sep = " " if cols < 60 else "  "
    available = max(8, cols - (len(OPERATIONS) - 1) * cell_len(sep))
    cells = max(1, min(4, available // len(OPERATIONS)))
    rail = operation_rail(cells_per_stage=cells, separator=sep)
    _append_centered(out, rail, cols)
    out.append("\n")

    subtitle = BRAND_TAGLINE if cell_len(BRAND_TAGLINE) <= cols else "CTF OPS"
    _append_centered(out, Text(subtitle, style="dim"), cols)
    return out


def full_brand(width: int | None = None, *, version: str = "") -> Text:
    """Full brutalist UCS_ExOdia splash for terminals at least 80 columns."""
    cols = terminal_width(width)
    if cols < WIDE_THRESHOLD:
        return compact_brand(cols, version=version)

    canvas = min(cols, FULL_LOGO_WIDTH)
    out = Text()
    for idx, line in enumerate(FULL_LOGO):
        _append_centered_block_line(
            out,
            gradient_text(line),
            block_width=FULL_LOGO_WIDTH,
            viewport_width=cols,
        )
        if idx < len(FULL_LOGO) - 1:
            out.append("\n")

    out.append("\n\n")
    brand = Text(BRAND_NAME, style=f"bold {_style(LOGO_MID)}")
    if version:
        brand.append(f"  {version}", style="dim")
    _append_centered(out, brand, cols)

    out.append("\n\n")
    indent = max(0, (cols - canvas) // 2)
    out.append(" " * indent)
    out.append(BRAND_TAGLINE, style="bold")
    out.append("\n")
    out.append(" " * indent)
    out.append("─" * canvas, style="dim")
    out.append("\n")
    _append_centered(out, operation_labels(canvas), cols)
    out.append("\n")
    _append_centered(out, operation_rail(cells_per_stage=4, separator="  "), cols)
    out.append("\n")
    out.append(" " * indent)
    out.append("─" * canvas, style="dim")
    out.append("\n")
    out.append(" " * indent)
    out.append("READY", style="bold")
    out.append(" ●", style="#62C97E")
    return out


def splash(width: int | None = None, *, version: str = "") -> Text:
    cols = terminal_width(width)
    if cols >= WIDE_THRESHOLD:
        return full_brand(cols, version=version)
    return compact_brand(cols, version=version)


__all__ = [
    "BRAND_NAME",
    "BRAND_TAGLINE",
    "BRAND_SUBTITLE",
    "OPERATIONS",
    "FULL_LOGO",
    "FULL_LOGO_WIDTH",
    "WIDE_THRESHOLD",
    "OPERATION_RAMPS",
    "operation_rail",
    "operation_labels",
    "compact_brand",
    "full_brand",
    "splash",
    "terminal_width",
]
