# GPT CTF Continuation Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run at most three GPT CLI CTF workers at once, retain each worker session, and resume a terminal-but-unverified challenge with `continue` until it reaches a verified candidate or its bounded continuation budget is exhausted.

**Architecture:** The GPT CLI exposes a structured, streaming lifecycle channel with the local session identifier before the first model event. The CTF scheduler owns the challenge-level loop: it records every turn outcome, verifies candidate evidence independently, and starts the next turn only after the previous turn is proven terminal. The first turn keeps the compact command `gpt -b br -p "solve <absolute-path>"`; automation adds an event-output switch solely for machine-readable supervision.

**Tech Stack:** Python 3.14, argparse, asyncio/curl-cffi runtime, subprocess worker pool, JSONL telemetry, pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-ctf-solver-worker-pool-design.md`

## Global Constraints

- The global GPT concurrency cap is exactly 3; no per-category mode may raise it.
- The initial model prompt is `solve <absolute-challenge-path>`; every automatic follow-up prompt is `continue`.
- A `model_terminal` event proves only that a turn ended. It never proves a challenge was solved.
- A format-matching string is only a candidate. Mark it verified only when a scheduler-run local verifier reproduces the same value from `solver/solve.py` and challenge inputs.
- Persist session ID, lifecycle evidence, candidate provenance, and continuation count per challenge in `script/worker-state.json`.
- Before a continuation, reconcile a prior interrupted turn. A pending upstream tool or unknown lifecycle is not eligible for a new turn.
- Do not call `ctf submit`, `ctf submit --auto`, or `ctf hoard --all`. A manually observed platform result can be recorded, but no code in this feature submits flags.
- Retain rejected candidates and their evidence. Do not overwrite a previous candidate record.
- Keep `challenge/` read-only. Runtime evidence belongs in `script/`; reusable solvers belong in `solver/`.

---

### Task 1: Add a structured streaming lifecycle protocol to `gpt`

**Files:**
- Modify: `/home/light/GitHub/gpt_cli/gpt_cli/cli/main.py`
- Modify: `/home/light/GitHub/gpt_cli/gpt_cli/cli/turn_runner.py`
- Modify: `/home/light/GitHub/gpt_cli/gpt_cli/session/logger.py`
- Test: `/home/light/GitHub/gpt_cli/tests/test_cli_and_gateway.py`
- Test: `/home/light/GitHub/gpt_cli/tests/test_turn_runner.py`

**Interfaces:**
- Consumes: `TelemetryEvent`, `TurnResult`, `SessionRecord` and the existing checkpoint updates.
- Produces: `--events-json`, one JSON object per line, with event types `turn_started`, `activity`, `tool_started`, `tool_finished`, and `turn_finished`.
- `turn_started` contains `session_id` before Wire transport begins.
- `turn_finished` contains `session_id`, `lifecycle_status`, `conversation_id`, `message_id`, `duration_ms`, and `error`.

- [ ] **Step 1: Write failing CLI tests for session identity and terminal evidence**

```python
def test_events_json_emits_session_before_terminal_result(monkeypatch, tmp_path, capsys):
    async def fake_send_turn(self, *args, **kwargs):
        return TurnResult(text="stopped", conversation_id="conv_1", message_id="msg_1",
                          model="gpt-5-6-thinking", status="terminal",
                          lifecycle_status="model_terminal")
    monkeypatch.setattr("gpt_cli.cli.main.WireTransport.send_turn", fake_send_turn)
    args = build_cli_parser().parse_args(["run", "--events-json", "-p", "solve /tmp/challenge"])
    asyncio.run(handle_run(args))
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["turn_started", "turn_finished"]
    assert events[0]["session_id"] == events[1]["session_id"]
    assert events[1]["lifecycle_status"] == "model_terminal"

def test_events_json_emits_tool_started_and_tool_finished(monkeypatch, tmp_path, capsys):
    emitted = []
    runner = make_runner(tmp_path, emit_event=lambda event: emitted.append(event))
    runner.emit_event(TelemetryEvent("tool_call_started", {"id": "tool_1", "tool": "host_read_file"}))
    runner.emit_event(TelemetryEvent("tool_call_finished", {"id": "tool_1", "tool": "host_read_file"}))
    assert [(event.event_type, event.data["id"]) for event in emitted] == [
        ("tool_call_started", "tool_1"), ("tool_call_finished", "tool_1")
    ]
