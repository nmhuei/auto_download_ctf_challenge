# Modern 5-Hub CTF Cockpit & SuperBQA Solver UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the cluttered 13-item interactive menu into a sleek, modern 5-Hub Tactical Cockpit, unify challenge operations into an actionable Command Card, polish SuperBQA/AGYworker solver telemetry without hardcoded styles, and preserve hotkey backward compatibility.

**Architecture:**
1. Modular Hubs (`ctf_downloader/ui/menu_hubs.py`): Extract dedicated controllers for `WorkspaceHub`, `ChallengeHub`, `FlagHub`, `SolverHub`, and `SystemHub` to decouple the monolith `interactive_menu.py`.
2. Main Cockpit Controller (`ctf_downloader/interactive_menu.py`): Render the high-impact status card and 5 core tactical options, dispatching cleanly to sub-hubs while handling legacy shortcut keys (`G`, `T`, `S`).
3. Solver UX Audit (`ctf_downloader/cli_commands.py`): Clean up raw un-themed markups (`[bold green]`, `[dim]`), polish table column ratios, and streamline daemon status inspection.
4. Comprehensive Testing (`tests/test_modern_menu_hubs.py`): Ensure all sub-menus, challenge cards, theme toggles, and shortcuts pass with 100% reliability.

**Tech Stack:** Python 3.10+, Rich (Console, Table, Text, Live, Panel, Theme), Git CLI, Pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-modern-menu-and-solver-ux.md`

## Global Constraints
- Strictly NO hardcoded hex codes, ANSI colors, or inline raw style strings in UI code (consume semantic tokens from `theme.py`).
- Single Responsibility Principle: decompose sub-menus into `ctf_downloader/ui/menu_hubs.py`.
- Preserve full backward compatibility for shortcuts (`G`, `T`, `S`).
- All interactive tables must adapt responsively to terminal widths without line wraps breaking table rows.

---

### Task 1: Create Modular Sub-Hubs (`ctf_downloader/ui/menu_hubs.py`)

**Files:**
- Create: `ctf_downloader/ui/menu_hubs.py`
- Test: `tests/test_modern_menu_hubs.py`

**Interfaces:**
- Consumes: `CTFInteractiveConsole`, `WorkspaceRepo`, `StatusService`, `InstanceService`, `FlagSubmitter`, `SolverService`, `GitWorkflowService`
- Produces:
  - `render_hub_menu(console, title, actions, prompt_text)`
  - `hub_workspace_targets(console_app) -> None`
  - `hub_challenge_operations(console_app) -> None`
  - `hub_flag_submission(console_app) -> None`
  - `hub_system_arsenal(console_app) -> None`

- [ ] **Step 1: Write failing unit test for `menu_hubs.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `menu_hubs.py` with modular sub-menus for Hub 1 (Workspace), Hub 3 (Flags), Hub 5 (System)**
- [ ] **Step 4: Run test to verify passes**
- [ ] **Step 5: Commit changes to `feature/git-workflow`**

---

### Task 2: Unified Challenge Action Card & Dynamic Instance Integration

**Files:**
- Modify: `ctf_downloader/ui/menu_hubs.py`
- Modify: `ctf_downloader/interactive_menu.py`
- Test: `tests/test_modern_menu_hubs.py`

**Interfaces:**
- Consumes: Challenge dictionary, `InstanceManager`, `FlagSubmitter`, `SolverService`
- Produces:
  - `challenge_action_card(console_app, challenge_dict)` offering: View Info, Container Control, Submit Flag, Launch Solver.

- [ ] **Step 1: Write failing test for challenge action card flow**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `challenge_action_card` and hook it into `hub_challenge_operations`**
- [ ] **Step 4: Run test to verify passes**
- [ ] **Step 5: Commit changes to `feature/git-workflow`**

---

### Task 3: Restructure Main Cockpit in `interactive_menu.py`

**Files:**
- Modify: `ctf_downloader/interactive_menu.py`
- Test: `tests/test_interactive_menu_features.py`
- Test: `tests/test_modern_menu_hubs.py`

**Interfaces:**
- Consumes: `_MAIN_ACTIONS_FULL`, `_MAIN_ACTIONS_COMPACT`, `menu_hubs`
- Produces: Streamlined 5-action main menu:
  - `[1] 🎯 Workspace & Targets`
  - `[2] ⚔️ Challenge Operations`
  - `[3] 🚩 Flag Submission Lab`
  - `[4] ⚡ SuperBQA AI Solver`
  - `[5] 🛠️ System & Arsenal`
  - `[0] 🚪 Exit`
  - Direct shortcut fallbacks for `G`, `T`, `S`.

- [ ] **Step 1: Update action definitions in `interactive_menu.py`**
- [ ] **Step 2: Route actions `1-5`, `G`, `T`, `S`, `0` to corresponding hubs**
- [ ] **Step 3: Update existing test suite mocks to accommodate new menu layout**
- [ ] **Step 4: Run all interactive menu tests and verify they pass**
- [ ] **Step 5: Commit changes to `feature/git-workflow`**

---

### Task 4: SuperBQA & AGYworker Solver Telemetry Audit

**Files:**
- Modify: `ctf_downloader/cli_commands.py`
- Modify: `ctf_downloader/interactive_menu.py`
- Test: `tests/test_solver_cli.py`

**Interfaces:**
- Consumes: `SolverService`, `theme.py` semantic tokens
- Produces: Clean themed daemon banner (replaces `[bold green]` and `[dim]`), responsive table columns, and streamlined options.

- [ ] **Step 1: Replace raw unthemed markup in `_render_solver_status` with semantic theme tokens**
- [ ] **Step 2: Ensure column overflow in `_solver_table` behaves cleanly on narrow screens**
- [ ] **Step 3: Run solver CLI tests and verify stability**
- [ ] **Step 4: Commit changes to `feature/git-workflow`**

---

### Task 5: End-to-End Real Workspace Validation & Codex Review

**Files:**
- Validate: `/home/light/Workspace/CTF/ASIS_CTF_Quals_2026`
- Script: `scratch/test_cockpit_e2e.py`
- Verify: Full pytest suite

- [ ] **Step 1: Run comprehensive end-to-end simulation across all 5 hubs on real workspace**
- [ ] **Step 2: Run full project pytest suite**
- [ ] **Step 3: Invoke Codex CLI (`/home/light/.local/bin/codex exec`) for independent architecture audit**
- [ ] **Step 4: Final commit, push to origin, and summary report**
