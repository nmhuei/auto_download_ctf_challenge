#!/usr/bin/env python3
"""Run or resume the mathematics-only Codex Astra expert session for one workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from validate_handoff import validate


SESSION_NAME = ".ctf-crypto-codex-astra-session.json"


def digest_workspace(workspace: Path, handoff: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(handoff, sort_keys=True, separators=(",", ":")).encode())
    for name in handoff["artifacts"]:
        artifact = workspace / name
        digest.update(name.encode())
        digest.update(artifact.read_bytes())
    digest.update((workspace / "TASK.md").read_bytes())
    return digest.hexdigest()


def load_session(path: Path, digest: str) -> str | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    conversation = record.get("conversation_id")
    return conversation if record.get("handoff_digest") == digest and isinstance(conversation, str) else None


def thread_id(jsonl: str) -> str | None:
    for line in jsonl.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "thread.started":
            continue
        value = payload.get("thread_id")
        if isinstance(value, str) and value:
            return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("math_workspace", type=Path)
    parser.add_argument("--new", action="store_true", help="start a new expert conversation")
    args = parser.parse_args()

    workspace = args.math_workspace.resolve()
    handoff_path = workspace / "handoff.json"
    task_path = workspace / "TASK.md"
    if not task_path.is_file():
        print("TASK.md is required", file=sys.stderr)
        return 2
    try:
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read handoff: {exc}", file=sys.stderr)
        return 2

    errors = validate(handoff)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    missing = [name for name in handoff["artifacts"] if not (workspace / name).is_file()]
    if missing:
        print(f"missing mathematical artifact(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    digest = digest_workspace(workspace, handoff)
    session_path = workspace / SESSION_NAME
    prior = None if args.new else load_session(session_path, digest)
    prompt = (
        "Read TASK.md and handoff.json. Solve only the mathematics-only task using the named "
        "artifacts in this directory. Produce the required deliverables and run the stated checks."
    )
    command = ["codex", "exec"]
    if prior:
        command.extend(("resume", prior, "--json", "--dangerously-bypass-approvals-and-sandbox", prompt))
    else:
        command.extend((
            "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"',
            "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "--json", "-C", str(workspace), prompt,
        ))

    try:
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"could not start Codex CLI: {exc}", file=sys.stderr)
        return 127
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    try:
        current = thread_id(result.stdout)
    except (TypeError, ValueError):
        current = None
    if not current:
        print("Codex CLI did not return a thread ID", file=sys.stderr)
        return 1
    session_path.write_text(json.dumps({"handoff_digest": digest, "conversation_id": current}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