```

- [ ] **Step 2: Run the two tests and confirm they fail because `--events-json` is absent**

Run:

```bash
cd /home/light/GitHub/gpt_cli
.venv/bin/pytest tests/test_cli_and_gateway.py -k events_json -v
```

Expected: parser rejects `--events-json` or the expected JSONL records are missing.

- [ ] **Step 3: Add `--events-json` and emit lifecycle JSONL from the existing callbacks**

Implement an emitter local to `handle_run` that writes only JSONL to stdout when this option is set. It must emit `turn_started` immediately after `SessionRegistry` selects or creates the session. Forward every received telemetry event as `activity`, `tool_started`, or `tool_finished`; preserve the existing normal terminal presentation when the option is not set. Emit `turn_finished` in `finally` only after `TurnResult` exists. For exceptions, emit `turn_finished` with `lifecycle_status: "upstream_error"` and the exception text.

Do not use the existing 20-second display heartbeat as a structured activity event. It is a local UI timer, not proof of upstream progress.

- [ ] **Step 4: Run focused tests and the CLI test suite**

Run:

```bash
cd /home/light/GitHub/gpt_cli
.venv/bin/pytest tests/test_cli_and_gateway.py tests/test_turn_runner.py -q
.venv/bin/pytest tests/ -q
```

Expected: all tests pass; non-JSON CLI behavior remains unchanged.

- [ ] **Step 5: Commit the isolated runtime change**

```bash
cd /home/light/GitHub/gpt_cli
git add gpt_cli/cli/main.py gpt_cli/cli/turn_runner.py gpt_cli/session/logger.py tests/test_cli_and_gateway.py tests/test_turn_runner.py
git commit -m "feat(cli): stream structured turn lifecycle events"
```

### Task 2: Make the GPT adapter preserve session and lifecycle state

**Files:**
- Modify: `ctf_downloader/solver/base.py`
- Modify: `ctf_downloader/solver/adapters/gpt.py`
- Test: `tests/test_solver_adapter_system.py`

**Interfaces:**
- Consumes: `--events-json` records from Task 1.
- Produces: `NormalizedSolverEvent` event types `session_started`, `turn_finished`, `tool_started`, and `tool_finished`.
- `GptSolverAdapter.capabilities().supports_session_resume` becomes `True`.
- `build_invocation(job, prompt, session_id=None)` returns the initial compact solve command with `--events-json`; with a session ID it returns `gpt run --resume-session <id> -b br --events-json -p continue`.

- [ ] **Step 1: Write failing adapter tests**

```python
def test_gpt_initial_invocation_uses_compact_solve_prompt_and_event_stream(tmp_path):
    spec = GptSolverAdapter(binary_override="/usr/bin/gpt").build_invocation(job(tmp_path))
    assert spec.argv == ["/usr/bin/gpt", "-b", "br", "-p", f"solve {tmp_path}", "--events-json"]

def test_gpt_continuation_resumes_same_session_with_continue(tmp_path):
    spec = GptSolverAdapter(binary_override="/usr/bin/gpt").build_invocation(job(tmp_path), session_id="sess_123")
    assert spec.argv == ["/usr/bin/gpt", "run", "--resume-session", "sess_123", "-b", "br", "-p", "continue", "--events-json"]

def test_gpt_event_decoder_reports_terminal_and_session_id():
    events = GptSolverAdapter().decode_stream_line('{"event":"turn_finished","session_id":"sess_123","lifecycle_status":"model_terminal"}')
    assert [(event.event_type, event.message) for event in events] == [("turn_finished", "model_terminal")]
```

- [ ] **Step 2: Run the adapter tests and confirm they fail**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_adapter_system.py -k gpt -v
```

Expected: resume capability, argv, and structured events do not match.

- [ ] **Step 3: Extend the adapter contract without parsing terminal text heuristically**

Add structured event decoding for the JSONL protocol. Store session ID in a `session_started` event, lifecycle status in a `turn_finished` event, and matched tool IDs in their own events. Do not infer `completed` from exit code `0`; return `terminal` only when the terminal event explicitly says `model_terminal`.

