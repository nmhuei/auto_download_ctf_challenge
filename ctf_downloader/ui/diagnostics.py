"""Structured diagnostics rendered in the uv / cargo style.

Example output::

    error: failed to download challenge set
      ╰─▶ network unreachable after 3 retries
    hint: check your VPN connection
    hint: pass --retry 5 to increase attempts

- ``error:`` is red bold, ``warning:`` yellow bold.
- The message body is bold but *uncolored*.
- A ``cause`` chain uses the ``╰─▶`` connector, indented under the label.
- Each ``hint`` gets its own line with a cyan-bold ``hint:`` label.
- All text word-wraps to the target console width; continuation lines
  align to the column right after their label.

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

CAUSE_PREFIX = "╰─▶ "
HINT_INDENT = "  "

_SEVERITY_STYLE = {
    "error": "red bold",
    "warning": "yellow bold",
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
    """Shorthand builder for a warning Diagnostic."""
    return Diagnostic("warning", message, cause=cause, hints=tuple(hints))


def _wrap(text: str, width: int) -> list[str]:
    if not text:
        return []
    effective = max(width, 20)
    lines = textwrap.wrap(text, width=effective, break_on_hyphens=False)
    # Degenerate widths can yield nothing for unbreakable tokens.
    return lines or [text]


def build_lines(diag: Diagnostic, width: int = 80) -> list[Text]:
    """Build the pre-styled, pre-wrapped lines for *diag* at *width*."""
    lines: list[Text] = []

    label = f"{diag.severity}:"
    label_style = _SEVERITY_STYLE.get(diag.severity, "bold")
    label_pad = len(label) + 1

    # --- headline body: bold but uncolored -----------------------------
    chunks = _wrap(diag.message, width - label_pad)
    first = Text()
    first.append(label, style=label_style)
    first.append(" ")
    first.append(chunks[0], style="bold")
    lines.append(first)
    for chunk in chunks[1:]:
        cont = Text()
        cont.append(" " * label_pad)
        cont.append(chunk, style="bold")
        lines.append(cont)

    # --- optional cause chain ------------------------------------------
    if diag.cause:
        cause_pad = len(HINT_INDENT) + len(CAUSE_PREFIX)
        cchunks = _wrap(diag.cause, width - cause_pad)
        line = Text(HINT_INDENT)
        line.append(CAUSE_PREFIX, style=label_style)
        line.append(cchunks[0])
        lines.append(line)
        for cchunk in cchunks[1:]:
            lines.append(Text(" " * cause_pad + cchunk))

    # --- one hint per line ----------------------------------------------
    hint_pad = len(HINT_INDENT) + len("hint:") + 1
    for hint in diag.hints:
        hchunks = _wrap(hint, width - hint_pad)
        line = Text(HINT_INDENT)
        line.append("hint:", style="cyan bold")
        line.append(" ")
        line.append(hchunks[0])
        lines.append(line)
        for hchunk in hchunks[1:]:
            lines.append(Text(" " * hint_pad + hchunk))

    return lines


def render(diag: Diagnostic, console: Console | None = None) -> None:
    """Print *diag* in uv style to ``console`` (default: stderr console)."""
    con = console if console is not None else err_console
    for line in build_lines(diag, width=con.width or 80):
        con.print(line)


def emit(diag: Diagnostic, console: Console | None = None) -> int:
    """Render *diag* and return its exit code."""
    render(diag, console=console)
    return diag.exit_code


__all__ = ["Diagnostic", "build_lines", "emit", "error", "render", "warning"]
