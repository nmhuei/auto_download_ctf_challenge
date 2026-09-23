"""Category registry loading and managing JSON-defined CTF categories."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Dict, List, Optional

from .models import CategoryDefinition

_DEFAULT_DEFINITIONS_PATH = Path(__file__).parent / "definitions.json"


import logging

logger = logging.getLogger(__name__)


class CategoryRegistry:
    """Manages discoverable, extensible CTF challenge category schemas."""

    _instances: Dict[Optional[str], CategoryRegistry] = {}
    _categories: Dict[str, CategoryDefinition] = {}
    _order: List[str] = []

    def __init__(self, custom_file: Optional[Path] = None, workspace: Optional[Path] = None):
        self._categories = {}
        self._order = []
        self._load_defaults()
        if custom_file and custom_file.is_file():
            self._load_file(custom_file)
        if workspace:
            self._load_workspace_overrides(workspace)

    @classmethod
    def get_instance(cls, workspace: Optional[Path] = None) -> CategoryRegistry:
        key = str(Path(workspace).expanduser().resolve()) if workspace else None
        if key not in cls._instances:
            cls._instances[key] = cls(workspace=workspace)
        return cls._instances[key]

    @classmethod
    def reset(cls) -> None:
        """Reset all cached instances (useful in tests)."""
        cls._instances.clear()

    def _load_defaults(self) -> None:
        if _DEFAULT_DEFINITIONS_PATH.is_file():
            self._load_file(_DEFAULT_DEFINITIONS_PATH)

    def _load_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("Category file %s must contain a JSON object", path)
                return
            cats = raw.get("categories", [])
            if not isinstance(cats, list):
                logger.warning("Category file %s 'categories' must be a list", path)
                return

            validated: List[CategoryDefinition] = []
            for item in cats:
                if not isinstance(item, dict):
                    logger.warning("Skipping non-dict category item in %s", path)
                    continue
                try:
                    cat = CategoryDefinition.from_dict(item)
                    if cat.key and cat.display_name:
                        validated.append(cat)
                    else:
                        logger.warning("Skipping category item missing key or display_name in %s", path)
                except Exception as ex:
                    logger.warning("Failed parsing category item in %s: %s", path, ex)

            for cat in validated:
                if cat.key not in self._categories:
                    self._order.append(cat.key)
                self._categories[cat.key] = cat
        except Exception as e:
            logger.warning("Failed loading category definitions from %s: %s", path, e)

    def _load_workspace_overrides(self, workspace: Path) -> None:
        ws = Path(workspace).expanduser().resolve()
        override_file = ws / ".ctf" / "categories.json"
        if override_file.is_file():
            self._load_file(override_file)

    def register(self, definition: CategoryDefinition) -> None:
        """Register or override a category definition at runtime."""
        if definition.key not in self._categories:
            self._order.append(definition.key)
        self._categories[definition.key] = definition

    def all_categories(self) -> List[CategoryDefinition]:
        return [self._categories[k] for k in self._order if k in self._categories]

    def normalize(self, category: Optional[str]) -> str:
        """Normalize any free-form category string to a canonical registered key."""
        if not category:
            return "general"
        clean = category.strip().lower()

        # 1. Exact key match
        if clean in self._categories:
            return clean

        # 2. Exact display name match
        for key, cat in self._categories.items():
            if clean == cat.display_name.lower():
                return key

        # 3. Keyword matching (prioritize longer, more specific keywords; word boundary for <= 3 chars)
        candidates: List[tuple[int, str]] = []
        for key in self._order:
            cat = self._categories.get(key)
            if not cat:
                continue
            for kw in cat.match_keywords:
                # For short keywords (<= 3 chars, e.g. 'rf', 'vm', 'ai', 'rev'), require word boundary
                if len(kw) <= 3:
                    if re.search(r"\b" + re.escape(kw) + r"\b", clean):
                        candidates.append((len(kw), key))
                else:
                    if kw in clean:
                        candidates.append((len(kw), key))

        if candidates:
            # Sort by keyword length descending; longest match wins
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

        return "general"

    def get(self, category_or_key: Optional[str]) -> CategoryDefinition:
        """Get the full category definition, falling back to 'general'."""
        key = self.normalize(category_or_key)
        if key in self._categories:
            return self._categories[key]
        if "general" in self._categories:
            return self._categories["general"]
        return CategoryDefinition(
            key="general",
            display_name="General",
            safe_label="Challenge Analysis",
            title_suffix="Challenge Analysis",
            instructions=[
                "Inspect metadata.json, challenge/NOTE.md, and all challenge attachments.",
                "Build diagnostic test harnesses or analysis scripts in script/.",
                "Document your hypotheses, findings, and technical notes in script/analysis.md.",
                "If a candidate flag is found, save it to flag.txt.",
            ],
            icon="🎯",
            color_token="base",
        )

    def safe_label(self, category: Optional[str]) -> str:
        return self.get(category).safe_label

    def icon(self, category: Optional[str]) -> str:
        return self.get(category).icon

    def color_token(self, category: Optional[str]) -> str:
        return self.get(category).color_token
