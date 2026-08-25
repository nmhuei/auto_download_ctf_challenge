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

# PHOSPHOR FIELD KIT token set (spec §3, chuẩn hoá codex-r3 #1). Dotted keys
# are resolved verbatim by rich's theme stack before any color parsing, so
# they are safe in markup.
#
# Palette kỷ luật: neutral fg.* + MỘT accent amber ``#FFB000`` family
# (#6B4300 → #FFB000 → #FFE49A — trùng đúng 3 mốc meter §3.3). Semantic chỉ
# còn 3 glyph vai trò: ✔ solved-green / ✗ error-red / ! warn. Cyan/green/red
# trang trí và vàng ngoài token đã bị bỏ — path/literal/lệnh dùng neutral.
FG_BASE = "#D8DFD9"       # nội dung chính
FG_MUTED = "#8A958C"      # thông tin phụ
FG_FAINT = "#4A534C"      # chrome: nhãn cột, đường nối, glyph trống
ACCENT = "#FFB000"        # amber phosphor — giọng nói duy nhất
ACCENT_HI = "#FFE49A"     # đỉnh nhấn amber — trùng mốc cuối meter §3.3
ACCENT_DEEP = "#6B4300"   # amber tắt đèn — trùng mốc đầu meter §3.3
INFO = FG_BASE            # path/literal/lệnh → neutral (đã bỏ cyan)
SOLVED = "#5CC878"        # ✔ solved-green (semantic duy nhất cùng ✗/!)
FIRSTBLOOD = "#FF2E63"    # ◆ + bold
ERROR = "#E5534B"         # ✗ đỏ
WARN = "#EAC54F"          # !

DEFAULT_STYLES: dict[str, str] = {
    # --- Legacy aliases (PALETTE) ĐẨY TRƯỚC để token spec ghi đè hết:
    # trước đây "error" của PALETTE đè token hex (codex-r3 #1) ---
    **PALETTE,  # dim / path / literal / hint / title nền chung
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