- [ ] **Step 4: Run all adapter tests**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_adapter_system.py -q
```

Expected: all registered adapters retain their existing tests; GPT exposes correct resume behavior.

- [ ] **Step 5: Commit the adapter change**

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
git add ctf_downloader/solver/base.py ctf_downloader/solver/adapters/gpt.py tests/test_solver_adapter_system.py
git commit -m "feat(solver): preserve GPT turn lifecycle and sessions"
```

### Task 3: Add durable candidate evidence and an independent local verifier

**Files:**
- Modify: `ctf_downloader/services/solver_service.py`
- Create: `ctf_downloader/services/local_verifier.py`
- Test: `tests/test_solver_service.py`
- Create: `tests/test_local_verifier.py`

**Interfaces:**
- Consumes: a `SolverJob`, the candidate found in a terminal turn, and the challenge `solver/solve.py`.
- Produces: `VerificationResult(status, candidate, reproduced_flag, command, exit_code, stdout_sha256, reason)`.
- Persists `candidate_records`, each containing `value`, `source_turn`, `source_session_id`, `source_log_offset`, `state`, and optional verification result.

- [ ] **Step 1: Write failing verification tests**

```python
def test_local_verifier_accepts_only_reproduced_candidate(tmp_path):
    # solve.py prints CSSCTF{reproduced}; candidate matches it.
    assert verify_candidate(tmp_path, "CSSCTF{reproduced}").status == "passed"

def test_local_verifier_rejects_model_report_without_reproduction(tmp_path):
    # worker-report claims passed, but solve.py prints a different value.
    assert verify_candidate(tmp_path, "CSSCTF{claimed}").status == "failed"

def test_second_candidate_does_not_inherit_first_candidate_verification(tmp_path):
    # Candidate A is reproducible; candidate B is not.
    assert verify_candidate(tmp_path, "CSSCTF{B}").status == "failed"
```

- [ ] **Step 2: Run verifier tests and confirm they fail because no independent verifier exists**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_local_verifier.py -v
```

Expected: import error for `local_verifier`.

- [ ] **Step 3: Implement `local_verifier.py` with a bounded subprocess**

Run only `solver/solve.py` from the challenge directory, with a fixed timeout and captured output. Extract one non-placeholder flag from output. Verification passes only if this exact value equals the candidate. Hash captured stdout and record the command/exit code for provenance. A model-written `worker-report.json` may be preserved as context but never grants verification on its own.

- [ ] **Step 4: Change `_finish_worker` to append candidate evidence instead of hoarding immediately**

Replace the current `local_verification == "passed"` trust decision with the verifier result. A passing result creates `candidate_ready`; every other candidate remains `candidate_unverified`. Do not mutate platform-solve state and do not invoke a submit command.

- [ ] **Step 5: Run service and verifier tests**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_local_verifier.py tests/test_solver_service.py -q
```

Expected: a report alone cannot verify a candidate; candidate provenance persists across runs.

- [ ] **Step 6: Commit the verification change**

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
git add ctf_downloader/services/solver_service.py ctf_downloader/services/local_verifier.py tests/test_solver_service.py tests/test_local_verifier.py
git commit -m "feat(solver): verify CTF candidates independently"
```

### Task 4: Implement the bounded continuation state machine with a global cap of three

**Files:**
- Modify: `ctf_downloader/services/solver_service.py`
- Modify: `ctf_downloader/cli.py`
- Test: `tests/test_solver_service.py`
- Test: `tests/test_solver_cli.py`

**Interfaces:**
- Consumes: `turn_finished` lifecycle events, session ID, pending tool IDs, candidate verification result, and prior `worker-state.json`.
- Produces: states `running`, `waiting_tool`, `needs_reconcile`, `needs_continuation`, `candidate_unverified`, `candidate_ready`, `continuation_exhausted`, and `failed`.
- Scheduler option: `--workers` accepts 1, 2, or 3 and defaults to 3; service rejects every value above 3.
- Per-job continuation budget: default 3 terminal turns without a new verified candidate or new durable artifact.

- [ ] **Step 1: Write failing scheduler tests**

```python
def test_terminal_without_candidate_resumes_same_gpt_session(tmp_path, monkeypatch):
    # First fake GPT turn emits session_started(sess_1) + turn_finished(model_terminal).
    # Second invocation must use --resume-session sess_1 and prompt continue.
    assert invocation_prompts == [f"solve {job.path}", "continue"]

