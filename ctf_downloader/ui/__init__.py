"""UI toolkit: output discipline, printers, diagnostics, style, theme.

Public surface::

    from ctf_downloader.ui import (
        err_console, out_console,          # console.py
        Printer, Verbosity,                # printers.py
        Diagnostic, render, emit, error, warning,  # diagnostics.py
        PALETTE, OK, FAIL, WARN, DOT, CROSS, BRANCH, SPINNER,
        ok_summary,                        # style.py
        load_theme, DEFAULT_STYLES,        # theme.py
    )
"""

from .console import err_console, out_console
from .diagnostics import Diagnostic, build_lines, emit, error, render, warning
from .printers import Printer, Verbosity
from .style import (
    BRANCH,
    CROSS,
    DOT,
    FAIL,
    OK,
    SPINNER,
    WARN,
    PALETTE,
    ok_summary,
)
from .theme import DEFAULT_STYLES, load_theme

__all__ = [
    "err_console",
    "out_console",
    "Printer",
    "Verbosity",
    "Diagnostic",
    "build_lines",
    "render",
    "emit",
    "error",
    "warning",
    "PALETTE",
    "OK",
    "FAIL",
    "WARN",
    "DOT",
    "CROSS",
    "BRANCH",
    "SPINNER",
    "ok_summary",
    "DEFAULT_STYLES",
    "load_theme",
]
