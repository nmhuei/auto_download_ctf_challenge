# Git Sync & AGYworker Solver UX Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the interactive Git synchronization system (monorepo & branch lifecycle, safe pull/push, anti-bloat large-file guards, menu integration) and audit/polish the AGYworker solver UI experience to ensure terminal output is balanced, readable, and cohesive with the PHOSPHOR FIELD KIT design system.

**Architecture:** 
1. Core Git Service (`git_workflow.py`): Add workspace-scoped dirty checking (preventing foreign contest folders from locking the repo), safe 2-way sync (`git pull --rebase`), and large-file scanner (>50MB).
2. UI Interactive Layer (`interactive_menu.py`): Introduce `[G] Git Sync & Remote Backup` action with a dedicated interactive sub-console, auto-checkpoint hooks on flag capture, and initial snapshot on challenge download.
3. AGYworker Solver UX (`solver_service.py`, `cli_commands.py`, `interactive_menu.py`): Audit and tune live radar formatting, worker daemon indicators, log tailing, and ensure graceful terminal resizing and clean status summaries.

**Tech Stack:** Python 3.10+, Rich (Console, Table, Text, Live, Panel, Theme), Git CLI subprocess, Pytest.

**Spec:** In-session design approved by user (Monorepo Arsenal + Smart Anti-Bloat + PHOSPHOR FIELD KIT UI v2).

## Global Constraints
- All UI chrome MUST adhere to PHOSPHOR FIELD KIT semantic tokens (`CYAN`, `SUCCESS`, `WARNING`, `ERROR`, `MUTED`, `FAINT`).
- Staged commits MUST NOT leak unrelated files outside the target workspace.
- Push operations MUST detect files exceeding 50MB and warn/exclude them before GitHub 100MB rejection.
- Terminal tables MUST truncate with ellipses (`…`) and respect terminal column widths without wrapping table headers ugly.

---

### Task 1: Scoped Dirty Tree & Large File Guard in GitWorkflowService

**Files:**
- Modify: `ctf_downloader/services/git_workflow.py`
- Test: `tests/test_git_workflow_enhancements.py`

**Interfaces:**
- Consumes: `subprocess.run`, `Path`, `GitWorkflowError`
- Produces: 
  - `GitWorkflowService.scan_large_files(workspace: Path, threshold_mb: int = 50) -> list[tuple[Path, int]]`
  - `GitWorkflowService.safe_sync(workspace: Path, remote: str = "origin") -> dict[str, Any]`
  - `GitWorkflowService.checkpoint_and_push(..., scoped_only: bool = True)`

- [ ] **Step 1: Write failing tests for scan_large_files and scoped dirty checking**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `scan_large_files` and update `_assert_clean` with `scoped_path` parameter**
- [ ] **Step 4: Implement `safe_sync` (git pull --rebase + push)**
- [ ] **Step 5: Run tests and verify they pass**
- [ ] **Step 6: Commit changes to `feature/git-workflow`**

---

### Task 2: Implement Interactive Git Sub-Menu `[G]` in InteractiveConsole

**Files:**
- Modify: `ctf_downloader/interactive_menu.py`
- Modify: `ctf_downloader/services/git_workflow.py`
- Test: `tests/test_interactive_git_menu.py`

**Interfaces:**
- Consumes: `GitWorkflowService`, `_section`, `_option`, `_prompt`, `_pause`, `_menu_console`
- Produces:
  - `CTFInteractiveConsole._menu_git()`
  - `_MAIN_ACTIONS_FULL` includes `('G', 'Git Sync & Remote Backup (Kho vũ khí GitHub)')`

- [ ] **Step 1: Write failing test for `_menu_git` menu dispatch and actions**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `_menu_git` in `interactive_menu.py` with actions: Push, Sync, Status, Large File Scan**
- [ ] **Step 4: Add `[G]` to `_MAIN_ACTIONS_FULL` and wire key dispatch in `run()`**
- [ ] **Step 5: Run tests and verify they pass**
- [ ] **Step 6: Commit changes to `feature/git-workflow`**

---

### Task 3: Automatic Git Snapshot on Download & Auto-Checkpoint on Solved Flags

**Files:**
- Modify: `ctf_downloader/interactive_menu.py`
- Modify: `ctf_downloader/services/pull_service.py`
- Test: `tests/test_git_auto_checkpoint.py`

**Interfaces:**
- Consumes: `GitWorkflowService.checkpoint_and_push`
- Produces: Automatic background checkpoint when flag is captured or challenge downloaded

- [ ] **Step 1: Write failing tests for download snapshot and flag submit auto-checkpoint**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Enable `git_workflow` in `_menu_download_new` and create initial commit**
- [ ] **Step 4: Hook auto-checkpoint into `_menu_submit_flag` and `_menu_auto_submit` on success**
- [ ] **Step 5: Run tests and verify they pass**
- [ ] **Step 6: Commit changes to `feature/git-workflow`**

---

### Task 4: AGYworker & SuperBQA Solver UI Audit and Polish

**Files:**
- Modify: `ctf_downloader/interactive_menu.py`
- Modify: `ctf_downloader/cli_commands.py`
- Test: `tests/test_solver_ui_polish.py`

**Interfaces:**
- Consumes: `SolverService`, `_solver_table`, `render_active_agy_workers`
- Produces: Polished, balanced table with responsive columns, intuitive keybindings, and phosphor-themed diagnostic cards

- [ ] **Step 1: Write test inspecting solver table rendering and column width bounds**
- [ ] **Step 2: Run test to verify baseline formatting**
- [ ] **Step 3: Refine table widths, alignment, and status icons in `_solver_table`**
- [ ] **Step 4: Polish option prompts and help texts in `_menu_solver`**
- [ ] **Step 5: Run tests and verify formatting and stability**
- [ ] **Step 6: Commit changes to `feature/git-workflow`**

---

### Task 5: End-to-End Practical UI & Real Workspace Verification

**Files:**
- Create: `tests/test_practical_git_and_solver.py`
- Test: Full pytest suite + live workspace validation on `/home/light/Workspace/CTF/ASIS_CTF_Quals_2026`

- [ ] **Step 1: Create practical end-to-end test executing all Git and Solver flows**
- [ ] **Step 2: Run practical test and inspect console outputs**
- [ ] **Step 3: Run full pytest suite across entire project**
- [ ] **Step 4: Final commit and summary report**
