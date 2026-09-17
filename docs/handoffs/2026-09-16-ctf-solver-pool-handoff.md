# Handoff — CTF solver pool

## User's current request

Continue the CTF solver manager implementation, then run a real parallel demo: choose easy challenges from different available categories, one challenge per category, with up to three Agy CLI workers at once. The latest prompt requirement is strict: the Agy prompt must contain only `/ctf-toolkit`, the CTF name, the challenge name, and its absolute directory. Do not repeat operational rules in the prompt because they belong in `challenge/NOTE.md`.

The user explicitly prohibits Codex collaboration subagents. Do not spawn them. Agy processes launched by the local `ctf solve` worker manager are desired and distinct from collaboration subagents.

## Absolute constraints

- Never automatically submit flags. Do not run `ctf submit`, `ctf submit --auto`, or `ctf hoard --all`.
- UI name is BQA; `agy` is only the underlying executable.
- Solver workers use Agy by default with `--mode accept-edits --dangerously-skip-permissions`.
- For difficult crypto, the eventual expert is **Codex CLI** Astra high, never `agy --model gpt-6-astra`.
- All temporary scripts, probes, parsers, payloads, local harnesses, debug artifacts, logs and worker state for a challenge belong in `<challenge>/script/`.
- `solver/solve.py` is only the final reusable solver. It must expose optional `--url URL` and `--remote HOST:PORT`, and no remote connection may occur unless one is explicitly supplied.
- A service challenge must be solved and verified locally before its instance. When source is absent, do not invent a local server.
- Do not put a ban on reading writeups/solvers in NOTE or the general skill; the user explicitly removed that rule.

## Changes made in this session

### New scheduler service

Created `ctf_downloader/services/solver_service.py`.

- `SolverService.scan()` scans `metadata.json`, assigns stable display IDs sorted by category/name, and classifies source/instance.
- `select_ids("1,2,3")` validates empty, duplicate and unknown IDs.
- `run(..., workers=1..3)` uses one workspace manager lock and starts at most three Agy process groups. Each child has `cwd=<challenge>` and logs stdout/stderr to `script/agy.log`.
- State lives at `script/worker-state.json`; Agy progress marker lines `@@CTF_PROGRESS@@ {"phase":"...","message":"..."}` go to `script/progress.json`.
- States: `queued`, `running`, `completed`, `failed`, `filtered`, `cancelled`.
- The manager recovers an orphaned `running` PID as `E_WORKER_CRASH`.
- It hard-limits each worker by `--timeout` (default 3600 seconds).
- Completion is no longer based merely on `solver/solve.py` existing: downloader creates an initial template. A worker must change the solver and create `script/worker-report.json` containing `{"local_verification":"passed", "summary":"..."}`; otherwise it becomes `E_VERIFY_LOCAL`.
- Added a filter detector for common refusals including the real observed phrase `This request was blocked by Gemini's filters.`. The new result is `filtered` / `E_FILTER` before local-verification checks.

### CLI

Modified `ctf_downloader/cli.py` and `ctf_downloader/cli_commands.py`.

- New `ctf solve [--ids 1,2,3] [--workers 1|2|3] [--timeout seconds]`.
- No `--ids` renders a Rich table and asks `Nhập challenge IDs (ví dụ: 1,2,3)`.
- `ctf status --solver` renders worker state; `ctf status <display-id> --solver` shows detail and log path; `ctf status --solver --watch` refreshes the table.
- The active table currently uses `◌ running`, but it is static. The user requested an actual loading icon; replace it with a time-varying spinner frame in `_solver_table()` or a Rich `Spinner` while preserving non-TTY output.

### Workspace rules

Modified `ctf_downloader/generator/workspace_builder.py`.

- `challenge/NOTE.md` now has a managed `<!-- CTF-SOLVER-RULES:START/END -->` block.
- Pull/update merges the block while preserving every user-written note outside it.
- Rules cover metadata-first, source/local-harness policy, all artifacts in `script/`, final `solver/solve.py`, local-first verification, and the worker report contract.
- Generated templates now expose `--url` and `--remote` adapters. The Pwn template defaults to `process('./vuln')`, avoiding an implicit remote connection.

### Skills

Rewrote global `/home/light/.agents/skills/ctf/SKILL.md` to remove the old gpt/subagent/recruiter/wargame flow. It now describes the downloaded workspace local-first flow and the solver commands. It explicitly forbids automated submit and collaboration subagents.

Updated global `/home/light/.agents/skills/ctf-toolkit/SKILL.md` so a short `/ctf-toolkit` prompt directs Agy to read `metadata.json` and `challenge/NOTE.md`, and to keep artifacts in `script/`.

Both global skills were validated with:

```bash
python /home/light/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/light/.agents/skills/ctf
python /home/light/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/light/.agents/skills/ctf-toolkit
```

## Latest prompt implementation

`SolverService.build_prompt(job)` has already been shortened to exactly:

```text
/ctf-toolkit
CTF: <title from challenges.json ctf_info, or workspace directory name>
Challenge: <challenge name>
Path: <absolute challenge path>
```

No further instructions should be added to this prompt unless the user changes this decision. NOTE owns the workflow rules.

## Tests added and last verified state

New files under `tests/`:

- `tests/test_solver_service.py`
- `tests/test_solver_cli.py`
- `tests/test_solver_workspace_rules.py`

The last code verification, after the short-prompt and NOTE updates, was:

