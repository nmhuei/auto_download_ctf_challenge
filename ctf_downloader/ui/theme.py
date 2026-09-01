"""Semantic terminal theme for UCS_ExOdia.

The palette is intentionally small and role-driven: cyan/teal owns identity,
active information and important chrome; cool neutrals carry normal text;
solve/success, warning and error keep distinct semantic colors. Callers should
prefer named Rich styles over raw colors so a TOML override can reskin the CLI
from one place.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from rich.theme import Theme

# Core semantic palette.
BG = "#070B10"
SURFACE = "#0D141C"
BORDER = "#244650"

TEXT = "#E6EDF3"
MUTED = "#8B98A5"
FAINT = "#50606C"

CYAN = "#5EEAD4"
CYAN_HI = "#A7F3E8"
CYAN_DEEP = "#1F6F78"

SUCCESS = "#9BE15D"
WARNING = "#FF9F43"
ERROR = "#E5534B"
FIRSTBLOOD = "#FF5C8A"

SELECTED_FG = "#DFFFFA"
SELECTED_BG = "#163A42"

# Stable category accents. Use only where category differentiation helps.
CATEGORY_WEB = CYAN
CATEGORY_CRYPTO = "#A7C7FF"
CATEGORY_PWN = "#FF8A65"
CATEGORY_REV = "#C9B5FF"
CATEGORY_FORENSICS = SUCCESS
CATEGORY_MISC = MUTED

# Backward-compatible aliases used by existing surfaces.
FG_BASE = TEXT
FG_MUTED = MUTED
FG_FAINT = FAINT
ACCENT = CYAN
ACCENT_HI = CYAN_HI
ACCENT_DEEP = CYAN_DEEP
INFO = CYAN
SOLVED = SUCCESS
WARN = WARNING
SEL_FG = SELECTED_FG
SEL_BG = SELECTED_BG

DEFAULT_STYLES: dict[str, str] = {
    # New semantic token family.
    "bg": f"on {BG}",
    "surface": f"on {SURFACE}",
    "border": BORDER,
    "text": TEXT,
    "muted": MUTED,
    "faint": FAINT,
    "cyan": CYAN,
    "selected": f"bold {SELECTED_FG} on {SELECTED_BG}",
    "category.web": CATEGORY_WEB,
    "category.crypto": CATEGORY_CRYPTO,
    "category.pwn": CATEGORY_PWN,
    "category.rev": CATEGORY_REV,
    "category.forensics": CATEGORY_FORENSICS,
    "category.misc": CATEGORY_MISC,

    # Existing UI token names mapped onto the semantic palette.
    "fg.base": FG_BASE,
    "fg.muted": FG_MUTED,
    "fg.faint": FG_FAINT,
    "accent": ACCENT,
    "accent.hi": ACCENT_HI,
    "accent.deep": ACCENT_DEEP,
    "info": INFO,
    "solved": SOLVED,
    "firstblood": FIRSTBLOOD,
    "error": ERROR,
    "warn": WARN,
    "success": SUCCESS,
    "warning": WARNING,
    "hint": INFO,
    "path": INFO,
    "literal": INFO,
    "title": f"bold {ACCENT}",
    "div_line": BORDER,
    "hi_fg": ACCENT,
    "unsolved": FG_FAINT,
    "sel": f"bold {SEL_FG} on {SEL_BG}",
    "done": f"strike {FG_MUTED}",
    "dim": "dim",
}


def load_theme(path: str | Path | None) -> Theme:
    """Build a Rich Theme, optionally overridden by a TOML file."""
    styles = dict(DEFAULT_STYLES)
    if path is not None:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        overrides = data.get("styles", data)
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if isinstance(value, str):
                    styles[key] = value
    return Theme(styles)


__all__ = [
    "DEFAULT_STYLES", "load_theme",
    "BG", "SURFACE", "BORDER",
    "TEXT", "MUTED", "FAINT",
    "CYAN", "CYAN_HI", "CYAN_DEEP",
    "SUCCESS", "WARNING", "ERROR", "FIRSTBLOOD",
    "SELECTED_FG", "SELECTED_BG",
    "CATEGORY_WEB", "CATEGORY_CRYPTO", "CATEGORY_PWN",
    "CATEGORY_REV", "CATEGORY_FORENSICS", "CATEGORY_MISC",
    "FG_BASE", "FG_MUTED", "FG_FAINT",
    "ACCENT", "ACCENT_HI", "ACCENT_DEEP",
    "INFO", "SOLVED", "WARN", "SEL_FG", "SEL_BG",
]
