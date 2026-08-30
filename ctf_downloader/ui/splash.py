"""Width-aware UCS_ExOdia splash.

Compatibility wrapper kept because interactive_menu and external callers import
ctf_downloader.ui.splash.splash directly.  The actual brand source of truth now
lives in ui.brand so help, logger, menu and framed commands cannot drift.
"""
from __future__ import annotations

from rich.text import Text

from .brand import WIDE_THRESHOLD, splash as _brand_splash

def splash(width: int | None = None) -> Text:
    try:
        from .. import __version__
    except Exception:
        __version__ = "3.0.0"
    return _brand_splash(width, version=f"v{__version__.split('.')[0]}")


__all__ = ["splash", "WIDE_THRESHOLD"]
