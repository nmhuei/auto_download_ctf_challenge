"""Curated Theme Palettes for UCS_ExOdia & CTF Toolkit.

Provides dramatic, harmonious, and highly readable terminal color schemes.
Strictly decoupled from UI rendering logic: all components consume semantic tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Palette:
    """Immutable color palette defining semantic tokens for UI rendering."""
    name: str
    display_name: str
    description: str

    # Canvas & Surface
    bg: str
    surface: str
    border: str

    # Typography & Neutrals
    text: str
    muted: str
    faint: str

    # Identity / Brand Accent
    accent: str
    accent_hi: str
    accent_deep: str

    # Semantic Status Feedback
    success: str
    warning: str
    error: str
    firstblood: str

    # Selection & Cursor
    selected_fg: str
    selected_bg: str

    # CTF Category Accents
    category_web: str
    category_crypto: str
    category_pwn: str
    category_rev: str
    category_forensics: str
    category_misc: str

    def to_rich_styles(self) -> Dict[str, str]:
        """Convert palette tokens into standard Rich style mappings."""
        return {
            # Canonical semantic token family
            "bg": f"on {self.bg}",
            "surface": f"on {self.surface}",
            "border": self.border,
            "text": self.text,
            "muted": self.muted,
            "faint": self.faint,
            "cyan": self.accent,
            "selected": f"bold {self.selected_fg} on {self.selected_bg}",

            # Category styles
            "category.web": self.category_web,
            "category.crypto": self.category_crypto,
            "category.pwn": self.category_pwn,
            "category.rev": self.category_rev,
            "category.forensics": self.category_forensics,
            "category.misc": self.category_misc,

            # Semantic aliases
            "fg.base": self.text,
            "fg.muted": self.muted,
            "fg.faint": self.faint,
            "accent": self.accent,
            "accent.hi": self.accent_hi,
            "accent.deep": self.accent_deep,
            "info": self.accent,
            "solved": self.success,
            "firstblood": self.firstblood,
            "error": self.error,
            "warn": self.warning,
            "success": self.success,
            "warning": self.warning,
            "hint": self.accent,
            "path": self.accent,
            "literal": self.accent,
            "title": f"bold {self.accent}",
            "div_line": self.border,
            "hi_fg": self.accent,
            "unsolved": self.faint,
            "sel": f"bold {self.selected_fg} on {self.selected_bg}",
            "done": f"strike {self.muted}",
            "dim": "dim",

            # Functional domain-specific semantic tokens (Codex guidance)
            "git.branch": f"bold {self.accent}",
            "git.clean": f"bold {self.success}",
            "git.dirty": f"bold {self.warning}",
            "git.connected": f"bold {self.success}",
            "git.disconnected": self.faint,
            "chrome.title": f"bold {self.text}",
            "chrome.section": f"bold {self.faint}",
            "radar.active": f"bold {self.warning}",
            "radar.solved": f"bold {self.success}",
        }


# ==============================================================================
# Presets
# ==============================================================================

EXODIA_PALETTE = Palette(
    name="exodia",
    display_name="ExOdia Phosphor (Default)",
    description="Cool deep slate canvas with phosphorescent cyan/teal accent",
    bg="#070B10",
    surface="#0D141C",
    border="#244650",
    text="#E6EDF3",
    muted="#8B98A5",
    faint="#50606C",
    accent="#5EEAD4",
    accent_hi="#A7F3E8",
    accent_deep="#1F6F78",
    success="#9BE15D",
    warning="#FF9F43",
    error="#E5534B",
    firstblood="#FF5C8A",
    selected_fg="#DFFFFA",
    selected_bg="#163A42",
    category_web="#5EEAD4",
    category_crypto="#A7C7FF",
    category_pwn="#FF8A65",
    category_rev="#C9B5FF",
    category_forensics="#9BE15D",
    category_misc="#8B98A5",
)

CYBERPUNK_PALETTE = Palette(
    name="cyberpunk",
    display_name="Cyberpunk Neo-Tokyo",
    description="Dramatic high-voltage neon pink & laser cyan over void midnight",
    bg="#08060D",
    surface="#120E1C",
    border="#4A154B",
    text="#F1F5F9",
    muted="#94A3B8",
    faint="#64748B",
    accent="#00F5FF",
    accent_hi="#70FFFF",
    accent_deep="#007788",
    success="#00FF9F",
    warning="#FFE600",
    error="#FF2A6D",
    firstblood="#FF007F",
    selected_fg="#FFFFFF",
    selected_bg="#3D0038",
    category_web="#00F5FF",
    category_crypto="#9D4EDD",
    category_pwn="#FF5500",
    category_rev="#FF007F",
    category_forensics="#00FF9F",
    category_misc="#94A3B8",
)

MATRIX_PALETTE = Palette(
    name="matrix",
    display_name="Matrix Hacker Terminal",
    description="High-contrast emerald & phosphor green over obsidian black",
    bg="#030A05",
    surface="#07150B",
    border="#0D331A",
    text="#E2F5E8",
    muted="#73A382",
    faint="#3D5C46",
    accent="#00FF66",
    accent_hi="#80FFB3",
    accent_deep="#00662A",
    success="#34D399",
    warning="#FBBF24",
    error="#F87171",
    firstblood="#FF4B72",
    selected_fg="#FFFFFF",
    selected_bg="#0F3819",
    category_web="#00FF66",
    category_crypto="#6EE7B7",
    category_pwn="#F59E0B",
    category_rev="#A78BFA",
    category_forensics="#34D399",
    category_misc="#73A382",
)

AMBER_PALETTE = Palette(
    name="amber",
    display_name="Tactical Amber DEFCON",
    description="Vintage CRT phosphor amber with flame and burnished gold accents",
    bg="#0D0902",
    surface="#1A1205",
    border="#4D3300",
    text="#FDF0D5",
    muted="#BFA175",
    faint="#665233",
    accent="#FFB000",
    accent_hi="#FFD166",
    accent_deep="#664400",
    success="#E09F3E",
    warning="#F3722C",
    error="#D62828",
    firstblood="#E63946",
    selected_fg="#FFF3C4",
    selected_bg="#3D2900",
    category_web="#FFB000",
    category_crypto="#FFD166",
    category_pwn="#F3722C",
    category_rev="#C77DFF",
    category_forensics="#E09F3E",
    category_misc="#BFA175",
)

NORD_PALETTE = Palette(
    name="nord",
    display_name="Nordic Arctic Aurora",
    description="Harmonious, balanced low eye-strain frost blue and polar night",
    bg="#242933",
    surface="#2E3440",
    border="#3B4252",
    text="#ECEFF4",
    muted="#D8DEE9",
    faint="#4C566A",
    accent="#88C0D0",
    accent_hi="#8FBCBB",
    accent_deep="#5E81AC",
    success="#A3BE8C",
    warning="#EBCB8B",
    error="#BF616A",
    firstblood="#D08770",
    selected_fg="#ECEFF4",
    selected_bg="#434C5E",
    category_web="#88C0D0",
    category_crypto="#81A1C1",
    category_pwn="#D08770",
    category_rev="#B48EAD",
    category_forensics="#A3BE8C",
    category_misc="#D8DEE9",
)

PRESET_PALETTES: Dict[str, Palette] = {
    "exodia": EXODIA_PALETTE,
    "cyberpunk": CYBERPUNK_PALETTE,
    "matrix": MATRIX_PALETTE,
    "amber": AMBER_PALETTE,
    "nord": NORD_PALETTE,
}
