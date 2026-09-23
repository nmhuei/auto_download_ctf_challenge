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


def get_active_palette(force_reload: bool = False) -> Palette:
    """Get current active palette, checking in-memory override, env var, then global config."""
    global _CURRENT_PALETTE
    if _CURRENT_PALETTE is not None and not force_reload:
        return _CURRENT_PALETTE
    import os
    import sys
    env_theme = os.environ.get("CTF_THEME", "").strip().lower()
    if env_theme and env_theme in PRESET_PALETTES:
        _CURRENT_PALETTE = PRESET_PALETTES[env_theme]
        _sync_module_tokens(_CURRENT_PALETTE)
        return _CURRENT_PALETTE
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        if not force_reload:
            return EXODIA_PALETTE
    try:
        from ..storage.global_config import load_global_config
        cfg_theme = str(load_global_config().get("theme") or "").strip().lower()
        if cfg_theme and cfg_theme in PRESET_PALETTES:
            _CURRENT_PALETTE = PRESET_PALETTES[cfg_theme]
            _sync_module_tokens(_CURRENT_PALETTE)
            return _CURRENT_PALETTE
    except Exception:
        pass
    _CURRENT_PALETTE = EXODIA_PALETTE
    return EXODIA_PALETTE


def init_theme(theme_name: str | None = None) -> Palette:
    """Explicitly initialize and synchronize active theme palette from config or given name."""
    if theme_name and theme_name.strip().lower() in PRESET_PALETTES:
        pal = PRESET_PALETTES[theme_name.strip().lower()]
        set_active_theme(pal.name)
        return pal
    pal = get_active_palette(force_reload=True)
    _sync_module_tokens(pal)
    return pal


