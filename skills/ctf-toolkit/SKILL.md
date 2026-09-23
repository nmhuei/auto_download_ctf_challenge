---
name: ctf-toolkit
description: Use when opening or advancing CTF challenges, including shorthand such as pwn5, rev12, Lottery, or terse steering prompts such as next, decoy, and tiến độ. Handles challenge downloading, Burp Suite cookie auto-sync, dynamic containers, parallel AI autosolvers with filter self-recovery, and local flag hoarding.
---

# CTF Toolkit (`ctf` CLI)

Treat concise user input as an actionable command packet. Preserve momentum: resolve state, perform the next local evidence-producing action, and report verified results without conversational fluff.

## Mandatory Rule: No Automated Flag Submission

**Agents, BQA, scripts, and automated workflows MUST NEVER invoke `ctf submit`, `ctf submit --auto`, or `ctf hoard --all`.**
Found flags must be saved locally with `ctf hoard` or recorded in `script/analysis.md`. The agent reports candidate flags along with deterministic proof for the user to review and submit manually from their own terminal.

---

## Route First: Prompt-as-Command Routing

When invoked via `/ctf-toolkit <target>`, `/ctf <target>`, or terse shorthand:
1. Run or evaluate against `references/routing.md` with strict precedence:
   `REJECT_CANDIDATE` → `STATUS` → `CONTINUE` → `SELECT` → `HYPOTHESIS` → `CONSTRAINT`.

| Normalized Input | Action | Immediate Deterministic Behavior |
|---|---|---|
| `pwn5`, `rev12`, `crypto_3`, `Lottery` | `SELECT` | Resolve challenge directory across active workspaces. Read `metadata.json`, `challenge/NOTE.md`, files in `challenge/`, and previous progress in `script/`. Begin triage immediately—**do not ask questions**. |
| `/ctf-toolkit <target>` | `SELECT` | Same as shorthand selector; explicit slash invocation maps to identical route. |
| `next`, `nexxt`, `continue`, `tiếp tục` | `CONTINUE` | Load `script/` state and execute the very next uncompleted verification or probe. |
| `tiến độ`, `tieens ddooj`, `status` | `STATUS` | Emit 4-bullet evidence report: phase, verified facts, next probe, blocker. |
| `decoy`, `cái này là flag decoy` | `REJECT_CANDIDATE` | Mark candidate rejected in `script/analysis.md`, retain provenance, pivot to next branch. |
| `có thể đó là hint...` | `HYPOTHESIS` | Record hypothesis and test via deterministic local probe. |
| `server chỉ connect qua lan`, `dùng mcp` | `CONSTRAINT` | Amend scoped runtime parameters in memory and continue without resetting progress. |

*See [routing.md](references/routing.md) for full precedence rules and resolution scoring.*

---

## Canonical CLI Commands

| Action | Command | Key Options |
|---|---|---|
| **Auth** | `ctf auth --from-burp -u <URL>` | `--from-burp`, `--show`, `--clear`, `-c <COOKIE>`, `-t <TOKEN>` |
| **Pull** | `ctf pull -u <URL> -o <DIR>` | `--save-cookie`, `--verify-downloads strict`, `--no-git`, `--bridge` |
| **Instance**| `ctf instance start --id <ID> -w <WORKSPACE>`| `start`, `stop`, `restart`, `renew` · `--list`, `--auto-extend` |
| **Solve** | `ctf solve -w <WORKSPACE> --workers 3` | `--workers <N>`, `--per-category`, `--detach`, `--status`, `--stop` |
| **Hoard** | `ctf hoard <ID> "FLAG{...}"` | `--list`, `--remove <ID>` |
| **Doctor** | `ctf doctor -u <URL>` · `ctf doctor --runtime` | `-c <COOKIE>`, `-t <TOKEN>`, `-w <WORKSPACE>` |
| **Theme** | `ctf config theme <THEME>` | `cyberpunk`, `matrix`, `amber`, `exodia`, `dracula`, `tokyo` |
| **Git** | `ctf git init -d <DIR> --remote-url <URL>` | `status`, `push`, `finish` (merges event branch to main) |

---

## Evidence Contract & Workspace Discipline

- **No Assertions Without Proof**: Never declare a challenge solved without local deterministic reproduction.
- **Directory Roles**:
  - `challenge/`: Read-only source artifacts and authoritative `NOTE.md`.
  - `script/`: Scratch probes, harnesses, logs, and `analysis.md`.
  - `solver/`: Self-contained, final reproducible `solve.py`.
- **Candidate States**: Every prospective flag is recorded as `CANDIDATE`, `VERIFIED`, or `REJECTED`.

*See [evidence.md](references/evidence.md) for complete verification rules.*

---

## Safety Filter Recovery & Context Isolation

- **Connection Endpoint Sanitization**: Never print raw IPs or wildcard DNS (`.nip.io`) in model prompts; sanitize to local metadata references.
- **Refusal Inertia & 3-Tier Rescue**:
  - **Tier 1 (Directed Micro-Prompt)**: `next: continue logic verification` or `/ctf-toolkit next`.
  - **Tier 2 (Context Rollback)**: Use `/rewind` or prune poisoned refusal turns from conversation history.
  - **Tier 3 (Math/Logic Isolation)**: Extract formal roadblocks to `math_workspace/` (`TASK.md` + `instance.json`) and invoke the `ctf-ask` skill. Never leak challenge context, URLs, or binaries to Astra.

*See [recovery.md](references/recovery.md) for empirical failure mode analysis.*
