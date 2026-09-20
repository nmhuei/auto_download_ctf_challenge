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

from .palettes import EXODIA_PALETTE, PRESET_PALETTES, Palette

_CURRENT_PALETTE: Palette | None = None

DEFAULT_STYLES: dict[str, str] = EXODIA_PALETTE.to_rich_styles()


def get_active_palette() -> Palette:
    """Get current active palette, checking in-memory override, env var, then global config."""
    if _CURRENT_PALETTE is not None:
        return _CURRENT_PALETTE
    import os
    env_theme = os.environ.get("CTF_THEME", "").strip().lower()
    if env_theme and env_theme in PRESET_PALETTES:
        return PRESET_PALETTES[env_theme]
    try:
        from ..storage.global_config import load_global_config
        cfg_theme = str(load_global_config().get("theme") or "").strip().lower()
        if cfg_theme and cfg_theme in PRESET_PALETTES:
            return PRESET_PALETTES[cfg_theme]
    except Exception:
        pass
    return EXODIA_PALETTE


def set_active_theme(name: str | None) -> bool:
    """Set active theme palette by preset name (None resets to global/default)."""
    global _CURRENT_PALETTE
    if name is None:
        _CURRENT_PALETTE = None
        return True
    key = name.strip().lower()
    if key in PRESET_PALETTES:
        _CURRENT_PALETTE = PRESET_PALETTES[key]
        return True
    return False


def list_themes() -> dict[str, str]:
    """Return dictionary of available theme names and their display labels."""
    return {k: p.display_name for k, p in PRESET_PALETTES.items()}


def load_theme(path_or_name: str | Path | None = None) -> Theme:
    """Build a Rich Theme from preset name, active palette, or TOML file path."""
    import os

    palette = get_active_palette()
    if isinstance(path_or_name, str) and path_or_name.lower() in PRESET_PALETTES:
        palette = PRESET_PALETTES[path_or_name.lower()]
        return Theme(palette.to_rich_styles())

    styles = palette.to_rich_styles()
    if path_or_name is not None and (isinstance(path_or_name, Path) or (isinstance(path_or_name, str) and os.path.isfile(str(path_or_name)))):
        with open(path_or_name, "rb") as fh:
            data = tomllib.load(fh)
        overrides = data.get("styles", data)
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if isinstance(value, str):
                    styles[key] = value
    return Theme(styles)


__all__ = [
    "DEFAULT_STYLES", "load_theme",
    "get_active_palette", "set_active_theme", "list_themes",
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
