"""Platform Schema Store for Extensible Platform Architecture.

Manages persistent storage, dynamic discovery, and runtime registry synchronization
of CTF platform definitions across three cascading tiers:
  1. Built-in bundled templates: ctf_downloader/platforms/definitions/*.json
  2. Global user overrides: ~/.config/ctf_toolkit/platforms/*.json
  3. Workspace-scoped definitions: <workspace>/.ctf/platforms/*.json
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Type

from .schema import PlatformSchema, validate_platform_key
from ..utils.logger import Logger

GLOBAL_PLATFORMS_DIR = Path.home() / ".config" / "ctf_toolkit" / "platforms"
BUILTIN_PLATFORMS_DIR = Path(__file__).resolve().parent / "definitions"


class PlatformSchemaStore:
    """Storage and registry synchronization manager for platform schemas."""

    @classmethod
    def get_global_dir(cls) -> Path:
        """Return global user platform definitions directory, creating if needed."""
        GLOBAL_PLATFORMS_DIR.mkdir(parents=True, exist_ok=True)
        return GLOBAL_PLATFORMS_DIR

    @classmethod
    def get_workspace_dir(cls, workspace_path: str | Path) -> Path:
        """Return workspace-specific platform definitions directory."""
        p = Path(workspace_path) / ".ctf" / "platforms"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def load_all(cls, workspace_path: Optional[str | Path] = None) -> Dict[str, PlatformSchema]:
        """Load and merge all schemas across built-in, global, and workspace tiers."""
        schemas: Dict[str, PlatformSchema] = {}

        # 1. Built-in templates
        if BUILTIN_PLATFORMS_DIR.is_dir():
            for f in sorted(BUILTIN_PLATFORMS_DIR.glob("*.json")):
                try:
                    s = PlatformSchema.from_json(f.read_text(encoding="utf-8"), source="builtin")
                    if s.key:
                        schemas[s.key] = s
                except Exception as e:
                    Logger.warning(f"Lỗi đọc built-in platform schema '{f.name}': {e}")

        # 2. Global user overrides
        if GLOBAL_PLATFORMS_DIR.is_dir():
            for f in sorted(GLOBAL_PLATFORMS_DIR.glob("*.json")):
                try:
                    s = PlatformSchema.from_json(f.read_text(encoding="utf-8"), source="global")
                    if s.key:
                        schemas[s.key] = s
                except Exception as e:
                    Logger.warning(f"Lỗi đọc global platform schema '{f.name}': {e}")

        # 3. Workspace-specific overrides
        ws_dir = None
        if workspace_path:
            ws_dir = Path(workspace_path) / ".ctf" / "platforms"
        elif (Path.cwd() / ".ctf" / "platforms").is_dir():
            ws_dir = Path.cwd() / ".ctf" / "platforms"

        if ws_dir and ws_dir.is_dir():
            for f in sorted(ws_dir.glob("*.json")):
                try:
                    s = PlatformSchema.from_json(f.read_text(encoding="utf-8"), source="workspace")
                    if s.key:
                        schemas[s.key] = s
                except Exception as e:
                    Logger.warning(f"Lỗi đọc workspace platform schema '{f.name}': {e}")

        return schemas

    @classmethod
    def validate_key(cls, key: str) -> str:
        """Sanitize and validate platform key to prevent directory traversal."""
        return validate_platform_key(key)

    @classmethod
    def get(cls, key: str, workspace_path: Optional[str | Path] = None) -> Optional[PlatformSchema]:
        """Retrieve a schema by its unique key."""
        clean_key = cls.validate_key(key)
        all_schemas = cls.load_all(workspace_path)
        return all_schemas.get(clean_key)

    @classmethod
    def save(
        cls,
        schema: PlatformSchema,
        scope: str = "global",
        workspace_path: Optional[str | Path] = None,
    ) -> Path:
        """Persist a platform schema into global or workspace storage."""
        clean_key = cls.validate_key(schema.key)
        schema.key = clean_key

        if scope == "workspace":
            if not workspace_path:
                raise ValueError("workspace_path is required when saving to workspace scope")
            target_dir = cls.get_workspace_dir(workspace_path)
            schema.source = "workspace"
        else:
            target_dir = cls.get_global_dir()
            schema.source = "global"

        file_path = (target_dir / f"{clean_key}.json").resolve()
        if file_path.parent != target_dir.resolve():
            raise ValueError(f"Path traversal detected in key: {clean_key}")

        file_path.write_text(schema.to_json(indent=2), encoding="utf-8")
        return file_path

    @classmethod
    def delete(
        cls,
        key: str,
        scope: str = "global",
        workspace_path: Optional[str | Path] = None,
    ) -> bool:
        """Delete a custom platform schema file and remove from runtime registry."""
        from .registry import PLATFORMS

        clean_key = cls.validate_key(key)
        if scope == "workspace" and workspace_path:
            target_dir = cls.get_workspace_dir(workspace_path)
        else:
            target_dir = cls.get_global_dir()

        f = (target_dir / f"{clean_key}.json").resolve()
        if f.parent != target_dir.resolve():
            raise ValueError(f"Path traversal detected in key: {clean_key}")

        deleted = False
        if f.is_file():
            f.unlink()
            deleted = True

        if clean_key in PLATFORMS and getattr(PLATFORMS[clean_key], "source", None) == "custom_schema":
            PLATFORMS.pop(clean_key, None)

        return deleted

    @classmethod
    def make_adapter_class(cls, schema: PlatformSchema) -> Type:
        """Dynamically generate a BasePlatform subclass for this schema."""
        from .configurable import ConfigurablePlatform

        class DynamicSchemaPlatform(ConfigurablePlatform):
            def __init__(self, base_url: str, session: any):
                super().__init__(base_url, session, schema=schema)

        class_name = "".join(part.capitalize() for part in schema.key.split("_")) + "Platform"
        DynamicSchemaPlatform.__name__ = class_name
        DynamicSchemaPlatform.__qualname__ = class_name
        DynamicSchemaPlatform.__doc__ = f"Dynamic platform adapter for {schema.label} ({schema.key})."
        return DynamicSchemaPlatform

    @classmethod
    def sync_to_registry(cls, workspace_path: Optional[str | Path] = None) -> int:
        """Register all stored schemas into the global platforms.registry.PLATFORMS."""
        from .registry import PLATFORMS, PlatformSpec

        schemas = cls.load_all(workspace_path)
        registered_count = 0

        # Purge stale custom schemas that no longer exist on disk
        for k in list(PLATFORMS.keys()):
            if getattr(PLATFORMS[k], "source", None) == "custom_schema" and k not in schemas:
                PLATFORMS.pop(k, None)

        for key, s in schemas.items():
            # If already registered with a native Python class, we don't overwrite unless it's a schema override
            if key in PLATFORMS and getattr(PLATFORMS[key], "source", None) != "custom_schema":
                continue

            adapter_cls = cls.make_adapter_class(s)
            spec = PlatformSpec(
                key=s.key,
                label=s.label,
                cls=adapter_cls,
                throttle=s.throttle,
                html_markers=tuple(s.html_markers),
                cookie_hints=tuple(s.cookie_hints),
                supports_container=s.supports_container,
                supports_scoreboard=s.supports_scoreboard,
                source="custom_schema",
            )
            PLATFORMS[s.key] = spec
            adapter_cls.spec = spec
            registered_count += 1

        return registered_count
