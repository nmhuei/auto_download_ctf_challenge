"""Shared, workspace-aware configuration for the local solver scheduler."""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_MAX_WORKERS = 3
_ENV_WORKERS_KEY = "CTF_SOLVER_MAX_WORKERS"
_ENV_ENGINE_KEY = "CTF_SOLVER_ENGINE"
_FALLBACK_DEFAULT_ENGINE = "agy"


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
    raw = os.environ.get(_ENV_WORKERS_KEY)
    if raw is None and workspace is not None:
        raw = _dotenv_value(Path(workspace).expanduser(), _ENV_WORKERS_KEY)
    try:
        value = int(raw) if raw is not None else DEFAULT_MAX_WORKERS
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS
    return value if value >= 1 else DEFAULT_MAX_WORKERS


def solver_default_engine(workspace: str | Path | None = None) -> str:
    """Return the remembered or configured solver engine.

    Order of precedence:
    1. Environment variable: CTF_SOLVER_ENGINE (or CTF_ENGINE).
    2. Workspace-level .env: CTF_SOLVER_ENGINE.
    3. Workspace-level memory: <workspace>/.ctf-solver/last_engine.txt.
    4. Global config: ~/.config/ctf_toolkit/config.json ['solver_engine'].
    5. Default fallback: 'agy'.
    """
    env_eng = os.environ.get(_ENV_ENGINE_KEY) or os.environ.get("CTF_ENGINE")
    if env_eng:
        return str(env_eng).strip().lower()

    ws_path = Path(workspace).expanduser().resolve() if workspace else None
    if ws_path:
        env_file_eng = _dotenv_value(ws_path, _ENV_ENGINE_KEY) or _dotenv_value(ws_path, "CTF_ENGINE")
        if env_file_eng:
            return str(env_file_eng).strip().lower()

        last_eng_file = ws_path / ".ctf-solver" / "last_engine.txt"
        if last_eng_file.is_file():
            try:
                content = last_eng_file.read_text(encoding="utf-8").strip().lower()
                if content:
                    return content
            except OSError:
                pass

    try:
        from ..storage.global_config import load_global_config
        cfg = load_global_config()
        last = cfg.get("solver_engine") or cfg.get("last_solver_engine")
        if last:
            return str(last).strip().lower()
    except Exception:
        pass

    return _FALLBACK_DEFAULT_ENGINE


def set_last_solver_engine(
    engine: str,
    workspace: str | Path | None = None,
    persist_global: bool = False,
) -> None:
    """Persist the last selected solver engine in the active workspace and optionally globally."""
    clean_eng = str(engine or "").strip().lower()
    if not clean_eng:
        return

    # 1. Save to workspace-level state
    ws_path = Path(workspace).expanduser().resolve() if workspace else None
    if ws_path:
        try:
            solver_dir = ws_path / ".ctf-solver"
            solver_dir.mkdir(parents=True, exist_ok=True)
            (solver_dir / "last_engine.txt").write_text(clean_eng, encoding="utf-8")
        except OSError:
            pass

    # 2. Save to global config (~/.config/ctf_toolkit/config.json) when requested or no workspace
    if ws_path is None or persist_global:
        try:
            from ..storage.global_config import update_global_config
            def _mut(cfg: dict) -> dict:
                cfg["solver_engine"] = clean_eng
                cfg["last_solver_engine"] = clean_eng
                return cfg
            update_global_config(_mut)
        except Exception:
            pass
