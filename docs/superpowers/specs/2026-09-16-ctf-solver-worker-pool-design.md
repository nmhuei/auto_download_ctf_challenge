# CTF Solver Worker Pool Design

## Goal

Add an interactive `ctf solve` workflow that classifies downloaded challenges, accepts comma-separated IDs, and runs up to three isolated Agy CLI workers at once while exposing durable progress through `ctf status`.

## Boundaries

The scheduler is local-only. It never submits flags, never invokes Codex collaboration subagents, and starts no remote connection itself. Agy is prompted to solve and verify locally first; remote adapters exist only in the final solver and run only when their arguments are supplied.

Challenge-specific operational artifacts belong in `<challenge>/script/`: `agy.log`, `worker-state.json`, `progress.json`, temporary probes and local harnesses. `solver/solve.py` is reserved for the reusable final solver. The workspace-level `.ctf-solver/` directory contains only the queue lock and a compact scheduler snapshot; it contains no challenge scripts or solver output.

## Components

`SolverService` owns scanning, classification, validation, durable job state, scheduling, process lifecycle, and rendering data. It is the only process allowed to claim or update a job. A job state is stored per challenge under `script/worker-state.json` using atomic writes. The single manager lock at `.ctf-solver/manager.lock` rejects a second scheduler in the same workspace.

`ctf solve` renders a Rich table grouped and sorted by category, then accepts `1,2,3` from stdin. The selection is persisted before processes are launched. `--ids` supports noninteractive automation. The default and maximum worker count are three; `--workers` accepts one through three.

Each Agy child is placed in a separate process group, runs with the challenge directory as `cwd`, and has stdout plus stderr appended to `script/agy.log`. The manager records its PID, start time, heartbeat, phase and last output line. Agy may emit `@@CTF_PROGRESS@@ {"phase":"...","message":"..."}`; malformed events never fail the worker and plain output remains the fallback status text.

## Classification and state

Source is true when the challenge payload has files other than generated workspace placeholders. Instance is true when `metadata.instance_info`, `connection_info`, or a dynamic-container declaration indicates one. The UI keeps the source/instance classifications separate from worker state.

State values are `queued`, `running`, `completed`, `failed`, `filtered`, and `cancelled`. Failure codes include `E_WORKER_CRASH`, `E_TIMEOUT`, `E_FILTER`, and `E_VERIFY_LOCAL`. A terminal `completed` result requires `solver/solve.py` to exist; the manager records that fact rather than claiming a flag or a submitted solve.

## Recovery and observability

On every scheduler launch, stale `running` state is recovered: a live PID stays owned; a dead PID becomes `failed` with `E_WORKER_CRASH`. Ctrl-C detaches from the live display without killing worker groups. `ctf status --solver` renders states and `ctf status <id> --solver` prints the selected worker's state and the final log lines. `ctf status --solver --watch` refreshes the same view until Ctrl-C.

## Testing

Tests use temporary workspaces and a small executable fake Agy script. They verify classification, selected-ID validation, the three-worker ceiling, one job claim per challenge, output/progress persistence, stale-PID recovery, and parser integration. No test calls a real Agy process or a platform.
