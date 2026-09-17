"""Antigravity (Agy) CLI binary detection and resolution helpers."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def resolve_agy_binary(candidate: str = "agy") -> str:
    """Resolve executable path for the agy binary.

    Resolution strategy:
    1. If candidate contains path separators, check if file exists.
    2. Check environment variables AGY_BIN or AGY_PATH.
    3. Check system PATH via shutil.which.
    4. Fallback to standard installation locations (e.g. ~/.local/bin/agy).
    5. Return candidate as-is if not found elsewhere.
    """
    if not candidate:
        candidate = "agy"
    if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
        return candidate
    env_bin = os.environ.get("AGY_BIN") or os.environ.get("AGY_PATH")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    found = shutil.which(candidate)
    if found:
        return found
    if candidate == "agy":
        fallbacks = [
            Path.home() / ".local" / "bin" / "agy",
            Path(sys.prefix) / "bin" / "agy",
            Path("/usr/local/bin/agy"),
            Path("/usr/bin/agy"),
        ]
        for p in fallbacks:
            if p.is_file() and os.access(str(p), os.X_OK):
                return str(p)
    return candidate


def is_agy_available(candidate: str = "agy") -> bool:
    """Return True if agy binary can be resolved to an executable file."""
    path = resolve_agy_binary(candidate)
    if shutil.which(path):
        return True
    return os.path.isfile(path) and os.access(path, os.X_OK)


def extract_json_payload(output: str) -> Optional[Dict[str, Any]]:
    """Robustly extract a JSON object from CLI output that may include logs or banners."""
    output = output.strip()
    if not output:
        return None
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    start = output.find("{")
    end = output.rfind("}")
    if start != -1 and end != -1 and end > start:
        chunk = output[start:end + 1]
        try:
            data = json.loads(chunk)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except (ValueError, TypeError):
                continue
    return None
