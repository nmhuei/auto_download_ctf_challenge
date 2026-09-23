"""Executable test suite for ctf-ask skill validator and runner across all categories."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "ctf-ask"
VALIDATOR = SKILL_ROOT / "scripts" / "validate_sanitized_handoff.py"
RUNNER = SKILL_ROOT / "scripts" / "run_astra_expert.py"


def run_validator_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *cmd],
        capture_output=True,
        text=True,
        check=False,
    )


def test_preflight_valid_formal_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "math_workspace"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Formal Discrete Optimization\nFind x in Z^2 satisfying constraints.\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[2, 3], [5, 1]],
        "b": [8, 9],
    }), encoding="utf-8")

    res = run_validator_cmd(["preflight", "--workspace", str(ws)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert not data["errors"]
    assert not data["findings"]


@pytest.mark.parametrize("forbidden_text", [
    "Recover the CTF flag from server",
    "Exploit buffer overflow to pop shell",
    "Analyze this vulnerability in the binary",
    "Shellcode payload to execute: \x90\x90",
    "Flag token is flag{test_secret_1234}",
    "Connect to http://challenge.ctf.local/api",
    "admin_token = secret_token_abc_123",
])
def test_preflight_rejects_cyber_domain_leakage(tmp_path: Path, forbidden_text: str) -> None:
    ws = tmp_path / "bad_workspace"
    ws.mkdir()
    (ws / "TASK.md").write_text(f"# Task description\n{forbidden_text}\n", encoding="utf-8")
    (ws / "instance.json").write_text("{}", encoding="utf-8")

    res = run_validator_cmd(["preflight", "--workspace", str(ws)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert len(data["findings"]) > 0


def test_verify_modular_linear(tmp_path: Path) -> None:
    ws = tmp_path / "ws_modular"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Formal Linear System\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[3, 5], [1, 2]],
        "b": [4, 7],
    }), encoding="utf-8")

    # 3*x0 + 5*x1 = 4 (mod 17), x0 + 2*x1 = 7 (mod 17) -> x0 = 7, x1 = 0
    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "modular_linear",
        "candidate": {"integers": [7, 0], "booleans": [], "states": [], "symbols": []},
        "derivation_summary": ["Gaussian elimination mod 17"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "2/2 modular equations satisfied" in data["checks"][0]

    # Test wrong candidate
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "modular_linear",
        "candidate": {"integers": [1, 1], "booleans": [], "states": [], "symbols": []},
    }), encoding="utf-8")
    res_wrong = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res_wrong.returncode != 0


def test_verify_boolean_cnf(tmp_path: Path) -> None:
    ws = tmp_path / "ws_cnf"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Boolean Satisfiability\n", encoding="utf-8")
    # 3 variables: (x1 or x2) and (!x1 or x3) and (!x2 or !x3)
    (ws / "instance.json").write_text(json.dumps({
        "type": "boolean_cnf",
        "num_vars": 3,
        "clauses": [[1, 2], [-1, 3], [-2, -3]],
    }), encoding="utf-8")

    # x1=True, x2=False, x3=True
    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "boolean_cnf",
        "candidate": {"booleans": [True, False, True], "integers": [], "states": [], "symbols": []},
        "derivation_summary": ["DPLL SAT solver"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "3/3 CNF clauses satisfied" in data["checks"][0]


def test_verify_finite_state_trace(tmp_path: Path) -> None:
    ws = tmp_path / "ws_fsm"
    ws.mkdir()
    (ws / "TASK.md").write_text("# State Machine Reachability\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "finite_state_trace",
        "start": "init",
        "accepting": ["goal"],
        "transitions": [
            {"from": "init", "symbol": "step_a", "to": "s1"},
            {"from": "s1", "symbol": "step_b", "to": "s2"},
            {"from": "s2", "symbol": "step_c", "to": "goal"},
        ],
    }), encoding="utf-8")

    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "finite_state_trace",
        "candidate": {
            "states": ["init", "s1", "s2", "goal"],
            "symbols": ["step_a", "step_b", "step_c"],
            "integers": [],
            "booleans": [],
        },
        "derivation_summary": ["BFS traversal"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "3 transitions valid" in data["checks"][0]


def test_verify_xor_system(tmp_path: Path) -> None:
    ws = tmp_path / "ws_xor"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Binary Parity Equations\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "xor_system",
        "num_vars": 4,
        "rows": [
            {"vars": [0, 1], "rhs": 1},
            {"vars": [1, 2], "rhs": 0},
            {"vars": [2, 3], "rhs": 1},
            {"vars": [0, 3], "rhs": 0},
        ],
    }), encoding="utf-8")

    # Solution: x = [1, 0, 0, 1] -> 1^0=1, 0^0=0, 0^1=1, 1^1=0
    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "xor_system",
        "candidate": {"integers": [1, 0, 0, 1], "booleans": [], "states": [], "symbols": []},
        "derivation_summary": ["GF(2) elimination"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "4/4 XOR equations satisfied" in data["checks"][0]


def test_verify_integer_linear(tmp_path: Path) -> None:
    ws = tmp_path / "ws_int"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Exact Integer System\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "integer_linear",
        "A": [[2, 1], [1, -1]],
        "b": [8, 1],
    }), encoding="utf-8")

    # 2*x0 + x1 = 8, x0 - x1 = 1 -> x0 = 3, x1 = 2
    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "integer_linear",
        "candidate": {"integers": [3, 2], "booleans": [], "states": [], "symbols": []},
        "derivation_summary": ["Exact rational solve"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True


def test_verify_negacyclic_product(tmp_path: Path) -> None:
    ws = tmp_path / "ws_nega"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Negacyclic Ring Product\n", encoding="utf-8")
    # n=2, modulus=7: a = [1, 2], x = [3, 4]
    # out[0] = a0*x0 - a1*x1 = 3 - 8 = -5 = 2 mod 7
    # out[1] = a0*x1 + a1*x0 = 4 + 6 = 10 = 3 mod 7
    (ws / "instance.json").write_text(json.dumps({
        "type": "negacyclic_product",
        "n": 2,
        "modulus": 7,
        "a": [1, 2],
        "b": [2, 3],
    }), encoding="utf-8")

    sol_file = tmp_path / "solution.json"
    sol_file.write_text(json.dumps({
        "status": "solved",
        "problem_type": "negacyclic_product",
        "candidate": {"integers": [3, 4], "booleans": [], "states": [], "symbols": []},
        "derivation_summary": ["Ring division"],
        "assumptions": [],
    }), encoding="utf-8")

    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_file)])
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "2/2 negacyclic coefficients satisfied" in data["checks"][0]


def test_runner_dry_run_and_boundary_checks(tmp_path: Path) -> None:
    ws = tmp_path / "ws_runner"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Math Task\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[1, 0]],
        "b": [5],
    }), encoding="utf-8")

    out_file = tmp_path / "handoff.json"
    cmd = [
        sys.executable, str(RUNNER),
        "--workspace", str(ws),
        "--output", str(out_file),
        "--dry-run",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "--sandbox" in data["command"]
    assert "read-only" in data["command"]
    assert "gpt-6-astra" in data["command"]

    # Test output inside workspace is strictly rejected
    bad_out = ws / "handoff.json"
    cmd_bad = [
        sys.executable, str(RUNNER),
        "--workspace", str(ws),
        "--output", str(bad_out),
        "--dry-run",
    ]
    res_bad = subprocess.run(cmd_bad, capture_output=True, text=True, check=False)
    assert res_bad.returncode != 0
    assert "outside the sanitized workspace" in res_bad.stderr


def test_runner_with_mock_codex_emits_verified_handoff(tmp_path: Path) -> None:
    ws = tmp_path / "ws_mock"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Linear Task\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[1]],
        "b": [5],
    }), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "out_file = None\n"
        "for i, arg in enumerate(sys.argv):\n"
        "    if arg == '--output-last-message': out_file = sys.argv[i + 1]\n"
        "if out_file:\n"
        "    with open(out_file, 'w') as f:\n"
        "        json.dump({\n"
        "            'status': 'solved',\n"
        "            'problem_type': 'modular_linear',\n"
        "            'candidate': {'integers': [5], 'booleans': [], 'states': [], 'symbols': []},\n"
        "            'derivation_summary': ['Direct substitution'],\n"
        "            'assumptions': []\n"
        "        }, f)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    out_file = tmp_path / "handoff.json"

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--workspace", str(ws), "--output", str(out_file)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    assert out_file.is_file()
    handoff = json.loads(out_file.read_text(encoding="utf-8"))
    assert handoff["verified"] is True
    assert handoff["solution"]["candidate"]["integers"] == [5]
    assert handoff["verification"]["ok"] is True





def test_validator_rejects_boolean_coercion_and_empty_systems(tmp_path: Path) -> None:
    ws = tmp_path / "ws_strict"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Formal Task\nVerify constraints\n", encoding="utf-8")

    # 1. Reject boolean in place of integer candidate
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[1]],
        "b": [1],
    }), encoding="utf-8")
    sol = tmp_path / "sol_bool.json"
    sol.write_text(json.dumps({
        "status": "solved",
        "problem_type": "modular_linear",
        "candidate": {"integers": [True]},
    }), encoding="utf-8")
    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("array of integers" in e for e in data["errors"])

    # 2. Reject empty XOR system
    (ws / "instance.json").write_text(json.dumps({
        "type": "xor_system",
        "num_vars": 2,
        "rows": [],
    }), encoding="utf-8")
    sol_xor = tmp_path / "sol_xor.json"
    sol_xor.write_text(json.dumps({
        "status": "solved",
        "problem_type": "xor_system",
        "candidate": {"integers": [0, 1]},
    }), encoding="utf-8")
    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_xor)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("non-empty" in e for e in data["errors"])

    # 3. Reject empty CNF clauses
    (ws / "instance.json").write_text(json.dumps({
        "type": "boolean_cnf",
        "num_vars": 2,
        "clauses": [],
    }), encoding="utf-8")
    sol_cnf = tmp_path / "sol_cnf.json"
    sol_cnf.write_text(json.dumps({
        "status": "solved",
        "problem_type": "boolean_cnf",
        "candidate": {"booleans": [True, False]},
    }), encoding="utf-8")
    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_cnf)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("non-empty" in e for e in data["errors"])

    # 4. Reject string disguise in FSM states
    (ws / "instance.json").write_text(json.dumps({
        "type": "finite_state_trace",
        "start": "S0",
        "transitions": [{"from": "S0", "symbol": "a", "to": "S1"}],
        "accepting": ["S1"],
    }), encoding="utf-8")
    sol_fsm = tmp_path / "sol_fsm.json"
    sol_fsm.write_text(json.dumps({
        "status": "solved",
        "problem_type": "finite_state_trace",
        "candidate": {"states": "S0S1", "symbols": ["a"]},
    }), encoding="utf-8")
    res = run_validator_cmd(["verify", "--workspace", str(ws), "--solution", str(sol_fsm)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False


def test_parse_tool_json_fail_closed() -> None:
    import importlib.util
    from unittest.mock import MagicMock

    spec = importlib.util.spec_from_file_location("run_astra_expert", str(RUNNER))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1. Non-zero exit code with non-JSON stdout
    p1 = MagicMock(returncode=1, stdout="crash", stderr="error")
    with pytest.raises(RuntimeError):
        mod.parse_tool_json(p1, "test")

    # 2. Zero exit code with malformed JSON
    p2 = MagicMock(returncode=0, stdout="not-json", stderr="")
    with pytest.raises(RuntimeError):
        mod.parse_tool_json(p2, "test")

    # 3. Zero exit code with ok: False
    p3 = MagicMock(returncode=0, stdout='{"ok": false, "errors": ["invalid"]}')
    with pytest.raises(RuntimeError):
        mod.parse_tool_json(p3, "test")

    # 4. Zero exit code with ok: True
    p4 = MagicMock(returncode=0, stdout='{"ok": true, "files": []}')
    res = mod.parse_tool_json(p4, "test")
    assert res["ok"] is True


def test_preflight_rejects_unisolated_challenge_files(tmp_path: Path) -> None:
    ws = tmp_path / "bad_challenge_ws"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Math Task\n", encoding="utf-8")
    (ws / "instance.json").write_text("{}", encoding="utf-8")
    (ws / "metadata.json").write_text('{"instance": "https://172.31.102.101.nip.io"}', encoding="utf-8")

    res = run_validator_cmd(["preflight", "--workspace", str(ws)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert any("unisolated challenge artifact forbidden" in e for e in data["errors"])

    # Also test chall.txt specifically (verifying Codex finding fix)
    (ws / "metadata.json").unlink()
    (ws / "chall.txt").write_text("pure coefficients only", encoding="utf-8")
    res2 = run_validator_cmd(["preflight", "--workspace", str(ws)])
    assert res2.returncode != 0
    data2 = json.loads(res2.stdout)
    assert data2["ok"] is False
    assert any("unisolated challenge artifact forbidden" in e for e in data2["errors"])


def test_preflight_rejects_ip_and_attack_heuristics(tmp_path: Path) -> None:
    ws = tmp_path / "attack_ws"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Task\nUse fault injection on HSM to extract key at 172.31.102.101 nip.io\n", encoding="utf-8")
    (ws / "instance.json").write_text("{}", encoding="utf-8")

    res = run_validator_cmd(["preflight", "--workspace", str(ws)])
    assert res.returncode != 0
    data = json.loads(res.stdout)
    assert data["ok"] is False
    labels = [f.get("label") for f in data["findings"]]
    assert "fault-injection" in labels
    assert "hsm" in labels
    assert "ip-address" in labels
    assert "dynamic-dns" in labels





def test_runner_executes_in_isolated_sandbox(tmp_path: Path) -> None:
    ws = tmp_path / "ws_isolated_runner"
    ws.mkdir()
    (ws / "TASK.md").write_text("# Pure Math Task\n", encoding="utf-8")
    (ws / "instance.json").write_text(json.dumps({
        "type": "modular_linear",
        "modulus": 17,
        "A": [[1]],
        "b": [3],
    }), encoding="utf-8")

    out_file = tmp_path / "handoff.json"
    cmd = [
        sys.executable, str(RUNNER),
        "--workspace", str(ws),
        "--output", str(out_file),
        "--dry-run",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert "isolated_sandbox" in data
    # Verify that the --cd passed to codex points to the isolated sandbox, NOT the source workspace!
    cd_idx = data["command"].index("--cd")
    cd_path = data["command"][cd_idx + 1]
    assert cd_path == data["isolated_sandbox"]
    assert cd_path != str(ws)


def test_four_mirror_skills_synchronization() -> None:
    """Invariant: All 4 skill mirrors must remain synchronized for ctf-ask and ctf-toolkit."""
    skills = ["ctf-ask", "ctf-toolkit", "ctf-crypto"]
    home = Path.home()
    
    for skill in skills:
        src = REPO_ROOT / ".agents" / "skills" / skill
        assert src.is_dir(), f"Source skill directory missing: {src}"
        
        mirrors = [
            home / ".gemini" / "config" / "skills" / skill,
            REPO_ROOT / "skills" / skill,
            home / ".agents" / "skills" / skill,
        ]
        
        src_files = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        
        for mirror in mirrors:
            assert mirror.is_dir(), f"Mirror directory missing: {mirror}"
            mirror_files = {p.relative_to(mirror): p.read_bytes() for p in mirror.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
            
            # Check for missing or extra files
            assert set(src_files.keys()) == set(mirror_files.keys()), (
                f"File inventory mismatch for {skill} between {src} and {mirror}: "
                f"missing={set(src_files.keys()) - set(mirror_files.keys())}, "
                f"extra={set(mirror_files.keys()) - set(src_files.keys())}"
            )
            # Check content equivalence
            for rel_path, content in src_files.items():
                assert mirror_files[rel_path] == content, f"Content mismatch in {skill}/{rel_path} between {src} and {mirror}"


def test_zero_stale_cli_references_in_skills() -> None:
    """Invariant: Shipped skills must have zero stale 'ctf ask' CLI invocations."""
    skills_root = REPO_ROOT / ".agents" / "skills"
    forbidden = ["ctf ask --", "ctf ask "]
    
    for f in skills_root.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".py", ".yaml", ".json"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for phrase in forbidden:
                assert phrase not in text, f"Stale CLI invocation '{phrase}' found in {f}"




