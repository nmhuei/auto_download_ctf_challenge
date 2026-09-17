"""Session forking and branching utilities for Antigravity (Agy) sessions."""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_gemini_cli_dir() -> Path:
    """Return root antigravity-cli directory (default ~/.gemini/antigravity-cli)."""
    custom = os.environ.get("GEMINI_CLI_DIR") or os.environ.get("ANTIGRAVITY_CLI_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / ".gemini" / "antigravity-cli"


def fork_agy_session(
    source_session_id: str,
    *,
    new_session_id: Optional[str] = None,
    title: Optional[str] = None,
    cli_dir: Optional[Path] = None,
) -> Optional[str]:
    """Clone an existing agy session to create an independent branch.

    Copies:
    1. brain/<source_id>/ -> brain/<new_id>/
    2. conversations/<source_id>.db -> conversations/<new_id>.db
    3. Updates cascade_id and trajectory_id in <new_id>.db
    4. Inserts branched entry into conversation_summaries.db

    Returns the new session UUID on success, or None if source does not exist.
    """
    if not source_session_id or not source_session_id.strip():
        return None

    source_id = source_session_id.strip()
    root = (cli_dir or get_gemini_cli_dir()).resolve()

    source_brain = root / "brain" / source_id
    source_db = root / "conversations" / f"{source_id}.db"

    # If the physical session files don't exist on disk, we cannot fork
    if not source_brain.is_dir() or not source_db.is_file():
        return None

    target_id = (new_session_id or str(uuid.uuid4())).strip()
    target_brain = root / "brain" / target_id
    target_db = root / "conversations" / f"{target_id}.db"
    summaries_db = root / "conversation_summaries.db"

    try:
        # 1. Copy brain directory tree
        if target_brain.exists():
            shutil.rmtree(target_brain)
        shutil.copytree(source_brain, target_brain)

        # 2. Copy SQLite conversation db using online backup API to ensure WAL consistency
        target_db.parent.mkdir(parents=True, exist_ok=True)
        target_db.unlink(missing_ok=True)
        with sqlite3.connect(str(source_db)) as src_conn, sqlite3.connect(str(target_db)) as dst_conn:
            src_conn.backup(dst_conn)

        # 3. Mutate trajectory_meta in target_db
        new_traj_id = str(uuid.uuid4())
        with sqlite3.connect(str(target_db)) as conn:
            c = conn.cursor()
            with contextlib.suppress(sqlite3.OperationalError):
                c.execute(
                    "UPDATE trajectory_meta SET cascade_id = ?, trajectory_id = ?",
                    (target_id, new_traj_id),
                )
            conn.commit()

        # 4. Insert or copy entry in conversation_summaries.db if exists
        if summaries_db.is_file():
            with sqlite3.connect(str(summaries_db)) as sum_conn:
                sc = sum_conn.cursor()
                try:
                    sc.execute(
                        "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                        (source_id,),
                    )
                    row = sc.fetchone()
                    if row:
                        row_list = list(row)
                        # Column 0 is conversation_id
                        row_list[0] = target_id
                        # Update title if given
                        if title and len(row_list) > 1:
                            row_list[1] = title
                        elif len(row_list) > 1 and row_list[1]:
                            row_list[1] = f"{row_list[1]} (Fork)"
                        # Update parent_conversation_id if present in schema
                        cols = [desc[0] for desc in sc.description]
                        if "parent_conversation_id" in cols:
                            p_idx = cols.index("parent_conversation_id")
                            row_list[p_idx] = source_id
                        if "last_modified_time" in cols:
                            m_idx = cols.index("last_modified_time")
                            row_list[m_idx] = datetime.now(timezone.utc).isoformat()

                        placeholders = ",".join(["?"] * len(row_list))
                        sc.execute(
                            f"INSERT OR REPLACE INTO conversation_summaries VALUES ({placeholders})",
                            row_list,
                        )
                        sum_conn.commit()
                except sqlite3.OperationalError:
                    pass

        return target_id
    except Exception:
        # Cleanup on failure
        if target_brain.exists():
            shutil.rmtree(target_brain, ignore_errors=True)
        if target_db.exists():
            target_db.unlink(missing_ok=True)
        return None
