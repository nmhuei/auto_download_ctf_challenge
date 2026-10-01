"""Shared, workspace-aware configuration for the local solver scheduler."""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_MAX_WORKERS = 3
_ENV_KEY = "CTF_SOLVER_MAX_WORKERS"


def _dotenv_value(workspace: Path, key: str) -> str | None:
    """Read one simple KEY=value setting from a workspace-local .env file."""
    path = workspace / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip().removeprefix("export ").strip() == key:
            return value.strip().strip("\"'")
    return None


def solver_max_workers(workspace: str | Path | None = None) -> int:
    """Return the configured global solver limit, defaulting to three workers."""
    raw = os.environ.get(_ENV_KEY)
    if raw is None and workspace is not None:
        raw = _dotenv_value(Path(workspace).expanduser(), _ENV_KEY)
    try:
        value = int(raw) if raw is not None else DEFAULT_MAX_WORKERS
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS
    return value if value >= 1 else DEFAULT_MAX_WORKERS
