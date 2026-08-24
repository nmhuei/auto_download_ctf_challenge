"""Semantic style tokens and terminal symbols.

Colors are referenced by *meaning*, never by raw color name, so a theme
override in one place re-skins the whole CLI.
"""

from __future__ import annotations

# Semantic palette: key -> rich style string.
PALETTE: dict[str, str] = {
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "hint": "cyan",
    "path": "cyan",
    "literal": "cyan",
    "dim": "dim",
    "title": "bold magenta",
}

# Unicode glyphs used across the CLI. Kept here so tests and callers can
# assert on constants instead of scattering literals.
OK: str = "✔"  # check mark
FAIL: str = "✗"  # ballot X
WARN: str = "!"
DOT: str = "·"  # middle dot (list bullets, progress ticks)
CROSS: str = "×"  # multiplication sign (uv-style problem marker)
BRANCH: str = "╰─▶"  # cause chain connector
SPINNER: tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴",
    "⠦", "⠧", "⠇", "⠏",  # braille spinner frames
)


def ok_summary(verb: str, n: int, noun: str, secs: float) -> str:
    """Build a completion line: ``Đã <verb> <n> <noun> trong <X.XXs>``.

    ``verb`` is a Vietnamese past-tense verb ("tải", "xuất", ...).
    Count is bold; elapsed time is dim. Noun gets an English-style plural
    ``s`` when ``n != 1`` (Vietnamese itself does not inflect nouns).
    """
    plural = "" if n == 1 else "s"
    return (
        f"Đã {verb} [bold]{n}[/] {noun}{plural}"
        f" [dim]trong {secs:.2f}s[/]"
    )


__all__ = [
    "PALETTE", "OK", "FAIL", "WARN", "DOT", "CROSS", "BRANCH",
    "SPINNER", "ok_summary",
]
