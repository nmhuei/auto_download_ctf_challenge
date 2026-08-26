"""Structured diagnostics rendered in the uv / cargo style (SPEC E1 error tree).

Example output::

    ✗ error: failed to download challenge set
      ├─ network unreachable after 3 retries
      └─ ACTION REQUIRED
         · check your VPN connection
         · pass --retry 5 to increase attempts

- The headline carries a severity glyph: ``✗`` for errors, ``!`` for
  warnings; ``error:`` is red bold, ``warning:`` yellow bold.
- The message body is bold but *uncolored*.
- An optional ``cause`` hangs off a ``├─`` branch (div_line connector,
  cause text ``fg.base``); wrapped cause lines continue with ``│``.
- When hints exist, the tree terminates in a bold-accent
  ``└─ ACTION REQUIRED`` node and each hint becomes a muted ``·`` leaf.
- All text word-wraps to the target console width; continuation lines
  align under their branch column.

:func:`build_lines` returns the styled lines (testable without ANSI);
:func:`render` prints them to a console (default: stderr).
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.text import Text

from .console import err_console
from .theme import ACCENT, ACCENT_DEEP, FG_BASE, FG_MUTED

TREE_TEE = "├─ "  # nhánh cause
TREE_ELL = "└─ "  # node kết ACTION REQUIRED
LEAF_DOT = "· "   # lá hint dưới node kết
TREE_BAR = "│  "  # connector nối tiếp khi cause wrap
TREE_INDENT = "  "

_SEVERITY_STYLE = {
    "error": "red bold",
    "warning": "yellow bold",
}

_SEVERITY_GLYPH = {
    "error": "✗",
    "warning": "!",
}


@dataclass
class Diagnostic:
    """A single structured problem report."""

    severity: str  # "error" | "warning"
    message: str
    cause: Optional[str] = None
    hints: tuple[str, ...] = ()
    exit_code: int = 1


def error(
    message: str,
    *,
    cause: Optional[str] = None,
    hints: tuple[str, ...] | list[str] = (),
) -> Diagnostic:
    """Shorthand builder for an error Diagnostic."""
    return Diagnostic("error", message, cause=cause, hints=tuple(hints))


def warning(
    message: str,
    *,
    cause: Optional[str] = None,
    hints: tuple[str, ...] | list[str] = (),
) -> Diagnostic:
    """Shorthand builder for an warning Diagnostic."""
    return Diagnostic("warning", message, cause=cause, hints=tuple(hints))


def _wrap(text: str, width: int) -> list[str]:
    if not text:
        return []
    effective = max(width, 20)
    lines = textwrap.wrap(text, width=effective, break_on_hyphens=False)
    # Degenerate widths can yield nothing for unbreakable tokens.
    return lines or [text]


def build_lines(diag: Diagnostic, width: int = 80) -> list[Text]:
    """Build the pre-styled, pre-wrapped error tree for *diag* at *width*."""
    lines: list[Text] = []

    label = f"{diag.severity}:"
    label_style = _SEVERITY_STYLE.get(diag.severity, "bold")
    glyph_prefix = f"{_SEVERITY_GLYPH.get(diag.severity, '✗')} "
    label_pad = len(glyph_prefix) + len(label) + 1

    # --- headline body: glyph + colored label + bold uncolored message --
    chunks = _wrap(diag.message, width - label_pad)
    first = Text()
    first.append(glyph_prefix, style=label_style)
    first.append(label, style=label_style)
    first.append(" ")
    first.append(chunks[0], style="bold")
    lines.append(first)
    for chunk in chunks[1:]:
        cont = Text()
        cont.append(" " * label_pad)
        cont.append(chunk, style="bold")
        lines.append(cont)

    tee_pad = len(TREE_INDENT) + len(TREE_TEE)

    # --- optional cause branch: ├─ cause (wrap nối bằng │) ---------------
    if diag.cause:
        cchunks = _wrap(diag.cause, width - tee_pad)
        line = Text(TREE_INDENT)
        line.append(TREE_TEE, style=ACCENT_DEEP)
        line.append(cchunks[0], style=FG_BASE)
        lines.append(line)
        for cchunk in cchunks[1:]:
            cont = Text(TREE_INDENT)
            cont.append(TREE_BAR, style=ACCENT_DEEP)
            cont.append(cchunk, style=FG_BASE)
            lines.append(cont)

    # --- terminal node └─ ACTION REQUIRED + muted · leaves ---------------
    if diag.hints:
        ell_line = Text(TREE_INDENT)
        ell_line.append(TREE_ELL, style=ACCENT_DEEP)
        ell_line.append("ACTION REQUIRED", style=f"bold {ACCENT}")
        lines.append(ell_line)

        leaf_indent = " " * tee_pad
        leaf_pad = tee_pad + len(LEAF_DOT)
        for hint in diag.hints:
            hchunks = _wrap(hint, width - leaf_pad)
            line = Text(leaf_indent)
            line.append(LEAF_DOT, style=FG_MUTED)
            line.append(hchunks[0], style=FG_MUTED)
            lines.append(line)
            for hchunk in hchunks[1:]:
                lines.append(Text(" " * leaf_pad + hchunk, style=FG_MUTED))

    return lines


def render(diag: Diagnostic, console: Console | None = None) -> None:
    """Print *diag* as an error tree to ``console`` (default: stderr)."""
    con = console if console is not None else err_console
    for line in build_lines(diag, width=con.width or 80):
        con.print(line)


def emit(diag: Diagnostic, console: Console | None = None) -> int:
    """Render *diag* and return its exit code."""
    render(diag, console=console)
    return diag.exit_code


__all__ = [
    "Diagnostic",
    "LEAF_DOT",
    "TREE_ELL",
    "TREE_TEE",
    "build_lines",
    "emit",
    "error",
    "render",
    "warning",
]
