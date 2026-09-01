"""UCS_ExOdia banner and compact AppHeader.

The old PHOSPHOR radar and duplicate hand-built bitmap banner are replaced by
one brand system from ui.brand.  Full splash is reserved for interactive entry;
ordinary commands get a single-line header so repeated CLI use stays
fast and uncluttered.
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from .brand import (
    BRAND_NAME,
    compact_brand,
    full_brand,
    operation_rail,
    terminal_width,
)
from .theme import ACCENT, FG_MUTED


def _major_version() -> str:
    try:
        from .. import __version__ as ver
    except Exception:
        ver = "3.0.0"
    return f"v{ver.split('.')[0]}"


def banner_b(width: int | None = None) -> Text:
    """Compact UCS_ExOdia identity block used by help/logger surfaces."""
    return compact_brand(width, version=_major_version())


def banner_a(width: int | None = None) -> Text:
    """Full brutalist UCS_ExOdia identity block."""
    return full_brand(width, version=_major_version())


def app_header(
    command: str,
    context: str = "",
    timestamp: str = "",
    width: int | None = None,
) -> Text:
    """Single-line UCS_ExOdia command header with an inline spectral rail."""
    cols = terminal_width(width)
    ver = _major_version()

    left = Text(BRAND_NAME, style=f"bold {ACCENT}")
    left.append(" // ", style="dim")
    left.append(str(command or "console"), style="bold")
    ctx = str(context or "").strip()
    if ctx:
        left.append(" · ", style="dim")
        left.append(ctx, style="dim")

    cells = 1 if cols < 60 else 2 if cols < 100 else 3
    rail = operation_rail(cells_per_stage=cells, separator=" ")

    right = Text()
    stamp = str(timestamp or "").strip()
    if stamp and cols >= 100:
        right.append(stamp, style="dim")
        right.append(" · ", style="dim")
    right.append(ver, style=f"bold {FG_MUTED}")

    rail_w = cell_len(rail.plain)
    right_w = cell_len(right.plain)
    left_budget = max(1, cols - rail_w - right_w - 2)
    if cell_len(left.plain) > left_budget:
        left.truncate(left_budget, overflow="ellipsis", pad=False)

    left_w = cell_len(left.plain)
    free = max(2, cols - left_w - rail_w - right_w)
    out = Text()
    out.append_text(left)
    out.append(" ")
    out.append_text(rail)
    out.append(" " * max(1, free - 1))
    out.append_text(right)
    return out


__all__ = ["banner_a", "banner_b", "app_header"]
