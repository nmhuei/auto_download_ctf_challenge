"""Persistent session store for CTF solver engines (.ctf-solver/sessions.json)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fileio import locked_update_json, atomic_write_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SolverSessionStore:
    """Manages persistent session tracking for multiple solver engines per workspace."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.control_dir = self.workspace / ".ctf-solver"
        self.sessions_path = self.control_dir / "sessions.json"

    def _ensure_dir(self) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True)

    def get_session(self, challenge_id: str | int, engine: str) -> Optional[str]:
        """Return the current active session ID for a challenge and engine."""
        cid = str(challenge_id)
        if not self.sessions_path.is_file():
            return None
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
            chal = data.get("sessions", {}).get(cid, {})
            engine_info = chal.get("engines", {}).get(engine, {})
            sess_id = engine_info.get("current_session_id")
            return str(sess_id) if sess_id else None
        except Exception:
            return None

    def save_session(
        self,
        challenge_id: str | int,
        engine: str,
        session_id: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register or update active session ID for a challenge and engine."""
        self._ensure_dir()
        cid = str(challenge_id)
        now_ts = _now()

        def _mutate(cur: dict) -> dict:
            cur.setdefault("workspace", str(self.workspace))
            cur["updated_at"] = now_ts
            sessions = cur.setdefault("sessions", {})
            chal = sessions.setdefault(cid, {})
            if metadata:
                for k, v in metadata.items():
                    if k not in chal or not chal[k]:
                        chal[k] = v
            engines = chal.setdefault("engines", {})
            eng_entry = engines.setdefault(engine, {
                "created_at": now_ts,
                "continuation_count": 0,
                "session_history": [],
            })
            if eng_entry.get("current_session_id") != session_id:
                eng_entry["current_session_id"] = session_id
                eng_entry["created_at"] = now_ts
            eng_entry["last_used_at"] = now_ts
            eng_entry["status"] = "active"
            return cur

        locked_update_json(self.sessions_path, _mutate)

    def increment_continuation(
        self,
        challenge_id: str | int,
        engine: str,
    ) -> int:
        """Increment continuation count and touch last_used_at timestamp."""
        self._ensure_dir()
        cid = str(challenge_id)
        now_ts = _now()
        count = 1

        def _mutate(cur: dict) -> dict:
            nonlocal count
            cur.setdefault("workspace", str(self.workspace))
            cur["updated_at"] = now_ts
            sessions = cur.setdefault("sessions", {})
            chal = sessions.setdefault(cid, {})
            engines = chal.setdefault("engines", {})
            eng_entry = engines.setdefault(engine, {
                "created_at": now_ts,
                "continuation_count": 0,
                "session_history": [],
            })
            count = int(eng_entry.get("continuation_count", 0)) + 1
            eng_entry["continuation_count"] = count
            eng_entry["last_used_at"] = now_ts
            return cur

        locked_update_json(self.sessions_path, _mutate)
        return count

    def archive_session(
        self,
        challenge_id: str | int,
        engine: str,
        reason: str = "filtered",
    ) -> Optional[str]:
        """Archive current session into session_history and clear active session_id.
        
        Returns the archived session_id if one was active.
        """
        self._ensure_dir()
        cid = str(challenge_id)
        now_ts = _now()
        archived_id: Optional[str] = None

        def _mutate(cur: dict) -> dict:
            nonlocal archived_id
            cur.setdefault("workspace", str(self.workspace))
            cur["updated_at"] = now_ts
            sessions = cur.setdefault("sessions", {})
            chal = sessions.setdefault(cid, {})
            engines = chal.setdefault("engines", {})
            eng_entry = engines.get(engine)
            if not eng_entry:
                return cur
            curr_id = eng_entry.get("current_session_id")
            if curr_id:
                archived_id = curr_id
                history = eng_entry.setdefault("session_history", [])
                history.append({
                    "session_id": curr_id,
                    "created_at": eng_entry.get("created_at", now_ts),
                    "archived_at": now_ts,
                    "continuation_count": eng_entry.get("continuation_count", 0),
                    "reason": reason,
                })
                eng_entry["current_session_id"] = None
                eng_entry["status"] = "archived"
                eng_entry["continuation_count"] = 0
            return cur

        locked_update_json(self.sessions_path, _mutate)
        return archived_id

    def list_sessions(self, engine: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Return dict of challenge_id -> session info."""
        if not self.sessions_path.is_file():
            return {}
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
            res = {}
            for cid, chal in data.get("sessions", {}).items():
                if engine:
                    eng = chal.get("engines", {}).get(engine)
                    if eng:
                        res[cid] = eng
                else:
                    res[cid] = chal
            return res
        except Exception:
            return {}
