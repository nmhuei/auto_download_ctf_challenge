"""Verbosity levels and the Printer gate (uv-style output discipline).

uv's rule of thumb:

- ``-q`` (quiet) silences summaries and disables progress bars.
- Default prints one-line summaries.
- ``--verbose`` shows debug detail but *disables* progress bars/spinners
  so that logs stay greppable.

``Printer`` encodes exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .console import err_console, out_console


class Verbosity(IntEnum):
    """Ordered verbosity: lower is quieter."""

    QUIET2 = 0  # -qq: nothing at all
    QUIET1 = 1  # -q: errors only
    NORMAL = 2  # default: summaries + progress
    VERBOSE = 3  # --verbose: debug lines, no progress bar


@dataclass
class Printer:
    """Central gate for status output and machine-readable data."""

    verbosity: Verbosity = Verbosity.NORMAL
    no_progress: bool = False

    @property
    def quiet(self) -> bool:
        return self.verbosity < Verbosity.NORMAL

    @property
    def progress_enabled(self) -> bool:
        """Progress bars/spinners run only in NORMAL mode (uv: verbose
        turns the bar off so logs stay clean)."""
        return self.verbosity == Verbosity.NORMAL and not self.no_progress

    def summary(self, text: str) -> None:
        """One-line human status -> stderr, printed unless quiet."""
        if not self.quiet:
            err_console.print(text)

    def debug(self, text: str) -> None:
        """Verbose-only detail -> stderr."""
        if self.verbosity >= Verbosity.VERBOSE and not self.no_progress:
            err_console.print(text)

    def out_data(self, text: str) -> None:
        """Machine-readable payload -> stdout, undecorated (pipe-safe)."""
        out_console.print(text, markup=False, highlight=False, emoji=False)


__all__ = ["Printer", "Verbosity"]
