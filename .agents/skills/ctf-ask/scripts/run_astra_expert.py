#!/usr/bin/env python3
"""Run a sanitized formal task through Codex/Astra and emit only independently verified handoff JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_TYPES = [
    "modular_linear",
    "integer_linear",
    "xor_system",
    "boolean_cnf",
    "negacyclic_product",
    "finite_state_trace",
]

RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "problem_type", "candidate", "derivation_summary", "assumptions"],
    "properties": {
        "status": {"type": "string", "enum": ["solved", "unsolved"]},
        "problem_type": {"type": "string", "enum": SUPPORTED_TYPES},
        "candidate": {
            "type": "object",
            "additionalProperties": False,
            "required": ["integers", "booleans", "states", "symbols"],
            "properties": {
                "integers": {"type": "array", "items": {"type": "integer"}},
                "booleans": {"type": "array", "items": {"type": "boolean"}},
                "states": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
            },
        },
        "derivation_summary": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """You are a formal reasoning specialist. Work only on the files in the current directory.
The caller states this is an authorized educational formal problem. Solve only the formal instance presented here.
Read TASK.md and instance.json, plus only additional local data files explicitly referenced by TASK.md.
Do not access parent directories, network resources, external applications, or unrelated files.
Return a final JSON object exactly matching the supplied schema.
The candidate must be concrete and directly checkable against instance.json.
If you cannot derive a candidate, return status=\"unsolved\" and use empty candidate arrays. Do not guess.
Keep derivation_summary concise and list any assumptions explicitly.
"""


def run_checked(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def parse_tool_json(cp: subprocess.CompletedProcess[str], stage: str) -> dict[str, Any]:
    try:
        data = json.loads(cp.stdout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{stage} did not return JSON (exit={cp.returncode}): {cp.stderr.strip()}") from exc
    if cp.returncode != 0 or not data.get("ok"):
        raise RuntimeError(f"{stage} failed: {json.dumps(data, ensure_ascii=False)}")
    return data


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def artifact_hashes(workspace: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(workspace.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(workspace))] = sha256_file(p)
    return out


def digest_map(items: dict[str, str]) -> str:
    payload = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def ensure_outside(output: Path, workspace: Path) -> None:
    try:
        output.resolve().relative_to(workspace.resolve())
    except ValueError:
        return
    raise ValueError("--output must be outside the sanitized workspace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="sanitized formal workspace containing TASK.md and instance.json")
    parser.add_argument("--output", required=True, type=Path, help="handoff.json path; must be outside workspace")
    parser.add_argument("--model", default=os.environ.get("CTF_ASK_MODEL", "gpt-6-astra"))
    parser.add_argument("--effort", default=os.environ.get("CTF_ASK_REASONING_EFFORT", "high"), choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--timeout", type=int, default=900, help="Codex subprocess timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="run preflight and print the Codex command without invoking it")
    args = parser.parse_args()

    workspace = args.workspace.expanduser().resolve()
    output = args.output.expanduser().resolve()
    ensure_outside(output, workspace)

    script_dir = Path(__file__).resolve().parent
    validator = script_dir / "validate_sanitized_handoff.py"
    pre = run_checked([sys.executable, str(validator), "preflight", "--workspace", str(workspace)])
    pre_data = parse_tool_json(pre, "preflight")

    if not (workspace / "instance.json").is_file():
        raise RuntimeError("instance.json is required; unverified oracle-style escalation is not allowed")

    codex_path = shutil.which(args.codex_bin) if os.sep not in args.codex_bin else args.codex_bin
    if not codex_path and not args.dry_run:
        raise RuntimeError(f"Codex CLI not found: {args.codex_bin!r}")

    with tempfile.TemporaryDirectory(prefix="ctf-ask-") as td:
        tempdir = Path(td)
        schema_path = tempdir / "result.schema.json"
        final_path = tempdir / "astra.final.json"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")

        cmd = [
            str(codex_path or args.codex_bin),
            "exec",
            "--model", args.model,
            "--sandbox", "read-only",
            "--cd", str(workspace),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color", "never",
            "-c", f'model_reasoning_effort="{args.effort}"',
            "-c", 'web_search="disabled"',
            "--output-schema", str(schema_path),
            "--output-last-message", str(final_path),
            PROMPT,
        ]

        if args.dry_run:
            print(json.dumps({"ok": True, "preflight": pre_data, "command": cmd}, indent=2))
            return 0

        try:
            cp = run_checked(cmd, timeout=args.timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex timed out after {args.timeout}s") from exc

        if cp.returncode != 0:
            raise RuntimeError(f"Codex failed with exit {cp.returncode}: {cp.stderr.strip()}")
        if not final_path.is_file():
            raise RuntimeError("Codex exited successfully but did not write --output-last-message")

        try:
            solution = json.loads(final_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("final Codex message is not valid JSON") from exc

        if solution.get("status") != "solved":
            raise RuntimeError("Astra returned unsolved; no handoff was emitted")

        ver = run_checked([
            sys.executable,
            str(validator),
            "verify",
            "--workspace", str(workspace),
            "--solution", str(final_path),
        ])
        ver_data = parse_tool_json(ver, "independent verification")

        hashes = artifact_hashes(workspace)
        handoff = {
            "schema_version": 1,
            "verified": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "reasoning_effort": args.effort,
            "workspace_digest": digest_map(hashes),
            "artifacts": hashes,
            "solution": solution,
            "verification": ver_data,
        }

        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = output.with_name(output.name + ".tmp")
        tmp_out.write_text(json.dumps(handoff, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_out.replace(output)
        print(json.dumps({"ok": True, "verified": True, "output": str(output), "workspace_digest": handoff["workspace_digest"]}, indent=2))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