def test_pending_tool_prevents_continuation_after_process_exit(tmp_path, monkeypatch):
    # A tool_started event without matching tool_finished becomes needs_reconcile.
    assert state["state"] == "needs_reconcile"
    assert invocation_count == 1

def test_verified_candidate_stops_continuation(tmp_path, monkeypatch):
    # Local verifier returns passed for the candidate.
    assert invocation_count == 1
    assert state["state"] == "candidate_ready"

def test_scheduler_never_starts_more_than_three_gpt_workers(tmp_path, monkeypatch):
    # Queue five GPT jobs with controlled fake processes.
    assert max_observed_active == 3

def test_workers_argument_above_three_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 3"):
        SolverService(tmp_path).run("1", workers=4, engine="gpt")
```

- [ ] **Step 2: Run the scheduler tests and confirm they fail**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_service.py tests/test_solver_cli.py -k 'continuation or three_gpt or workers_argument' -v
```

Expected: current scheduler treats exit `0` as a terminal worker result and permits up to five workers.

- [ ] **Step 3: Implement lifecycle reconciliation before continuation**

Persist `pending_tool_ids` from structured events. When the process exits:

1. If lifecycle is `server_active`, `transport_lost`, or a tool remains pending, store `needs_reconcile` and do not create another turn.
2. If lifecycle is `model_terminal` and independent verification is passing, store `candidate_ready` and stop the job.
3. If lifecycle is `model_terminal` without a verified candidate, increment `continuation_attempts`, store `needs_continuation`, and enqueue the same job behind active workers.
4. If the continuation budget is reached without a newly produced solver, finding, candidate, or verification result, store `continuation_exhausted` and preserve all evidence.

The follow-up invocation must resume the persisted GPT session and use exactly `continue` as its prompt.

- [ ] **Step 4: Enforce the cap of three in every scheduler entry point**

Change CLI validation and `SolverService.run()` validation from 5 to 3. Remove the `per_category` exception for GPT workers. Keep each active job under the existing process-group ownership so a scheduled continuation does not overlap its previous turn.

- [ ] **Step 5: Run the scheduler test suites**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_service.py tests/test_solver_cli.py tests/test_solver_adapter_system.py -q
```

Expected: continuations preserve session, uncertain turns do not replay, and measured active GPT workers never exceed 3.

- [ ] **Step 6: Commit the scheduler change**

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
git add ctf_downloader/services/solver_service.py ctf_downloader/cli.py tests/test_solver_service.py tests/test_solver_cli.py
git commit -m "feat(solver): continue terminal GPT turns with three-worker cap"
```

### Task 5: Record manual platform feedback without submitting from automation

**Files:**
- Modify: `ctf_downloader/cli.py`
- Modify: `ctf_downloader/services/solver_service.py`
- Test: `tests/test_solver_cli.py`
- Test: `tests/test_solver_service.py`

**Interfaces:**
- New command: `ctf solve-feedback -w <workspace> --name <challenge-name> --candidate <flag> --result accepted|incorrect|unknown`.
- Consumes: a human-observed platform result for one exact candidate.
- Produces: a durable candidate-record state change; `incorrect` schedules `needs_continuation` only if its continuation budget remains.

- [ ] **Step 1: Write failing feedback tests**

```python
def test_incorrect_feedback_rejects_only_the_named_candidate(tmp_path):
    # Candidate A and B are recorded for the same challenge.
    record_feedback(job, "CSSCTF{A}", "incorrect")
    assert state["candidate_records"][0]["state"] == "rejected"
    assert state["candidate_records"][1]["state"] != "rejected"

def test_unknown_feedback_does_not_resume_or_reject(tmp_path):
    record_feedback(job, "CSSCTF{A}", "unknown")
    assert state["state"] == "candidate_ready"

def test_cli_feedback_never_invokes_submission(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    result = invoke_cli(["solve-feedback", "-w", str(tmp_path), "--name", "Demo",
                         "--candidate", "CSSCTF{A}", "--result", "incorrect"])
    assert result.exit_code == 0
    assert all(argv[:2] != ["ctf", "submit"] for argv in calls)
```

