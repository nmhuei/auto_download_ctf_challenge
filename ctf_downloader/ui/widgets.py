"""Pure UI widget layer inspired by btop++ draw routines.

These helpers are self-contained: they accept explicit RGB tuples /
rich styles as parameters and return ``rich.text.Text`` objects (or
plain markup strings). They must never import theme/console modules so
they stay usable regardless of which styling backend is active.

Algorithms ported from btop++ (btop_draw.cpp / btop_theme.cpp):

- :func:`gradient`   -- ``Theme::generateGradients`` (two passes of
  50 + 51 interpolations when a mid color is defined).
- :func:`meter`      -- ``Meter::operator()``: every filled cell gets
  its own interpolated color based on its percentage position
  (per-cell gradient).
- :func:`braille_graph` -- ``Graph::_create`` braille sparkline: each
  glyph encodes two consecutive data points via
  ``index = result0 * 5 + result1`` into the 25-entry ``braille_up``
  symbol table.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Tuple

from rich.text import Text

RGB = Tuple[int, int, int]

#: btop++ ``graph_symbols["braille_up"]`` -- 25 glyphs indexed
#: ``result0 * 5 + result1`` where each result is a 0-4 scaled value.
BRAILLE_UP: Tuple[str, ...] = tuple(
    " "
    "⢀⢠⢰⢸"
    "⡀⣀⣠⣰⣸"
    "⡄⣄⣤⣴⣼"
    "⡆⣆⣦⣶⣾"
    "⡇⣇⣧⣷⣿"
)

METER_FILL = "█"
METER_EMPTY = "░"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _interp_channel(start: int, end: int, step: int, rng: int) -> int:
    """One channel of btop linear interpolation.

    Mirrors ``output_colors[i][rgb] = input[start] + (i - offset) *
    (input[end] - input[start]) / current_range`` with C-style integer
    truncation (inputs may be negative, hence float divide + int()).
    """
    return start + int(step * (end - start) / rng)


def gradient(
    start_rgb: RGB,
    mid_rgb: Optional[RGB],
    end_rgb: Optional[RGB],
    steps: int = 101,
) -> list:
    """Build an interpolated color ramp of ``steps`` ``(r, g, b)`` tuples.

    Ported from btop ``generateGradients``:

    * with a mid color defined, interpolation runs as two passes
      (start->mid over the first half, mid->end over the rest);
    * otherwise a single pass start->end;
    * with no end color, the whole ramp collapses to ``start_rgb``.
    """
    if steps < 1:
        raise ValueError("steps must be >= 1")
    if end_rgb is None:
        return [tuple(start_rgb) for _ in range(steps)]  # type: ignore[arg-type]

    has_mid = mid_rgb is not None
    current_range = (steps - 1) // 2 if has_mid else (steps - 1)

    out: list = []
    offset = 0
    start_idx = 0
    # End index into the (start, mid, end) source tuple.
    src = [tuple(start_rgb)]
    if has_mid:
        src.append(tuple(mid_rgb))
    src.append(tuple(end_rgb))

    for i in range(steps):
        if has_mid and i == current_range:
            # Switch source arrays from start->mid to mid->end.
            start_idx += 1
            offset += current_range
        out.append(
            tuple(
                _interp_channel(src[start_idx][c], src[start_idx + 1][c], i - offset, current_range)
                for c in range(3)
            )
        )
    return out


def _rgb_style(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


@lru_cache(maxsize=8192)
def _meter_cells(value: int, width: int, colors_key: tuple, invert: bool) -> tuple:
    """Cached raw cells for :func:`meter`.

    ``colors_key`` is the gradient as a tuple of ``(r, g, b)`` tuples;
    caching keeps the heavy
    per-cell styling work off repeated redraws (btop caches per value).
    """
    colors = list(colors_key)
    cells = []
    for i in range(1, width + 1):
        y = int(round(i * 100.0 / width))
        if value >= y:
            rgb = colors[(100 - y) if invert else y]
            cells.append((METER_FILL, rgb))
        else:
            for _ in range(width + 1 - i):
                cells.append((METER_EMPTY, None))
            break
    return tuple(cells)


def meter(value: float, width: int, colors: Sequence[RGB], *, invert: bool = False) -> Text:
    """Gradient bar in the spirit of btop ``Meter::operator()``.

    Every filled cell is colored from ``colors`` (a :func:`gradient`
    output) according to the percentage position of that cell -- this
    per-cell gradient is what makes btop meters look smooth. ``value``
    is clamped to 0-100. Results are LRU-cached per
    ``(value, width, colors, invert)``.
    """
    if width < 1:
        return Text()
    v = int(_clamp(int(value), 0, 100))
    colors_key = tuple(tuple(c) for c in colors)
    text = Text()
    for ch, rgb in _meter_cells(v, width, colors_key, invert):
        if rgb is None:
            text.append(ch, style="dim")
        else:
            text.append(ch, style=_rgb_style(rgb))
    return text


def braille_graph(
    values: Iterable[float],
    width: int,
    *,
    low: Optional[float] = None,
    high: Optional[float] = None,
    down: bool = False,
) -> Text:
    """Braille sparkline (btop ``Graph::_create``, single-height).

    Each glyph covers two consecutive data points; both are scaled to
    0-4 over the ``[low, high]`` range and combined as
    ``BRAILLE_UP[result0 * 5 + result1]``. When ``low``/``high`` are
    omitted they are derived from the min/max of the data (auto-scale).
    ``down=True`` flips the vertical axis (graphs fall as values rise),
    mirroring btop's ``invert`` behaviour.

    Only the last ``width * 2`` values are drawn.
    """
    data = [float(v) for v in values]
    if width < 1:
        return Text()
    pts = data[-(width * 2):]
    # Pad on the left with the first value so short series still align right.
    while len(pts) < width * 2:
        pts.insert(0, pts[0] if pts else 0.0)

    if low is None or high is None:
        data_low = min(pts) if pts else 0.0
        data_high = max(pts) if pts else 0.0
    else:
        data_low, data_high = float(low), float(high)

    def level(v: float) -> int:
        span = data_high - data_low
        if span <= 0:
            # Flat series: zero stays empty, any constant value fills.
            return 0 if data_high <= 0 else 4
        lvl = int(round((v - data_low) * 4.0 / span))
        return int(_clamp(lvl, 0, 4))

    text = Text()
    for i in range(0, len(pts), 2):
        r0 = level(pts[i])
        r1 = level(pts[i + 1])
        if down:
            r0, r1 = 4 - r0, 4 - r1
        text.append(BRAILLE_UP[r0 * 5 + r1])
    return text


def _visible_len(markup: str) -> int:
    """Length of ``markup`` as rendered, ignoring ``[...]`` style tags."""
    out = []
    skip = False
    for ch in markup:
        if ch == "[":
            skip = True
        elif ch == "]":
            skip = False
        elif not skip:
            out.append(ch)
    return len("".join(out))


def footer_bar(bindings: Sequence[Tuple[str, str]], width: int) -> str:
    """Single-line keybinding bar, btop menu style::

        [↑↓] chọn  ·  [s] sync  ·  [q] thoát

    Shortcut keys are wrapped in ``[hi_fg]`` markup; separators are
    dim ``·``. Items are dropped from the front (keeping the trailing
    quit binding) until the visible width fits ``width``. Returns rich
    markup as a plain string.
    """
    if not bindings or width < 1:
        return ""

    def render(key: str, label: str) -> str:
        return f"[hi_fg]{key}[/] {label}"

    sep = "[dim] · [/]"
    items = [render(k, label) for k, label in bindings]

    def total(parts: list) -> int:
        return sum(_visible_len(p) for p in parts) + _visible_len(sep) * max(0, len(parts) - 1)

    # Always keep the last item (quit); trim from the front otherwise.
    keep = [items[-1]] if items else []
    dropped = items[:-1]
    while dropped and total(dropped + keep) > width:
        dropped.pop(0)
    parts = dropped + keep
    return sep.join(parts)


def shortcut_title(title: str, key: str) -> str:
    """Section title with the hotkey letter highlighted, e.g.::

        ┌┐[hi_fg]m[/]enu┌┐

    The first occurrence of ``key`` inside ``title`` (case-insensitive)
    is wrapped in ``[hi_fg]`` markup; if absent, the first character is
    highlighted instead.
    """
    if not title:
        return ""
    idx = title.lower().find(key.lower()) if key else -1
    if idx < 0:
        idx = 0
    before = title[:idx]
    ch = title[idx]
    after = title[idx + 1:]
    return f"┌┐{before}[hi_fg]{ch}[/]{after}┌┐"
