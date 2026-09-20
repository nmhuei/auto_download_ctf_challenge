# Modern CTF Cockpit & SuperBQA Solver UX Specification

## 1. Executive Summary
The interactive console (`ctf menu` / `ctf_downloader/interactive_menu.py`) previously presented a flat, cluttered 13-item list without visual grouping or logical hierarchy. Functions with identical or related responsibilities were fragmented across disparate menu numbers (e.g., workspaces split across 2 and 8; challenges split across 3, 4, 5; flags split across 6 and 7; solver under an enigmatic title `[S] SUPERBQA EATING`).

This specification establishes the **PHOSPHOR COMMAND CENTER v3**:
- A streamlined **5-Hub Tactical Cockpit** on the main screen.
- Unified **Challenge Action Card** (view info, start instance, submit flag, or dispatch AI solver from one place).
- Polished **SuperBQA / AGYworker UX** with live radar telemetry, balanced column ratios, and clean log tailing.
- 100% Theme-decoupled styling with zero hardcoded ANSI/hex codes.
- Backward compatibility for direct quick-keys (`G`, `T`, `S`).

---

## 2. Information Architecture: 5 Tactical Hubs

### Hub 1: `[1] Workspace & Targets` (Chiến trường & Nền tảng)
Consolidates all workspace and authentication actions:
1. `[1] Switch Active Workspace`: Interactive selector listing all local CTF workspaces with solved progress meters.
2. `[2] Clone / Download New CTF`: Interactive wizard to fetch challenges & attachments from CTFd / GZCTF / rCTF.
3. `[3] Configure Auth (Cookie / Token)`: Update credentials for the current platform.
4. `[4] Platform Health Check (Doctor)`: Check network connectivity, auth validity, and Cloudflare status.
5. `[0] Back to Main Cockpit`

### Hub 2: `[2] Challenge Operations` (Tác chiến Challenge & Instance)
Consolidates challenge exploration and dynamic container control:
1. `[1] Interactive Challenge Explorer (Tree View)`: Browse challenges grouped by category with visual meters.
2. `[2] Select Challenge & Open Action Card`:
   Selecting a challenge opens its **Challenge Command Card**:
   - `[1] View Description, Hints & Attachments`
   - `[2] Dynamic Instance: Start / Stop / Extend container`
   - `[3] Submit Flag for this challenge`
   - `[4] Dispatch SuperBQA Solver (AGYworker)`
3. `[3] Dynamic Container Fleet Overview`: List all running instances across challenges with countdown timers.
4. `[0] Back to Main Cockpit`

### Hub 3: `[3] Flag Submission Lab` (Trung tâm Nộp Flag)
Consolidates all flag submission and hoarded flag harvesting:
1. `[1] Submit Flag for a Specific Challenge`: Quick-select challenge and submit flag with immediate server feedback.
2. `[2] Auto-Scan & Submit Hoarded Flags`: Harvest all local flags recovered by solvers and submit automatically.
3. `[3] View Flag History & Hoarded Vault`: Inspect submitted flags, timestamps, and local hoarded secrets.
4. `[0] Back to Main Cockpit`

### Hub 4: `[4] SuperBQA AI Autonomous Solver` (Trí tuệ Nhân tạo Tác chiến)
Consolidates all AI agent automation and worker orchestration:
1. `[1] Live Radar (Bản đồ Tác chiến Real-time)`: Live updating telemetry of active agents, elapsed time, phase, and candidate flags.
2. `[2] Auto-Solve All Eligible Challenges`: Launch background daemon across all unsolved challenges with attachments/instances.
3. `[3] Solve Specific Challenge(s)`: Select target challenge IDs to solve.
4. `[4] Worker Daemon Manager`: Check background daemon PID, stop/kill workers, or inspect logs (`tail -f`).
5. `[0] Back to Main Cockpit`

### Hub 5: `[5] System & Arsenal` (Hệ thống & Kho vũ khí)
Consolidates Git versioning and visual customization:
1. `[1] Git Sync & Remote Backup`: Safe 2-way sync (`git pull --rebase` + `push`) and large-file guard (>50MB).
2. `[2] Switch Visual Theme`: Instantly toggle between Cyberpunk Neo-Tokyo, Matrix Emerald, Amber DEFCON, Nord Frost, and ExOdia Cyan.
3. `[3] Global Configuration`: Manage default workspace root and auto-sync settings.
4. `[0] Back to Main Cockpit`

---

## 3. Visual Design System & Terminal Balance
- **Cockpit Header**: Displays competition name, platform badge, connection state, Git branch, and progress meter.
- **Responsive Width Policy**:
  - Console widths >= 88 cols: Full descriptive labels with status badges.
  - Console widths < 88 cols: Compact responsive layout with ellipses (`…`) truncation; no column header breakage.
- **Zero Hardcoding**: All colors must resolve from `ctf_downloader/ui/theme.py` semantic styles (`accent`, `surface`, `border`, `fg.base`, `fg.muted`, `fg.faint`, `success`, `warn`, `error`).

---

## 4. Migration & Compatibility
- Direct hotkeys `G` (Git), `T` (Theme), `S` (Solver) on the main menu will continue to work directly for fast muscle-memory access.
- All existing CLI commands (`ctf pull`, `ctf status`, `ctf solve`, `ctf git`, etc.) remain 100% unaffected.