- [ ] **Step 2: Run feedback tests and confirm they fail**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_service.py tests/test_solver_cli.py -k feedback -v
```

Expected: feedback command and candidate-level state transition do not exist.

- [ ] **Step 3: Implement the feedback ledger and CLI command**

Find a challenge by name, require an existing candidate record with the exact submitted value, append timestamped feedback, and update only that record. `accepted` makes the challenge `solved_local`; `incorrect` records rejection and makes the challenge eligible for the bounded `continue` path; `unknown` leaves it unchanged. The command only records supplied feedback and never performs network submission.

- [ ] **Step 4: Run feedback and full local solver tests**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_service.py tests/test_solver_cli.py tests/test_solver_adapter_system.py tests/test_local_verifier.py -q
```

Expected: every feedback state is tied to a single candidate and no submit path is reachable.

- [ ] **Step 5: Commit the feedback change**

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
git add ctf_downloader/cli.py ctf_downloader/services/solver_service.py tests/test_solver_cli.py tests/test_solver_service.py
git commit -m "feat(solver): record manual candidate feedback"
```

### Task 6: Migrate the CSS CTF workers only after offline verification

**Files:**
- Create: `scripts/systemd/ctf-css-gpt-scheduler.service`
- Test: manual dry run against a temporary workspace under `/home/light/Downloads/test_gpt_scheduler/`.

**Interfaces:**
- Consumes: the Task 4 scheduler with engine `gpt`.
- Produces: at most three active GPT processes, one challenge session per worker, and durable worker state.

- [ ] **Step 1: Prepare a temporary mock workspace with four tiny challenge directories**

Create the fixture only under `/home/light/Downloads/test_gpt_scheduler/`; use a fake `gpt` binary that emits the Task 1 JSONL protocol. Configure two jobs as terminal-without-candidate, one as terminal-with-verified-candidate, and one as waiting-tool.

- [ ] **Step 2: Run a dry scheduler test and measure process concurrency**

Run:

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
pytest tests/test_solver_service.py -k 'three_gpt or continuation or pending_tool' -q
```

Expected: no more than three fake GPT processes coexist; only the two eligible terminal jobs receive `continue`; the waiting-tool job is reconciled rather than replayed.

- [ ] **Step 3: Create and install the scheduler-owned systemd unit**

Create `scripts/systemd/ctf-css-gpt-scheduler.service` with `ExecStart` set to the `ctf solve` scheduler invocation for the CSS workspace, `--engine gpt`, and `--workers 3`. After the dry run passes, copy this unit into `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, stop the prior direct one-shot units, then start this scheduler-owned service. The scheduler must read persisted worker state instead of treating pre-existing direct sessions as new work.

- [ ] **Step 4: Observe one complete lifecycle without submitting a flag**

Check `worker-state.json`, `gpt.log`, `events.jsonl`, and the process list. Confirm: an initial solve creates a session record; terminal-but-unverified jobs receive one session resume; verified candidates stop; unknown/pending tool jobs do not replay.

- [ ] **Step 5: Commit only launcher/template changes, if any**

```bash
cd /home/light/Workspace/Project/auto_download_ctf_challenge
git add scripts/systemd/ctf-css-gpt-scheduler.service
git commit -m "ops(ctf): run GPT scheduler with three workers"
```

## Plan review

- Coverage: Tasks 1–2 establish reliable session/lifecycle evidence; Task 3 establishes independent candidate verification; Task 4 implements bounded continuation and the cap of 3; Task 5 stores manual chấm feedback; Task 6 migrates workers only after test evidence.
- Safety: no task invokes CTF submission, no candidate becomes solved merely from regex or model output, and uncertain tool state blocks replay.
- Type consistency: Task 1 emits `session_started`/`turn_finished`; Task 2 maps them to adapter events; Task 4 consumes the same session and lifecycle fields; Task 5 updates candidate records produced by Task 3.
