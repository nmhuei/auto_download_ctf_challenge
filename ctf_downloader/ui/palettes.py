"""Curated Theme Palettes for UCS_ExOdia & CTF Toolkit.

Provides dramatic, harmonious, and highly readable terminal color schemes.
Strictly decoupled from UI rendering logic: all components consume semantic tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from rich.text import Text


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

    @property
    def fg_base(self) -> str:
        return self.text

    @property
    def fg_muted(self) -> str:
        return self.muted

    @property
    def fg_faint(self) -> str:
        return self.faint

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

    def color_ramp_text(self) -> Text:
        """Visual color ramp swatch showing accent, accent_hi, success, warning, error, firstblood."""
        t = Text()
        t.append("■", style=self.accent)
        t.append("■", style=self.accent_hi)
        t.append("■", style=self.success)
        t.append("■", style=self.warning)
        t.append("■", style=self.error)
        t.append("■", style=self.firstblood)
        return t

    def full_spectrum_text(self) -> Text:
        """Full rich visual spectrum breakdown for showcase and headers."""
        t = Text()
        t.append("  Core Spectrum:     ", style="dim")
        t.append("■■ ", style=self.accent)
        t.append(f"Accent ({self.accent})  ", style=self.accent)
        t.append("■■ ", style=self.accent_hi)
        t.append(f"Glow ({self.accent_hi})  ", style=self.accent_hi)
        t.append("■■ ", style=self.success)
        t.append(f"Solved ({self.success})  ", style=self.success)
        t.append("■■ ", style=self.warning)
        t.append(f"Warn ({self.warning})  ", style=self.warning)
        t.append("■■ ", style=self.error)
        t.append(f"Alert ({self.error})\n", style=self.error)

        t.append("  Category Spectrum: ", style="dim")
        t.append("■ ", style=self.category_web)
        t.append("Web  ", style=self.category_web)
        t.append("■ ", style=self.category_crypto)
        t.append("Crypto  ", style=self.category_crypto)
        t.append("■ ", style=self.category_pwn)
        t.append("Pwn  ", style=self.category_pwn)
        t.append("■ ", style=self.category_rev)
        t.append("Rev  ", style=self.category_rev)
        t.append("■ ", style=self.category_forensics)
        t.append("Forensics  ", style=self.category_forensics)
        t.append("■ ", style=self.category_misc)
        t.append("Misc", style=self.category_misc)
        return t


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

DRACULA_PALETTE = Palette(
    name="dracula",
    display_name="Dracula Midnight Vampire",
    description="Gothic velvet slate with neon orchid, radioactive mint & blood orange",
    bg="#0B0B12",
    surface="#151522",
    border="#342B4E",
    text="#F8F8F2",
    muted="#9580FF",
    faint="#6272A4",
    accent="#BD93F9",
    accent_hi="#E2D9F3",
    accent_deep="#6A4C93",
    success="#50FA7B",
    warning="#FFB86C",
    error="#FF5555",
    firstblood="#FF79C6",
    selected_fg="#FFFFFF",
    selected_bg="#2D1F47",
    category_web="#8BE9FD",
    category_crypto="#BD93F9",
    category_pwn="#FFB86C",
    category_rev="#FF79C6",
    category_forensics="#50FA7B",
    category_misc="#6272A4",
)

TOKYO_NIGHT_PALETTE = Palette(
    name="tokyo",
    display_name="Tokyo Night Stealth",
    description="Deep midnight indigo, luminous wisteria purple, electric cyan & blossom",
    bg="#0A0E1A",
    surface="#121829",
    border="#1F2A4A",
    text="#C0CAF5",
    muted="#7AA2F7",
    faint="#565F89",
    accent="#7DCFFF",
    accent_hi="#B4F9F8",
    accent_deep="#2AC3DE",
    success="#9ECE6A",
    warning="#E0AF68",
    error="#F7768E",
    firstblood="#BB9AF7",
    selected_fg="#FFFFFF",
    selected_bg="#1A2B4C",
    category_web="#7DCFFF",
    category_crypto="#BB9AF7",
    category_pwn="#FF9E64",
    category_rev="#9D7CD8",
    category_forensics="#9ECE6A",
    category_misc="#7AA2F7",
)

SYNTHWAVE_PALETTE = Palette(
    name="synthwave",
    display_name="Synthwave Sunset 1984",
    description="Retrowave sunset magenta, laser violet, neon peach & solar gold",
    bg="#0F051D",
    surface="#1B0E33",
    border="#501B6B",
    text="#F9F5FF",
    muted="#B388EB",
    faint="#724E91",
    accent="#FF71CE",
    accent_hi="#FFAAE5",
    accent_deep="#B9348B",
    success="#01CDFE",
    warning="#FF9B71",
    error="#FF3864",
    firstblood="#FE88F7",
    selected_fg="#FFFFFF",
    selected_bg="#3D0F47",
    category_web="#01CDFE",
    category_crypto="#B388EB",
    category_pwn="#FF9B71",
    category_rev="#FE88F7",
    category_forensics="#05FFA1",
    category_misc="#724E91",
)

MONOKAI_PRO_PALETTE = Palette(
    name="monokai",
    display_name="Monokai Pro Dark",
    description="Elite code-auditor charcoal, vibrant solar yellow, radiant magenta & sky",
    bg="#131316",
    surface="#1C1C21",
    border="#36353F",
    text="#FCFCFA",
    muted="#939293",
    faint="#5B595C",
    accent="#FFD866",
    accent_hi="#FFF0A0",
    accent_deep="#B89B38",
    success="#A9DC76",
    warning="#FC9867",
    error="#FF6188",
    firstblood="#AB9DF2",
    selected_fg="#FFFFFF",
    selected_bg="#38362B",
    category_web="#78DCE8",
    category_crypto="#AB9DF2",
    category_pwn="#FC9867",
    category_rev="#FF6188",
    category_forensics="#A9DC76",
    category_misc="#939293",
)

CRIMSON_PALETTE = Palette(
    name="crimson",
    display_name="Crimson Blood Moon",
    description="Sith obsidian darkness, ember crimson, molten scarlet & hazard orange",
    bg="#0D0405",
    surface="#1A0A0C",
    border="#471419",
    text="#FEE8E8",
    muted="#C4787D",
    faint="#6E3B3E",
    accent="#FF2A4D",
    accent_hi="#FF7088",
    accent_deep="#A8152D",
    success="#4EBA6F",
    warning="#FF8C42",
    error="#E61C24",
    firstblood="#FF0033",
    selected_fg="#FFFFFF",
    selected_bg="#420D12",
    category_web="#FF2A4D",
    category_crypto="#D473FF",
    category_pwn="#FF8C42",
    category_rev="#FF0033",
    category_forensics="#4EBA6F",
    category_misc="#C4787D",
)

# Register all palettes + convenience aliases
PRESET_PALETTES.update({
    "dracula": DRACULA_PALETTE,
    "tokyo": TOKYO_NIGHT_PALETTE,
    "tokyo_night": TOKYO_NIGHT_PALETTE,
    "synthwave": SYNTHWAVE_PALETTE,
    "monokai": MONOKAI_PRO_PALETTE,
    "monokai_pro": MONOKAI_PRO_PALETTE,
    "crimson": CRIMSON_PALETTE,
    "sith": CRIMSON_PALETTE,
})
