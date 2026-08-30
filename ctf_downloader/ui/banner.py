"""UCS_ExOdia banner and compact AppHeader.

The old PHOSPHOR radar and duplicate hand-built bitmap banner are replaced by
one brand system from ui.brand.  Full splash is reserved for interactive entry;
ordinary commands get a compact three-line header so repeated CLI use stays
fast and uncluttered.
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from .brand import (
    BRAND_NAME,
    BRAND_SUBTITLE,
    BRAND_TAGLINE,
    compact_brand,
    full_brand,
    operation_rail,
    terminal_width,
)

TAGLINE = BRAND_SUBTITLE


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


def tagline_text() -> Text:
    return Text(TAGLINE, style="dim italic")


def _fit_plain(value: str, width: int) -> str:
    value = str(value or "")
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    if width == 1:
        return "…"
    # Current command/context payloads are overwhelmingly ASCII paths/names.
    # Keep truncation deterministic and add an ellipsis rather than wrapping.
    return value[: max(0, width - 1)] + "…"


def app_header(
    command: str,
    context: str = "",
    timestamp: str = "",
    width: int | None = None,
) -> Text:
    """Three-line UCS_ExOdia command header.

    Line 1: product + command on the left, major version on the right.
    Line 2: eight-stage segmented spectral rail (every stage its own gradient).
    Line 3: workspace/context on the left and local timestamp on the right.

    The header never wraps; narrow terminals reduce each rail segment to one
    cell and truncate context with an ellipsis.
    """
    cols = terminal_width(width)
    out = Text()

    left = Text(BRAND_NAME, style="bold #5EEAD4")
    left.append(" // ", style="dim")
    left.append(str(command or "console"), style="bold")
    ver = _major_version()
    gap = max(1, cols - cell_len(left.plain) - cell_len(ver))
    if cell_len(left.plain) + 1 + cell_len(ver) > cols:
        left = Text(_fit_plain(left.plain, max(1, cols - cell_len(ver) - 1)), style="bold")
        gap = 1
    out.append_text(left)
    out.append(" " * gap)
    out.append(ver, style="bold #C084FC")
    out.append("\n")

    sep = " " if cols < 60 else "  "
    available = max(8, cols - (7 * cell_len(sep)))
    cells = max(1, min(4, available // 8))
    rail = operation_rail(cells_per_stage=cells, separator=sep)
    rail_pad = max(0, cols - cell_len(rail.plain))
    out.append(" " * (rail_pad // 2))
    out.append_text(rail)
    out.append("\n")

    stamp = str(timestamp or "")
    ctx_budget = cols - (cell_len(stamp) + 2 if stamp else 0)
    ctx = _fit_plain(str(context or BRAND_TAGLINE), max(1, ctx_budget))
    out.append(ctx, style="dim")
    if stamp:
        gap = max(1, cols - cell_len(ctx) - cell_len(stamp))
        out.append(" " * gap)
        out.append(stamp, style="dim")
    return out


__all__ = ["banner_a", "banner_b", "tagline_text", "app_header", "TAGLINE"]
