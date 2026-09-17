# CTF Solver Worker Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a three-worker local Agy scheduler with interactive challenge selection and durable solver progress.

**Architecture:** A dedicated `SolverService` scans downloaded challenge directories and owns all scheduler writes. CLI handlers use it to select jobs, launch isolated Agy process groups, and render Rich status; each challenge stores operational artifacts in `script/`.

**Tech Stack:** Python 3.10+, argparse, subprocess, Rich, existing atomic file I/O helpers.

**Spec:** `docs/superpowers/specs/2026-09-16-ctf-solver-worker-pool-design.md`

## Global Constraints

- Worker limit is exactly 1 through 3, default 3.
- Never submit flags or invoke a Codex collaboration subagent.
- Agy is the default worker executable; its process is scoped to one challenge directory.
- Challenge artifacts are written under `script/`; `solver/solve.py` is the final solver location.
- No remote connection is initiated by scheduler code.

---

### Task 1: Solver state and workspace scanner

**Files:**
- Create: `ctf_downloader/services/solver_service.py`
- Test: `tests/test_solver_service.py`

**Interfaces:**
- Produces: `SolverService.scan() -> list[SolverJob]`, `SolverService.select_ids(str) -> list[SolverJob]`, `SolverService.read_job(SolverJob) -> dict`.

- [ ] **Step 1: Write the failing tests** for source/instance classification and comma-separated ID validation using a temporary workspace.
- [ ] **Step 2: Run** `pytest tests/test_solver_service.py -q` and confirm missing-module failure.
- [ ] **Step 3: Implement** dataclasses, metadata scan, numbering, per-challenge state paths and atomic state reads/writes.
- [ ] **Step 4: Run** `pytest tests/test_solver_service.py -q` and confirm passing classification/selection tests.

### Task 2: Process pool and progress capture

**Files:**
- Modify: `ctf_downloader/services/solver_service.py`
- Test: `tests/test_solver_service.py`

**Interfaces:**
- Produces: `SolverService.run(ids, workers=3, agy_command=None) -> list[dict]` and `SolverService.recover_stale_jobs() -> int`.

- [ ] **Step 1: Write failing tests** with a fake executable that emits progress and creates `solver/solve.py`; assert at most three simultaneous jobs and persisted log/state.
- [ ] **Step 2: Run** the focused tests and confirm the absent scheduler behavior fails.
- [ ] **Step 3: Implement** manager locking, worker launch, line capture, progress parsing, polling, terminal state validation and stale-PID recovery.
- [ ] **Step 4: Run** focused tests and confirm success.

### Task 3: CLI and live dashboard

**Files:**
- Modify: `ctf_downloader/cli.py`
- Modify: `ctf_downloader/cli_commands.py`
- Modify: `ctf_downloader/services/status_service.py`
- Test: `tests/test_solver_cli.py`

**Interfaces:**
- Adds: `ctf solve [--ids IDS] [--workers 1..3]`, `ctf status --solver [--watch]`, `ctf status <id> --solver`.

- [ ] **Step 1: Write failing parser and rendering tests** using a temporary workspace.
- [ ] **Step 2: Run** focused tests and confirm parser/handler absence fails.
- [ ] **Step 3: Implement** parser dispatch, interactive ID input, Rich table renderer, detail view and watch refresh.
- [ ] **Step 4: Run** focused CLI tests and confirm success.

### Task 4: Prompt and regression verification

**Files:**
- Modify: `ctf_downloader/services/solver_service.py`
- Test: `tests/test_solver_service.py`

- [ ] **Step 1: Write failing test** that reads the generated prompt through the public builder and asserts all operational requirements are present as behavior-driving input.
- [ ] **Step 2: Run** the focused test and confirm it fails.
- [ ] **Step 3: Implement** the Agy prompt with local-first, artifact placement, final solver interface and no-submit constraints.
- [ ] **Step 4: Run** `pytest tests/test_solver_service.py tests/test_solver_cli.py -q` and targeted existing CLI tests.
