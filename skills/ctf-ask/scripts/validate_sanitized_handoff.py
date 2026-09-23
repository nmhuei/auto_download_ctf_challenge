#!/usr/bin/env python3
"""Preflight sanitized formal workspaces and independently verify model candidates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ALLOWED_SUFFIXES = {".md", ".json", ".txt", ".csv", ".tsv", ".bnf"}
MAX_FILES = 64
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024

FORBIDDEN_FILENAMES = {
    "metadata.json",
    "flag.txt",
    "note.md",
    "readme.md",
    "docker-compose.yml",
    "docker-compose.yaml",
    "dockerfile",
    "solution.py",
    "solve.py",
    "exploit.py",
}

# Domain-leakage terms. This is a reject-only lint gate, not a rewriting/evasion engine.
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ctf", re.compile(r"\bctf\b", re.I)),
    ("flag", re.compile(r"\bflag\b", re.I)),
    ("exploit", re.compile(r"\bexploit(?:s|ed|ing)?\b", re.I)),
    ("vulnerability", re.compile(r"\bvulnerabilit(?:y|ies)\b|\bvuln(?:s)?\b", re.I)),
    ("hack", re.compile(r"\bhack(?:er|ers|ing|ed|s)?\b", re.I)),
    ("payload", re.compile(r"\bpayloads?\b", re.I)),
    ("shellcode", re.compile(r"\bshellcode\b", re.I)),
    ("reverse-shell", re.compile(r"\breverse\s+shell\b", re.I)),
    ("command-injection", re.compile(r"\bcommand\s+injection\b", re.I)),
    ("sql-injection", re.compile(r"\bsql\s+injection\b", re.I)),
    ("buffer-overflow", re.compile(r"\bbuffer\s+overflow\b", re.I)),
    ("stack-overflow", re.compile(r"\bstack\s+overflow\b", re.I)),
    ("use-after-free", re.compile(r"\buse[- ]after[- ]free\b", re.I)),
    ("rop", re.compile(r"\brop\b", re.I)),
    ("pwn", re.compile(r"\bpwn(?:ed|ing|able)?\b", re.I)),
    ("malware", re.compile(r"\bmalware\b", re.I)),
    ("ransomware", re.compile(r"\bransomware\b", re.I)),
    ("keylogger", re.compile(r"\bkeyloggers?\b", re.I)),
    ("cve", re.compile(r"\bcve[-_ ]?\d{4}[-_]\d+\b", re.I)),
    ("aes", re.compile(r"\baes(?:-?\d+)?\b", re.I)),
    ("rsa", re.compile(r"\brsa\b", re.I)),
    ("challenge-id", re.compile(r"\b(?:picoctf|hackthebox|tryhackme|pwnable)\b", re.I)),
    ("fault-injection", re.compile(r"\bfault\s+injection\b|\bdifferential\s+fault\b|\bdfa\b", re.I)),
    ("hsm", re.compile(r"\bhsm\b", re.I)),
    ("oracle-attack", re.compile(r"\boracle\s+(?:attack|quer(?:y|ies))\b", re.I)),
    ("ip-address", re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b")),
    ("dynamic-dns", re.compile(r"\b(?:nip\.io|ngrok|sslip\.io)\b", re.I)),
    ("recon-term", re.compile(r"\b(?:target\s+system|victim|exfiltrat(?:e|ion))\b", re.I)),
)

URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://|\bwww\.")
FLAG_TOKEN_RE = re.compile(r"(?i)\b(?:flag|ctf|picoctf|htb|thm)\s*\{[^\r\n]{1,256}\}")
SECRET_ASSIGN_RE = re.compile(r"(?i)\b(?:[a-z0-9_]+[_-])?(?:api[_-]?key|password|passwd|token|secret)\s*[:=]\s*\S+")


def emit(obj: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(obj, indent=2, sort_keys=True))
    return code


def safe_files(workspace: Path) -> tuple[list[Path], list[str]]:
    errors: list[str] = []
    if not workspace.is_dir():
        return [], [f"workspace is not a directory: {workspace}"]

    files: list[Path] = []
    total = 0
    for p in sorted(workspace.rglob("*")):
        rel = p.relative_to(workspace)
        if any(part.startswith(".") for part in rel.parts):
            errors.append(f"hidden path not allowed: {rel}")
            continue
        if p.is_symlink():
            errors.append(f"symlink not allowed: {rel}")
            continue
        if p.is_dir():
            continue
        if not p.is_file():
            errors.append(f"non-regular file not allowed: {rel}")
            continue
        stem_lower = p.stem.lower()
        name_lower = p.name.lower()
        if (
            name_lower in FORBIDDEN_FILENAMES
            or stem_lower.startswith(("chall", "flag", "solve", "exploit"))
            or name_lower.startswith("metadata")
        ):
            errors.append(f"unisolated challenge artifact forbidden in formal workspace: {rel} (formal workspace must only contain sanitized mathematical problems, no chall*/flag*/solve*/exploit* files)")
            continue
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            errors.append(f"file type not allowed: {rel}")
            continue
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            errors.append(f"file too large: {rel} ({size} bytes)")
        total += size
        files.append(p)

    if len(files) > MAX_FILES:
        errors.append(f"too many files: {len(files)} > {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        errors.append(f"workspace too large: {total} > {MAX_TOTAL_BYTES} bytes")
    return files, errors


def preflight(workspace: Path) -> tuple[bool, dict[str, Any]]:
    files, errors = safe_files(workspace)
    task = workspace / "TASK.md"
    if not task.is_file():
        errors.append("TASK.md is required")

    findings: list[dict[str, Any]] = []
    for p in files:
        rel = str(p.relative_to(workspace))
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"file is not valid UTF-8 text: {rel}")
            continue

        for label, pattern in FORBIDDEN_PATTERNS:
            m = pattern.search(text)
            if m:
                findings.append({"file": rel, "kind": "forbidden-term", "label": label, "offset": m.start()})
        for label, pattern in (("url", URL_RE), ("answer-token-format", FLAG_TOKEN_RE), ("secret-like-assignment", SECRET_ASSIGN_RE)):
            m = pattern.search(text)
            if m:
                findings.append({"file": rel, "kind": label, "offset": m.start()})

    if findings:
        errors.append(f"sanitization findings: {len(findings)}")

    result = {
        "ok": not errors,
        "workspace": str(workspace.resolve()),
        "files": [str(p.relative_to(workspace)) for p in files],
        "errors": errors,
        "findings": findings,
    }
    return not errors, result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot parse JSON {path}: {exc}") from exc


def candidate_integers(solution: dict[str, Any]) -> list[int]:
    cand = solution.get("candidate", {})
    if isinstance(cand, list):
        values = cand
    elif isinstance(cand, dict):
        values = cand.get("integers")
        if values is None and "booleans" in cand and cand["booleans"]:
            values = [int(v) for v in cand["booleans"]]
        elif values is None:
            values = []
    else:
        values = []
    if not isinstance(values, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        raise ValueError("candidate.integers must be an array of integers")
    return values


def candidate_booleans(solution: dict[str, Any]) -> list[bool]:
    cand = solution.get("candidate", {})
    if isinstance(cand, list):
        values = cand
    elif isinstance(cand, dict):
        values = cand.get("booleans")
        if (not values) and "integers" in cand and cand["integers"] and all(isinstance(v, int) and not isinstance(v, bool) and v in (0, 1) for v in cand["integers"]):
            values = [bool(v) for v in cand["integers"]]
        elif values is None:
            values = []
    else:
        values = []
    if not isinstance(values, list) or not all(isinstance(v, bool) for v in values):
        raise ValueError("candidate.booleans must be an array of booleans")
    return values


def verify_modular_linear(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    q = inst.get("modulus") or inst.get("q") or inst.get("p") or inst.get("mod")
    A = inst.get("A") or inst.get("a") or inst.get("matrix")
    b = inst.get("b") or inst.get("vector") or inst.get("rhs")
    x = candidate_integers(sol)
    if isinstance(q, bool) or not isinstance(q, int) or q <= 1:
        raise ValueError("modulus must be an integer > 1")
    if not isinstance(A, list) or not isinstance(b, list) or len(A) == 0:
        raise ValueError("instance must provide non-empty matrix A and vector b")
    if len(A) != len(b):
        raise ValueError("A row count must equal len(b)")
    n = len(A[0]) if A and isinstance(A[0], list) else 0
    if n == 0:
        raise ValueError("matrix A must have at least 1 column")
    if len(x) != n:
        raise ValueError(f"candidate length {len(x)} != expected {n}")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in b):
        raise ValueError("vector b must contain integers")
    for i, row in enumerate(A):
        if not isinstance(row, list) or len(row) != n or not all(isinstance(v, int) and not isinstance(v, bool) for v in row):
            raise ValueError("A must be rectangular integer matrix")
        lhs = sum(a * xv for a, xv in zip(row, x)) % q
        if lhs != b[i] % q:
            raise ValueError(f"equation {i} failed: {lhs} != {b[i] % q} (mod {q})")
    return [f"{len(A)}/{len(A)} modular equations satisfied"]


def verify_integer_linear(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    A = inst.get("A") or inst.get("a") or inst.get("matrix")
    b = inst.get("b") or inst.get("vector") or inst.get("rhs")
    x = candidate_integers(sol)
    if not isinstance(A, list) or not isinstance(b, list) or len(A) == 0:
        raise ValueError("instance must provide non-empty matrix A and vector b")
    if len(A) != len(b):
        raise ValueError("A row count must equal len(b)")
    n = len(A[0]) if A and isinstance(A[0], list) else 0
    if n == 0:
        raise ValueError("matrix A must have at least 1 column")
    if len(x) != n:
        raise ValueError(f"candidate length {len(x)} != expected {n}")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in b):
        raise ValueError("vector b must contain integers")
    for i, row in enumerate(A):
        if not isinstance(row, list) or len(row) != n or not all(isinstance(v, int) and not isinstance(v, bool) for v in row):
            raise ValueError("A must be rectangular integer matrix")
        lhs = sum(a * xv for a, xv in zip(row, x))
        if lhs != b[i]:
            raise ValueError(f"equation {i} failed: {lhs} != {b[i]}")
    return [f"{len(A)}/{len(A)} integer equations satisfied"]


def xor_values(inst: dict[str, Any], sol: dict[str, Any]) -> list[int]:
    cand = sol.get("candidate", {})
    if "booleans" in cand or (isinstance(cand, list) and cand and isinstance(cand[0], bool)):
        bs = candidate_booleans(sol)
        return [int(v) for v in bs]
    xs = candidate_integers(sol)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v in (0, 1) for v in xs):
        raise ValueError("xor candidate integers must be 0/1")
    return xs


def verify_xor(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    n = inst.get("num_vars")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("num_vars must be an integer > 0")
    vals = xor_values(inst, sol)
    if len(vals) != n:
        raise ValueError(f"candidate length {len(vals)} != num_vars {n}")
    rows = inst.get("rows") or inst.get("equations") or []
    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError("instance.json must provide non-empty 'rows' or 'equations' list for xor_system")
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or "vars" not in row or "rhs" not in row:
            raise ValueError(f"row {i} must be an object with 'vars' and 'rhs'")
        acc = 0
        if not isinstance(row["vars"], list):
            raise ValueError(f"row {i} 'vars' must be a list")
        for idx in row["vars"]:
            if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < n):
                raise ValueError(f"row {i} has invalid variable index {idx}")
            acc ^= vals[idx]
        rhs = row["rhs"]
        if isinstance(rhs, bool) or not isinstance(rhs, int) or rhs not in (0, 1):
            raise ValueError(f"row {i} rhs must be integer 0/1")
        if acc != rhs:
            raise ValueError(f"xor row {i} failed: {acc} != {rhs}")
    return [f"{len(rows)}/{len(rows)} XOR equations satisfied"]


def verify_cnf(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    n = inst.get("num_vars")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("num_vars must be an integer > 0")
    vals = candidate_booleans(sol)
    if len(vals) != n:
        raise ValueError(f"candidate length {len(vals)} != num_vars {n}")
    clauses = inst.get("clauses")
    if not isinstance(clauses, list) or len(clauses) == 0:
        raise ValueError("clauses must be a non-empty list")
    for ci, clause in enumerate(clauses):
        if not isinstance(clause, list) or not clause:
            raise ValueError(f"clause {ci} must be a non-empty list")
        sat = False
        for lit in clause:
            if isinstance(lit, bool) or not isinstance(lit, int) or lit == 0 or abs(lit) > n:
                raise ValueError(f"clause {ci} has invalid literal {lit}")
            val = vals[abs(lit) - 1]
            sat |= val if lit > 0 else not val
        if not sat:
            raise ValueError(f"clause {ci} is unsatisfied")
    return [f"{len(clauses)}/{len(clauses)} CNF clauses satisfied"]


def negacyclic_mul(a: list[int], x: list[int], q: int) -> list[int]:
    n = len(a)
    out = [0] * n
    for i, av in enumerate(a):
        for j, xv in enumerate(x):
            k = i + j
            if k < n:
                out[k] += av * xv
            else:
                out[k - n] -= av * xv
    return [v % q for v in out]


def verify_negacyclic(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    a = inst.get("a") or inst.get("A")
    b = inst.get("b") or inst.get("B")
    if not isinstance(a, list) or not isinstance(b, list) or len(a) == 0:
        raise ValueError("instance must provide non-empty polynomial coefficient vectors a and b")
    n = inst.get("n") or len(a)
    q = inst.get("modulus") or inst.get("q") or inst.get("p") or inst.get("mod")
    x = candidate_integers(sol)
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("n must be a positive integer")
    if isinstance(q, bool) or not isinstance(q, int) or q <= 1:
        raise ValueError("modulus must be an integer > 1")
    if not (len(a) == len(b) == len(x) == n):
        raise ValueError("a, b, and candidate must all have length n")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in a):
        raise ValueError("vector a must contain integers")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in b):
        raise ValueError("vector b must contain integers")
    got = negacyclic_mul(a, x, q)
    want = [v % q for v in b]
    if got != want:
        for i, (g, w) in enumerate(zip(got, want)):
            if g != w:
                raise ValueError(f"coefficient {i} failed: {g} != {w} (mod {q})")
    return [f"{n}/{n} negacyclic coefficients satisfied"]


def verify_fsm(inst: dict[str, Any], sol: dict[str, Any]) -> list[str]:
    cand = sol.get("candidate", {})
    if isinstance(cand, list):
        if not all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in cand):
            raise ValueError("candidate states must be a list of state identifiers")
        states = [str(s) for s in cand]
        symbols = []
    elif isinstance(cand, dict):
        raw_states = cand.get("states")
        raw_symbols = cand.get("symbols")
        if not isinstance(raw_states, list) or not isinstance(raw_symbols, list):
            raise ValueError("candidate states and symbols must be lists")
        if not all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in raw_states):
            raise ValueError("candidate states must be a list of state identifiers")
        if not all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in raw_symbols):
            raise ValueError("candidate symbols must be a list of symbol identifiers")
        states = [str(s) for s in raw_states]
        symbols = [str(s) for s in raw_symbols]
    else:
        raise ValueError("candidate must be a list or dict with 'states' and 'symbols'")
    if len(states) != len(symbols) + 1:
        raise ValueError("states length must equal symbols length + 1")
    if "start" not in inst:
        raise ValueError("instance must specify 'start' state")
    start = str(inst["start"])
    if not states or states[0] != start:
        raise ValueError(f"trace does not begin at start state: expected {start!r}, got {states[0] if states else None!r}")
    raw_transitions = inst.get("transitions")
    if not isinstance(raw_transitions, list) or len(raw_transitions) == 0:
        raise ValueError("instance must provide non-empty 'transitions' list")
    trans = set()
    for t in raw_transitions:
        if not isinstance(t, dict) or "from" not in t or "symbol" not in t or "to" not in t:
            raise ValueError("each transition must be an object with 'from', 'symbol', and 'to'")
        trans.add((str(t["from"]), str(t["symbol"]), str(t["to"])))
    for i, sym in enumerate(symbols):
        edge = (states[i], sym, states[i + 1])
        if edge not in trans:
            raise ValueError(f"missing transition at step {i}: {edge}")
    accepting_raw = inst.get("accepting", [])
    if not isinstance(accepting_raw, list):
        raise ValueError("'accepting' must be a list of states")
    accepting = {str(s) for s in accepting_raw}
    if states[-1] not in accepting:
        raise ValueError(f"terminal state {states[-1]!r} is not accepting")
    return [f"{len(symbols)} transitions valid", "terminal state is accepting"]


VERIFIERS = {
    "modular_linear": verify_modular_linear,
    "integer_linear": verify_integer_linear,
    "xor_system": verify_xor,
    "boolean_cnf": verify_cnf,
    "negacyclic_product": verify_negacyclic,
    "finite_state_trace": verify_fsm,
}


def verify(workspace: Path, solution_path: Path) -> tuple[bool, dict[str, Any]]:
    ok, pf = preflight(workspace)
    if not ok:
        return False, {"ok": False, "stage": "preflight", "errors": pf["errors"], "findings": pf["findings"]}
    instance_path = workspace / "instance.json"
    if not instance_path.is_file():
        return False, {"ok": False, "stage": "verify", "errors": ["instance.json is required for deterministic verification"]}
    try:
        inst = load_json(instance_path)
        sol = load_json(solution_path)
        if sol.get("status") != "solved":
            raise ValueError("solution status is not 'solved'")
        typ = inst.get("type")
        if typ not in VERIFIERS:
            raise ValueError(f"unsupported verifier type: {typ!r}")
        if sol.get("problem_type") != typ:
            raise ValueError(f"solution problem_type {sol.get('problem_type')!r} != instance type {typ!r}")
        checks = VERIFIERS[typ](inst, sol)
        return True, {"ok": True, "stage": "verify", "type": typ, "checks": checks, "errors": []}
    except Exception as exc:  # noqa: BLE001
        return False, {"ok": False, "stage": "verify", "type": None, "checks": [], "errors": [str(exc)]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="lint a sanitized formal workspace")
    p_pre.add_argument("--workspace", required=True, type=Path)

    p_ver = sub.add_parser("verify", help="independently verify a structured candidate against instance.json")
    p_ver.add_argument("--workspace", required=True, type=Path)
    p_ver.add_argument("--solution", required=True, type=Path)

    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()

    if args.command == "preflight":
        ok, result = preflight(workspace)
        return emit(result, 0 if ok else 2)

    ok, result = verify(workspace, args.solution.expanduser().resolve())
    return emit(result, 0 if ok else 3)


if __name__ == "__main__":
    raise SystemExit(main())