```bash
pytest tests/test_solver_service.py tests/test_solver_cli.py tests/test_solver_workspace_rules.py -q
python -m compileall -q ctf_downloader
```

Result: `12 passed` and compileall succeeded.

The tests cover scanning/classification, bad ID input, three-worker ceiling, progress/log persistence, stale-PID recovery, preexisting-template false completion, a Gemini filter message, parser integration, short prompt, NOTE preservation and template adapters.

## Live demo attempted

Demo workspace (isolated copies; no `writeup/` or preexisting `solver/solve.py`):

```text
work/solver-live-demo/
├── Crypto/leaky_rsa
├── Forensics/close_enough
└── Pwn/big-win
```

These are real payload copies from `/home/light/Workspace/CTF/K17_CTF_2026`, selected as the available source-bearing categories. The first run launched all three Agy processes simultaneously and the dashboard showed all three as `running`, confirming the three-worker pool. Crypto and Forensics emitted `analysis` progress markers. Pwn returned the exact Gemini filter phrase above.

That run used the earlier long prompt and was deliberately terminated after the user required the short prompt. Current state in the demo workspace is intentionally failed:

- Crypto and Forensics: `E_WORKER_CRASH` because their process groups were stopped.
- Pwn: `E_VERIFY_LOCAL` because the old running process used the old detector; source code now has a regression test and recognizes it as `E_FILTER`.

No flags were submitted. Do not treat this demo as a solve result.

Before retrying the demo with the new short prompt, refresh the managed NOTE block in each copied challenge because the copies predate the new rule. The simplest safe approach is to recreate the isolated demo workspace, copy only `challenge/` plus `metadata.json`, make an empty `script/`, then call the builder's managed-rule merger or explicitly add its marker block. Do not copy `writeup/` or old `solver/` into the demo.

Then run from the source repo:

```bash
cd work/solver-live-demo
ctf status --solver
ctf solve --ids 1,2,3 --workers 3 --timeout 600
```

Use a real TTY. In captured/non-TTY logs Rich Live emits many redraw lines; in a terminal it is a single smooth table.

All demo Agy processes and the manager were stopped before this handoff. Confirm with `ps` before restarting.

## Important unfinished work

1. **Run the requested live demo again with the short prompt.** Let it finish or reach the time limit. Inspect `ctf status --solver`, `script/agy.log`, `script/progress.json`, `script/worker-state.json`, `script/worker-report.json`, and `solver/solve.py`. Report performance and terminal states honestly. Do not submit flags.

2. **Implement the animated loading icon.** `_solver_table` currently displays a fixed `◌`. The user asked for a loading icon when Agy is active. Implement an animated frame tied to `time.monotonic()` or use a Rich spinner with a non-TTY fallback, then add a focused rendering test if practical.

3. **Make state writes race-safe.** `_write_job()` currently uses `read_job()` + `atomic_write_json()` without a lock. The Agy output reader thread and scheduler thread can overwrite different fields. Replace it with existing `locked_update_json()` or a per-state lock so `pid`, heartbeat, last output and final state cannot be lost.

4. **Improve refusal detection.** `_finish_worker()` currently checks only `last_output`; a refusal line followed by ordinary output can be missed. Persist `filter_detected=True` during `_append_output()` or scan the full current log before final classification. Add a regression test where the refusal is not the final line.

5. **Implement an actual stale-heartbeat policy.** The current manager has a hard timeout only. It should surface an explicit stalled state/code when a process stays alive without output/heartbeat for a configurable period, instead of showing stale `running` until 3600 seconds. Do not kill/retry automatically without recording an error; filtered sessions must never receive a follow-up in the same conversation.

6. **Fix Ctrl-C semantics or text.** `handle_solve()` says it keeps workers alive, which is true for existing child groups, but the foreground scheduler exits and queued jobs will not start. Either implement a detached/resumable manager with a durable queue or change the UX to state precisely that Ctrl-C ends scheduling. This is a real contract gap.

7. **Decide and implement no-source policy.** The user earlier said challenges without source should be left alone, while the list must show source/instance classification. Current code lets the user select them. Confirm the desired behavior from conversation context or implement a clear `skipped_no_source` state if appropriate; never invent a local server.

8. **Update the project-local duplicate ctf-toolkit skill only if it is the active one for Agy.** Global `/home/light/.agents/skills/ctf-toolkit/SKILL.md` was updated. The project also has `.agents/skills/ctf-toolkit/SKILL.md`, which was already modified before this session and should not be overwritten blindly. Compare it before editing.

9. **Keep the short prompt.** Do not re-add source/instance/rules into `build_prompt()`; those belong in NOTE and skills by user instruction.

10. **Do not touch unrelated working-tree changes.** This repo already has a very large set of modifications and root-level deleted tests from prior work, plus untracked `tests/`, BQA files, platform files, docs and `work/`. Do not restore, delete, stage, or commit unrelated changes. The new solver tests live in the untracked `tests/` directory alongside preexisting test migration work.

## Installation fact

`ctf` is exposed by pipx, but its venv imports this source checkout directly:

```bash
/home/light/.local/share/pipx/venvs/ctf-toolkit/bin/python -c 'import ctf_downloader; print(ctf_downloader.__file__)'
# /home/light/Workspace/Project/auto_download_ctf_challenge/ctf_downloader/__init__.py
```

`pipx install --force .` printed an existing-venv warning but the executable already resolves the modified source. Verify `ctf solve --help` from a non-repo workspace before relying on it.
