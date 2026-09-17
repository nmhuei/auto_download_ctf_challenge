# BQA CLI Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tự chuyển lỗi CLI sang phiên BQA bền vững, kiểm tra bản vá và retry lệnh đúng một lần.

**Architecture:** `ctf_downloader/bqa_recovery.py` owns incident redaction, session persistence, `agy` process lifecycle, verification and retry. `ctf_downloader/cli.py` only supplies the original argv and maps command termination to the recovery boundary, preserving existing command handlers.

**Tech Stack:** Python 3.8+, standard library (`subprocess`, `json`, `threading`, `traceback`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-bqa-cli-recovery-design.md`

## Global Constraints

- BQA is display text only; executable remains `agy`.
- Never persist or prompt cookie/token/password/flag values.
- Retry has `CTF_BQA_RETRY=1` and may never trigger recovery again.
- BQA only applies to case 1 CLI recovery; never solve CTF challenges.
- Existing dirty source changes must remain untouched except files in this feature.

---

### Task 1: Sanitized incident and cookie-shape model

**Files:**
- Create: `ctf_downloader/bqa_recovery.py`
- Test: `tests/test_bqa_recovery.py`

**Interfaces:**
- Produces `CookieShape.from_input(value: Optional[str]) -> CookieShape`.
- Produces `redact_argv(argv: Sequence[str]) -> list[str]`.
- Produces `RecoveryIncident.from_failure(argv, exit_code, exc=None) -> RecoveryIncident` and `to_prompt() -> str`.

- [x] **Step 1: Write failing tests** for header, JSON and raw-token cookie shapes, plus redaction of `--cookie`, `--token`, `--password`, `--flag` and their short options. Assert literal secret values never occur in serialized prompt.
- [x] **Step 2: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm failure because `ctf_downloader.bqa_recovery` does not exist.
- [x] **Step 3: Implement** frozen dataclasses and stdlib-only sanitizers in `ctf_downloader/bqa_recovery.py`. Use field names and counts only; include exception class/traceback without argument values.
- [x] **Step 4: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm the incident tests pass.

### Task 2: Durable BQA session and background command runner

**Files:**
- Modify: `ctf_downloader/bqa_recovery.py`
- Test: `tests/test_bqa_recovery.py`

**Interfaces:**
- Produces `BqaSessionStore(path).load(root) -> Optional[BqaSession]` and `save(session) -> None`.
- Produces `BqaRecovery(source_root, session_store, popen_factory).repair(incident) -> BqaRepairResult`.
- `BqaRepairResult` contains `changed_test_paths`, `conversation_id`, `returncode`, and `reason`.

- [x] **Step 1: Write failing tests** with a fake `Popen` JSON result: first recovery builds bootstrap command; second recovery includes `--conversation`; stale revision uses bootstrap; prompt does not contain secret literals; TTY status writer receives BQA progress labels.
- [x] **Step 2: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm failures identify the missing runner/session behavior.
- [x] **Step 3: Implement** atomic JSON storage under the existing global config directory, source-root/revision detection, `agy --mode accept-edits --output-format json --print` commands, JSON conversation parsing, test-diff discovery, and nonblocking polling/status output. If `agy` is missing or outputs invalid JSON, return a failed result without retrying.
- [x] **Step 4: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm all runner tests pass.

### Task 3: Verification and one-shot retry

**Files:**
- Modify: `ctf_downloader/bqa_recovery.py`
- Test: `tests/test_bqa_recovery.py`

**Interfaces:**
- Produces `verify_bqa_changes(source_root, changed_test_paths, run) -> bool`.
- Produces `retry_command(argv, source_root, run) -> int`.

- [x] **Step 1: Write failing tests** proving verification runs `compileall`, `pytest --collect-only`, and every changed test path before retry; a failed verification prevents retry; retry invokes `python -m ctf_downloader.cli` with `CTF_BQA_RETRY=1`.
- [x] **Step 2: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm the missing verification/retry behavior fails.
- [x] **Step 3: Implement** deterministic subprocess execution with no shell, changed-test path validation under `tests/`, and a retry environment copied from `os.environ` with the sentinel set.
- [x] **Step 4: Run** `python -m pytest tests/test_bqa_recovery.py -q` and confirm verification/retry tests pass.

### Task 4: CLI recovery boundary

**Files:**
- Modify: `ctf_downloader/cli.py`
- Test: `tests/test_bqa_cli_integration.py`

**Interfaces:**
- Produces `run_with_bqa_recovery(argv, dispatch, recovery) -> int`.
- `main()` invokes the boundary around existing dispatch while preserving `SystemExit` codes.

- [x] **Step 1: Write failing tests** for nonzero command exit invoking recovery once, successful command skipping recovery, sentinel retry skipping recovery, and help/version/Ctrl-C skipping recovery.
- [x] **Step 2: Run** `python -m pytest tests/test_bqa_cli_integration.py -q` and confirm failure because the recovery boundary does not exist.
- [x] **Step 3: Add** a decorator-based boundary around `main()` so its existing dispatch remains intact. Preserve all parser and command handler behavior; use recovery result’s retry exit code when repair succeeds.
- [x] **Step 4: Run** `python -m pytest tests/test_bqa_cli_integration.py tests/test_bqa_recovery.py -q` and confirm all feature tests pass.

### Task 5: Regression verification and documentation

**Files:**
- Modify: `README.md`
- Test: `tests/test_bqa_recovery.py`, `tests/test_bqa_cli_integration.py`

- [x] **Step 1: Document** BQA as automatic CLI recovery, its one-shot retry behavior, configuration/session location, and explicit non-goal of challenge solving.
- [x] **Step 2: Run** `python -m compileall -q ctf_downloader`.
- [x] **Step 3: Run** `python -m pytest tests/test_bqa_recovery.py tests/test_bqa_cli_integration.py -q`.
- [x] **Step 4: Run** `python -m pytest --collect-only -q`.
- [x] **Step 5: Run** `git diff --check` and inspect `git diff -- ctf_downloader/bqa_recovery.py ctf_downloader/cli.py README.md tests/test_bqa_recovery.py tests/test_bqa_cli_integration.py`.
