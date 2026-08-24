"""Rich theme built from semantic palette tokens, with TOML override support.

PHOSPHOR FIELD KIT (design-system spec v1.0 §3): một accent duy nhất
amber ``#FFB000`` trên nền tối; mọi màu khác trung tính hoặc gắn ngữ nghĩa
glyph (✔ solved / ◆ firstblood / ✗ error / ! warn). Users can override any
style with a TOML file::

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

# PHOSPHOR FIELD KIT token set (spec §3). Dotted keys are resolved verbatim
# by rich's theme stack before any color parsing, so they are safe in markup.
FG_BASE = "#D8DFD9"       # nội dung chính
FG_MUTED = "#8A958C"      # thông tin phụ
FG_FAINT = "#4A534C"      # chrome: nhãn cột, đường nối, glyph trống
ACCENT = "#FFB000"        # amber phosphor — giọng nói duy nhất
ACCENT_HI = "#FFD75F"     # đỉnh nhấn amber (điểm vừa đạt)
ACCENT_DEEP = "#7A5200"   # amber tắt đèn: khung panel, ── heading
INFO = "#62C8CE"          # path/literal/lệnh — chỗ lạnh duy nhất
SOLVED = "#46C46B"        # ✔
FIRSTBLOOD = "#FF2E63"    # ◆ + bold
ERROR = "#FF5C57"         # ✗
WARN = "#EAC54F"          # !

DEFAULT_STYLES: dict[str, str] = {
    # --- Token spec §3 ---
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
    # --- Legacy aliases (giữ tương thích caller cũ) trỏ vào token mới ---
    **PALETTE,  # dim / path / literal / hint / title nền chung
    "success": SOLVED,
    "warning": WARN,
    "hint": INFO,
    "path": INFO,
    "literal": INFO,
    "title": f"bold {ACCENT}",
    "div_line": ACCENT_DEEP,
    "hi_fg": ACCENT,
    "unsolved": FG_FAINT,
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


__all__ = [
    "DEFAULT_STYLES", "load_theme",
    "FG_BASE", "FG_MUTED", "FG_FAINT",
    "ACCENT", "ACCENT_HI", "ACCENT_DEEP",
    "INFO", "SOLVED", "FIRSTBLOOD", "ERROR", "WARN",
]
