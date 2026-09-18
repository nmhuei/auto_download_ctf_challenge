"""SuperBQA background supervisor daemon.

Runs detached from terminal/session to supervise AI solver workers,
stream logs, monitor health/stalls, and finalize challenge solutions.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .solver_service import (
    SolverService,
    get_pid_start_ticks,
    get_system_boot_id,
)
from ..storage.fileio import atomic_write_json, locked_update_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_daemon(
    workspace: Path,
    ids: str,
    workers: int,
    timeout: int,
    stale_timeout: int,
    reuse_session: bool = True,
    per_category: bool = False,
) -> int:
    control_dir = workspace / ".ctf-solver"
    control_dir.mkdir(parents=True, exist_ok=True)
    lock_path = control_dir / "manager.lock"
    state_path = control_dir / "manager-state.json"
    log_path = control_dir / "supervisor.log"

    lock_handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_now()}] Another SuperBQA daemon is already holding manager.lock. Exiting.\n")
        return 2

    pid = os.getpid()
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(pid))
    lock_handle.flush()

    boot_id = get_system_boot_id()
    start_ticks = get_pid_start_ticks(pid)

    daemon_state = {
        "daemon_pid": pid,
        "pid_start_ticks": start_ticks,
        "boot_id": boot_id,
        "started_at": _now(),
        "updated_at": _now(),
        "workspace": str(workspace.resolve()),
        "target_ids": ids,
        "workers": workers,
        "status": "running",
        "active_ids": [],
        "completed_ids": [],
        "error": None,
    }
    atomic_write_json(state_path, daemon_state)

    service = SolverService(
        workspace,
        timeout_seconds=timeout,
        stale_seconds=stale_timeout,
    )

    stop_requested = False

    def _handle_sigterm(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_now()}] Daemon received signal {signum}. Stopping active workers.\n")

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    def _on_refresh():
        if stop_requested:
            raise KeyboardInterrupt("Daemon stop requested via signal.")
        active = []
        completed = []
        for job in service.scan():
            st = service.read_job(job)
            if st.get("state") in ("starting", "running"):
                active.append(str(job.display_id))
            elif st.get("state") in ("completed", "failed", "cancelled", "filtered", "skipped_no_source"):
                completed.append(str(job.display_id))

        def mutate(cur):
            cur["updated_at"] = _now()
            cur["active_ids"] = active
            cur["completed_ids"] = completed
            return cur

        locked_update_json(state_path, mutate)

    exit_code = 0
    try:
        results = service.run(
            ids,
            workers=workers,
            on_refresh=_on_refresh,
            acquire_lock=False,
            reuse_session=reuse_session,
            per_category=per_category,
        )
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_now()}] Daemon completed work for ids '{ids}'. Total results: {len(results)}\n")
    except BaseException as exc:
        exit_code = 1
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_now()}] Daemon error/signal: {exc}\n")

        def mutate_err(cur):
            cur["status"] = "cancelled" if stop_requested else "error"
            cur["error"] = str(exc)
            cur["ended_at"] = _now()
            cur["active_ids"] = []
            return cur

        locked_update_json(state_path, mutate_err)
    else:
        def mutate_idle(cur):
            cur["status"] = "idle"
            cur["active_ids"] = []
            cur["ended_at"] = _now()
            return cur

        locked_update_json(state_path, mutate_idle)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    return exit_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SuperBQA Background Solver Supervisor Daemon")
    parser.add_argument("--workspace", required=True, help="Path to CTF workspace")
    parser.add_argument("--ids", required=True, help="Challenge display IDs to solve")
    parser.add_argument("--workers", type=int, default=3, help="Max worker concurrency")
    parser.add_argument("--timeout", type=int, default=3600, help="Per-worker timeout seconds")
    parser.add_argument("--stale-timeout", type=int, default=300, help="Stale heartbeat timeout seconds")
    parser.add_argument("--new-session", action="store_true", help="Start new sessions instead of reusing category sessions")
    parser.add_argument("--per-category", action="store_true", help="Allow one worker slot per category")
    args = parser.parse_args(argv)

    ws = Path(args.workspace).resolve()
    sys.exit(run_daemon(
        ws,
        args.ids,
        args.workers,
        args.timeout,
        args.stale_timeout,
        reuse_session=not args.new_session,
        per_category=args.per_category,
    ))


if __name__ == "__main__":
    main()
