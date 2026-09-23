"""Data models for schema-driven CTF challenge categories."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class CategoryDefinition:
    """Schema for a modular, extensible CTF challenge category."""

    key: str
    display_name: str
    safe_label: str
    title_suffix: str
    match_keywords: List[str] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    scope_focus: Optional[str] = None
    icon: str = "🎯"
    color_token: str = "base"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CategoryDefinition:
        return cls(
            key=str(data.get("key", "")).strip().lower(),
            display_name=str(data.get("display_name", "")).strip(),
            safe_label=str(data.get("safe_label", "")).strip() or "Challenge Analysis",
            title_suffix=str(data.get("title_suffix", "")).strip() or "Challenge Analysis",
            match_keywords=[str(k).strip().lower() for k in data.get("match_keywords", []) if str(k).strip()],
            instructions=[str(i).strip() for i in data.get("instructions", []) if str(i).strip()],
            scope_focus=str(data["scope_focus"]).strip() if data.get("scope_focus") else None,
            icon=str(data.get("icon", "🎯")),
            color_token=str(data.get("color_token", "base")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "safe_label": self.safe_label,
            "title_suffix": self.title_suffix,
            "match_keywords": list(self.match_keywords),
            "instructions": list(self.instructions),
            "scope_focus": self.scope_focus,
            "icon": self.icon,
            "color_token": self.color_token,
        }
