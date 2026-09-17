"""Executable contract for the ctf-crypto specialist handoff gate."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "ctf-crypto"
VALIDATOR = SKILL_ROOT / "scripts" / "validate_handoff.py"


def run_validator(tmp_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(handoff)],
        capture_output=True,
        text=True,
        check=False,
    )


def valid_handoff() -> dict[str, object]:
    return {
        "schema_version": 1,
        "objective": "Find an integer vector x satisfying A*x = b modulo q.",
        "domain": "integer linear algebra",
        "variables": [{"name": "x", "domain": "Z^n", "bounds": "|x_i| <= 2"}],
        "constraints": ["A*x = b (mod q)", "||x||_2^2 = 64"],
        "artifacts": ["matrix_A.json", "vector_b.json"],
        "verification": ["Substitute x into A*x = b (mod q)."],
        "requested_output": ["solution.json", "reproduce.sage"],
    }


def test_math_only_handoff_is_accepted(tmp_path: Path) -> None:
    result = run_validator(tmp_path, valid_handoff())

    assert result.returncode == 0, result.stderr


def test_ctf_or_secret_language_blocks_specialist_handoff(tmp_path: Path) -> None:
    payload = valid_handoff()
    payload["objective"] = "Recover the CTF flag from the attached challenge."

    result = run_validator(tmp_path, payload)

    assert result.returncode != 0
    assert "not mathematics-only" in result.stderr


def test_expert_handoff_uses_codex_astra_high_and_resumes_one_thread(tmp_path: Path) -> None:
    workspace = tmp_path / "math_workspace"
    workspace.mkdir()
    (workspace / "handoff.json").write_text(json.dumps(valid_handoff()), encoding="utf-8")
    (workspace / "TASK.md").write_text("# Numerical Algebra Task\n", encoding="utf-8")
    (workspace / "matrix_A.json").write_text("[[1]]", encoding="utf-8")
    (workspace / "vector_b.json").write_text("[0]", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "codex.jsonl"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CODEX_TEST_LOG'], 'a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'astra-thread-1'}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CODEX_TEST_LOG": str(log),
    }
    runner = SKILL_ROOT / "scripts" / "run_astra_expert.py"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(runner), str(workspace)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[0][:5] == ["exec", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']
    assert "--dangerously-bypass-approvals-and-sandbox" in calls[0]
    assert calls[1][:3] == ["exec", "resume", "astra-thread-1"]
