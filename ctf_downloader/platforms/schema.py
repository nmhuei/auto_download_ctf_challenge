"""Platform Schema Data Models for Extensible & Data-Driven CTF Platforms.

Allows defining, serializing, and dynamically registering CTF platform structures
(endpoints, JSON field mappings, HTML markers, cookie hints, auth methods)
without touching or hardcoding core Python adapter files.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def validate_platform_key(key: str) -> str:
    """Validate and sanitize a platform identifier key to prevent directory traversal."""
    raw = str(key or "").strip()
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError(f"Platform key cannot contain path separators or directory traversal: {key!r}")
    clean = re.sub(r"[^a-z0-9_-]", "_", raw.lower()).strip("_")
    if not clean or clean in (".", "..") or not re.match(r"^[a-z0-9][a-z0-9_-]*$", clean):
        raise ValueError(f"Invalid platform key format: {key!r}")
    return clean


@dataclass
class PlatformEndpoints:
    """API endpoint routes for the CTF platform."""
    auth_check: Optional[str] = None           # e.g. "/api/auth/me" or "/api/v1/user"
    challenges: Optional[str] = None           # e.g. "/api/challenges" or "/api/v1/challenges"
    challenge_detail: Optional[str] = None     # e.g. "/api/challenges/{id}"
    submit: Optional[str] = None               # e.g. "/api/challenges/{id}/submit" or "/api/v1/submit"
    scoreboard: Optional[str] = None           # e.g. "/api/scoreboard" or "/api/v1/scoreboard"
    instance_spawn: Optional[str] = None       # e.g. "/api/container/spawn"
    instance_stop: Optional[str] = None        # e.g. "/api/container/stop"
    instance_renew: Optional[str] = None       # e.g. "/api/container/renew"

    def __post_init__(self):
        for field_name in (
            "auth_check", "challenges", "challenge_detail", "submit",
            "scoreboard", "instance_spawn", "instance_stop", "instance_renew",
        ):
            val = getattr(self, field_name, None)
            if val is not None:
                setattr(self, field_name, str(val).strip())

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class PlatformSchemaMapping:
    """JSON response path and field mapping for challenges and metadata."""
    challenges_root: str = "data.challenges"
    id: str = "id"
    name: str = "name"
    category: str = "category"
    points: str = "points"
    description: str = "description"
    files: str = "files"
    solved: str = "solved"
    connection_info: str = "connection_info"
    solves_count: str = "solves_count"

    def __post_init__(self):
        for field_name in (
            "challenges_root", "id", "name", "category", "points",
            "description", "files", "solved", "connection_info", "solves_count",
        ):
            val = getattr(self, field_name, "")
            setattr(self, field_name, str(val).strip() if val is not None else "")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlatformSubmitSpec:
    """Flag submission request and response verification rules."""
    method: str = "POST"                       # POST, PUT, or PATCH
    flag_param: str = "flag"                   # JSON payload key or form parameter
    id_param: Optional[str] = None             # None if ID is in URL path, or param name
    content_type: str = "json"                 # "json" or "form"
    success_field: str = "success"             # "success", "status:success", "data.status == 200"
    message_field: str = "message"

    def __post_init__(self):
        m = str(self.method or "POST").strip().upper()
        self.method = m if m in ("POST", "PUT", "PATCH", "GET") else "POST"
        self.flag_param = str(self.flag_param or "flag").strip()
        self.id_param = str(self.id_param).strip() if self.id_param is not None else None
        ct = str(self.content_type or "json").strip().lower()
        self.content_type = ct if ct in ("json", "form") else "json"
        self.success_field = str(self.success_field or "success").strip()
        self.message_field = str(self.message_field or "message").strip()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlatformSchema:
    """Complete specification of a data-driven CTF platform structure."""
    key: str                                   # Unique identifier, e.g. "metactf", "huntress"
    label: str                                 # Display label, e.g. "MetaCTF"
    throttle: float = 3.0                      # Min seconds between submits
    html_markers: List[str] = field(default_factory=list)      # Strings or "regex:..." on root HTML
    cookie_hints: List[str] = field(default_factory=list)      # Cookie names
    url_patterns: List[str] = field(default_factory=list)      # URL substrings/patterns
    endpoints: PlatformEndpoints = field(default_factory=PlatformEndpoints)
    schema_mapping: PlatformSchemaMapping = field(default_factory=PlatformSchemaMapping)
    submit_spec: PlatformSubmitSpec = field(default_factory=PlatformSubmitSpec)
    supports_container: bool = False
    supports_scoreboard: bool = False
    source: str = "custom"                     # "builtin", "global", or "workspace"
    description: str = ""

    def __post_init__(self):
        self.key = validate_platform_key(self.key)
        try:
            th = float(self.throttle)
            if not math.isfinite(th) or th <= 0:
                self.throttle = 3.0
            else:
                self.throttle = max(0.1, min(60.0, th))
        except (ValueError, TypeError):
            self.throttle = 3.0

        clean_markers = []
        for m in self.html_markers:
            m_str = str(m).strip()
            if not m_str:
                continue
            if m_str.startswith("regex:"):
                pat = m_str[len("regex:"):]
                if len(pat) > 256:
                    continue
                try:
                    re.compile(pat)
                    clean_markers.append(m_str)
                except re.error:
                    continue
            else:
                clean_markers.append(m_str)
        self.html_markers = clean_markers

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "throttle": self.throttle,
            "html_markers": list(self.html_markers),
            "cookie_hints": list(self.cookie_hints),
            "url_patterns": list(self.url_patterns),
            "endpoints": self.endpoints.to_dict(),
            "schema_mapping": self.schema_mapping.to_dict(),
            "submit_spec": self.submit_spec.to_dict(),
            "supports_container": self.supports_container,
            "supports_scoreboard": self.supports_scoreboard,
            "source": self.source,
            "description": self.description,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: Optional[str] = None) -> PlatformSchema:
        raw_key = str(data.get("key", "")).strip().lower()
        clean_key = validate_platform_key(raw_key)

        ep_data = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else {}
        endpoints = PlatformEndpoints(**{k: v for k, v in ep_data.items() if hasattr(PlatformEndpoints, k)})

        map_data = data.get("schema_mapping") if isinstance(data.get("schema_mapping"), dict) else {}
        schema_mapping = PlatformSchemaMapping(**{k: v for k, v in map_data.items() if hasattr(PlatformSchemaMapping, k)})

        sub_data = data.get("submit_spec") if isinstance(data.get("submit_spec"), dict) else {}
        submit_spec = PlatformSubmitSpec(**{k: v for k, v in sub_data.items() if hasattr(PlatformSubmitSpec, k)})

        try:
            th = float(data.get("throttle", 3.0))
            if not math.isfinite(th) or th <= 0:
                throttle = 3.0
            else:
                throttle = max(0.1, min(60.0, th))
        except (ValueError, TypeError):
            throttle = 3.0

        html_markers = [str(x) for x in data.get("html_markers", []) if x] if isinstance(data.get("html_markers"), list) else []
        cookie_hints = [str(x) for x in data.get("cookie_hints", []) if x] if isinstance(data.get("cookie_hints"), list) else []
        url_patterns = [str(x) for x in data.get("url_patterns", []) if x] if isinstance(data.get("url_patterns"), list) else []

        resolved_source = source if source is not None else str(data.get("source", "custom"))

        return cls(
            key=clean_key,
            label=str(data.get("label") or clean_key),
            throttle=throttle,
            html_markers=html_markers,
            cookie_hints=cookie_hints,
            url_patterns=url_patterns,
            endpoints=endpoints,
            schema_mapping=schema_mapping,
            submit_spec=submit_spec,
            supports_container=bool(data.get("supports_container", False)),
            supports_scoreboard=bool(data.get("supports_scoreboard", False)),
            source=resolved_source,
            description=str(data.get("description", "")),
        )

    @classmethod
    def from_json(cls, json_str: str, source: Optional[str] = None) -> PlatformSchema:
        return cls.from_dict(json.loads(json_str), source=source)