def _sync_module_tokens(palette: Palette) -> None:
    """Synchronize module-level color constants with the given palette."""
    import sys
    g = globals()
    tokens = {
        "ACCENT": palette.accent,
        "ACCENT_HI": palette.accent_hi,
        "ACCENT_DEEP": palette.accent_deep,
        "SUCCESS": palette.success,
        "SOLVED": palette.success,
        "WARNING": palette.warning,
        "WARN": palette.warning,
        "ERROR": palette.error,
        "FIRSTBLOOD": palette.firstblood,
        "TEXT": palette.text,
        "FG_BASE": palette.text,
        "MUTED": palette.muted,
        "FG_MUTED": palette.muted,
        "FAINT": palette.faint,
        "FG_FAINT": palette.faint,
        "BORDER": palette.border,
        "SURFACE": palette.surface,
        "BG": palette.bg,
        "INFO": palette.accent,
        "CATEGORY_WEB": palette.category_web,
        "CATEGORY_CRYPTO": palette.category_crypto,
        "CATEGORY_PWN": palette.category_pwn,
        "CATEGORY_REV": palette.category_rev,
        "CATEGORY_FORENSICS": palette.category_forensics,
        "CATEGORY_MISC": palette.category_misc,
    }
    g.update(tokens)

    alias_map = {
        "_TEXT_COLOR": palette.text,
        "_MUTED_COLOR": palette.muted,
        "_FAINT_COLOR": palette.faint,
        "_ACCENT_COLOR": palette.accent,
        "_CYAN_HI": palette.accent_hi,
        "_CYAN_DEEP": palette.accent_deep,
        "_SUCCESS_COLOR": palette.success,
        "_WARN_COLOR": palette.warning,
        "_ERROR_COLOR": palette.error,
        "_FIRSTBLOOD_COLOR": palette.firstblood,
        "_SOLVED_COLOR": palette.success,
        "_CAT_WEB": palette.category_web,
        "_CAT_CRYPTO": palette.category_crypto,
        "_CAT_PWN": palette.category_pwn,
        "_CAT_REV": palette.category_rev,
        "_CAT_FORENSICS": palette.category_forensics,
        "_CAT_MISC": palette.category_misc,
    }

    for mod_name in ("ctf_downloader.interactive_menu", "ctf_downloader.ui.menu_hubs", "ctf_downloader.cli_commands"):
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for k, v in tokens.items():
                if hasattr(mod, k):
                    setattr(mod, k, v)
            for k, v in alias_map.items():
                if hasattr(mod, k):
                    setattr(mod, k, v)
            if hasattr(mod, "_CAT_COLORS") and isinstance(mod._CAT_COLORS, dict):
                mod._CAT_COLORS.update({
                    "web": palette.category_web,
                    "crypto": palette.category_crypto,
                    "pwn": palette.category_pwn,
                    "pwnable": palette.category_pwn,
                    "reverse": palette.category_rev,
                    "rev": palette.category_rev,
                    "forensics": palette.category_forensics,
                })

    # Synchronize Brand Identity
    brand_mod = sys.modules.get("ctf_downloader.ui.brand")
    if brand_mod is not None:
        brand_name = "UCS_ExOdia" if palette.name == "exodia" else f"UCS_{palette.name.capitalize()}"
        setattr(brand_mod, "BRAND_NAME", brand_name)
        if hasattr(brand_mod, "hex_to_rgb"):
            h2r = brand_mod.hex_to_rgb
            setattr(brand_mod, "LOGO_START", h2r(palette.accent))
            setattr(brand_mod, "LOGO_MID", h2r(palette.accent_hi))
            setattr(brand_mod, "LOGO_END", h2r(palette.firstblood))
            if palette.name == "exodia" and hasattr(brand_mod, "DEFAULT_OPERATION_RAMPS"):
                setattr(brand_mod, "OPERATION_RAMPS", brand_mod.DEFAULT_OPERATION_RAMPS)
            elif hasattr(brand_mod, "build_operation_ramps"):
                setattr(brand_mod, "OPERATION_RAMPS", brand_mod.build_operation_ramps(palette))

    # Synchronize Banner
    banner_mod = sys.modules.get("ctf_downloader.ui.banner")
    if banner_mod is not None:
        brand_name = "UCS_ExOdia" if palette.name == "exodia" else f"UCS_{palette.name.capitalize()}"
        setattr(banner_mod, "BRAND_NAME", brand_name)

    # Synchronize Widgets & clear cached meter cells
    widgets_mod = sys.modules.get("ctf_downloader.ui.widgets")
    if widgets_mod is not None:
        from .brand import hex_to_rgb
        deep_rgb = hex_to_rgb(palette.accent_deep)
        accent_rgb = hex_to_rgb(palette.accent)
        hi_rgb = hex_to_rgb(palette.accent_hi)
        new_stops = (deep_rgb, accent_rgb, hi_rgb)
        setattr(widgets_mod, "UTILITY_STOPS", new_stops)
        if hasattr(widgets_mod, "multi_stop_gradient"):
            new_ramp = widgets_mod.multi_stop_gradient(new_stops, steps=101)
            setattr(widgets_mod, "UTILITY_RAMP", new_ramp)
            setattr(widgets_mod, "AMBER_RAMP", new_ramp)
        if hasattr(widgets_mod, "_meter_cells"):
            try:
                widgets_mod._meter_cells.cache_clear()
            except Exception:
                pass


def set_active_theme(name: str | None) -> bool:
    """Set active theme palette by preset name (None resets to global/default)."""
    global _CURRENT_PALETTE
    if name is None:
        _CURRENT_PALETTE = None
        _sync_module_tokens(get_active_palette())
        return True
    key = name.strip().lower()
    if key in PRESET_PALETTES:
        _CURRENT_PALETTE = PRESET_PALETTES[key]
        _sync_module_tokens(_CURRENT_PALETTE)
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
    "DEFAULT_STYLES", "load_theme", "init_theme",
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

# Initialize module tokens to active palette on import
_sync_module_tokens(get_active_palette())
