# GPT CTF Controller Handoff

This document records the current integration between `ctf`, the local `gpt`
CLI, and the `br` host connector.  Start by checking `git status` in both
repositories; the feature branches are `feat/ctf-autonomous-session-controller`
for this repository and `feat/ctf-session-contract` in `/home/light/GitHub/gpt_cli`.

## Runtime flow

1. `ctf solve --engine gpt` selects a challenge and starts one scheduler worker.
2. `GptSolverAdapter.create_session()` invokes `gpt session new --json` before
   the worker process exists.  The returned `gpt_session_id` is stored in
   `script/worker-state.json`.
3. The first turn runs `gpt run --resume-session <id> -b br --json -p "solve <absolute-path>"`.
4. GPT persists the ChatGPT conversation pointer.  When `host_workspace_bind`
   returns a result, it also persists BQA `chat_id` and workspace path.
5. The final GPT JSON line updates the CTF worker state with `conversation_id`,
   `gpt_session_id`, `bqa_chat_id`, and `bqa_workspace_path`.
6. A normal turn without a locally verified flag is queued again with the same
   GPT local session.  Its next prompt is `host_workspace_bind resume_id=<chat_id>; continue`.

The scheduler never infers an old workspace from the current directory.  If a
prior GPT session has no recorded `bqa_chat_id`, continuation enters
`blocked/workspace` with `E_WORKSPACE_MAPPING`.

## State and outcome contract

`script/worker-state.json` is the scheduler checkpoint.  Relevant fields are:

| Field | Meaning |
|---|---|
| `gpt_session_id` | Local GPT CLI session, created before the first worker launch. |
| `conversation_id` | Last observed ChatGPT server-side conversation. |
| `bqa_chat_id` | BQA workspace identity used for every resumed tool turn. |
| `bqa_workspace_path` | Path returned by BQA for diagnostics only. |
| `continuation_attempts` | Number of normal no-flag turns requeued for this challenge. |
| `continuation_instruction` | `continue` or `verify candidate`. |

The scheduler classifies these results:

| Condition | Result |
|---|---|
| Exit 0, no local verification | Queue the same GPT session with `continue`. |
| Candidate present but not locally verified | Queue `verify candidate`. |
| Missing BQA mapping on continuation | `E_WORKSPACE_MAPPING`, blocked. |
| Session creation failure | `E_GPT_SESSION`, failed. |
| Timeout, stalled worker, transport crash, filter/refusal, quota | Terminal worker result for review; no automatic continuation. |
| Local verification passed | `completed`, and the local flag state is updated. |

`gpt` may report `queued` or `steered` while a local session is live.  These are
delivery acknowledgements, not proof that a model task completed.  Use the
worker state, the GPT session history, and BQA journal together when debugging.

## Worker limit configuration

The global maximum is read in this order:

1. `CTF_SOLVER_MAX_WORKERS` from the process environment.
2. `CTF_SOLVER_MAX_WORKERS` in `<ctf-workspace>/.env`.
3. Default `3`.

Copy `.env.example` into the event workspace when a persistent setting is
needed.  `--workers` can select a smaller pool but the scheduler rejects a
request above the configured cap.  The interactive menu and detached daemon
use the same configured cap, including per-category scheduling.

```bash
cp /home/light/Workspace/Project/auto_download_ctf_challenge/.env.example \
  "/path/to/CTF workspace/.env"
ctf solve -w "/path/to/CTF workspace" --engine gpt --workers 1
ctf solve -w "/path/to/CTF workspace" --status
```

## Verification completed

- GPT CLI lifecycle/session tests: 50 passed.
- CTF adapter, scheduler, CLI, daemon, and worker-setting tests: 84 passed.
- No live ChatGPT/BQA run was made before this handoff.  The first live run
  should use one challenge and one worker, then inspect `worker-state.json`,
  `script/gpt.log`, and `gpt session show <gpt_session_id> --json`.

## Files to inspect first

- `ctf_downloader/solver/adapters/gpt.py` — session provision, command shape,
  JSON telemetry decoding.
- `ctf_downloader/services/solver_service.py` — state writes, continuation
  policy, global scheduling limit.
- `ctf_downloader/solver/settings.py` — `.env` and environment configuration.
- `/home/light/GitHub/gpt_cli/gpt_cli/cli/turn_runner.py` — BQA bind result is
  attached to the GPT session record.
- `/home/light/GitHub/gpt_cli/docs/CTF_SESSION_CONTRACT.md` — the cross-repo
  session contract.
