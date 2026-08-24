"""Rich theme built from the semantic palette, with TOML override support.

Default keys are CTF-semantic (solved / unsolved / firstblood / ...) on
top of the generic :data:`ctf_downloader.ui.style.PALETTE`. Users can
override any style with a TOML file::

    [styles]
    solved = "green bold"
    firstblood = "#ff004f"

Load it via :func:`load_theme`; ``None`` means defaults only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from rich.theme import Theme

from .style import PALETTE

# CTF-semantic additions on top of the generic palette.
DEFAULT_STYLES: dict[str, str] = {
    **PALETTE,
    "solved": "bold green",
    "unsolved": "dim",
    "firstblood": "bold magenta",
    "div_line": "dim cyan",
    "hi_fg": "cyan",
    "title": "bold magenta",
}


def load_theme(path: str | Path | None) -> Theme:
    """Build a :class:`rich.theme.Theme`, optionally overridden by a TOML file.

    The TOML file may contain a top-level ``styles`` table whose entries
    replace same-named default styles. Unknown keys are kept as-is so a
    theme file can add brand-new styles too.
    """
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


__all__ = ["DEFAULT_STYLES", "load_theme"]
