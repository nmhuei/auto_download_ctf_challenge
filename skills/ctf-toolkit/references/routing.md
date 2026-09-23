# Prompt Routing & Resolution Reference

This document details the deterministic command routing and challenge resolution contract for the CTF Toolkit.

## Precedence Order

When user input matches multiple patterns, apply the following strict precedence:
1. `REJECT_CANDIDATE`: Decoy or fake flag notifications (`decoy`, `cái này là flag decoy`)
2. `STATUS`: Progress queries (`tiến độ`, `tieens ddooj`, `status`)
3. `CONTINUE`: Unassisted momentum nudges (`next`, `nexxt`, `continue`, `tiếp tục`)
4. `SELECT`: Challenge targets (`pwn5`, `rev12`, `Lottery`, `/ctf-toolkit pwn5`)
5. `HYPOTHESIS`: Emerging leads (`có thể đó là hint`, `maybe`)
6. `CONSTRAINT`: Environment restrictions (`server chỉ connect qua LAN`, `dùng mcp`)

## Action Semantics

### 1. `SELECT` (`pwn5`, `rev12`, `Lottery`, `/ctf-toolkit <target>`)
- **Agent Behavior**:
  1. Resolve challenge path immediately across active CTF workspaces (e.g. `/home/light/Workspace/CTF/*/<category>/<chal>`).
  2. Inspect durable state: read `<chal_dir>/metadata.json` and `<chal_dir>/challenge/NOTE.md`.
  3. Inspect attachments in `<chal_dir>/challenge/` and prior logs in `<chal_dir>/script/`.
  4. Begin triage and diagnostic probes immediately without asking clarifying questions or repeating boilerplate.

### 2. `CONTINUE` (`next`, `nexxt`, `continue`)
- **Agent Behavior**:
  1. Load current execution state from `script/` or `NOTE.md`.
  2. Execute the very next uncompleted verification or probe step.
  3. If previous step was blocked by safety filter, switch to Tier 1 directed task prompt (`next: continue logic verification`) or Tier 3 math isolation.

### 3. `STATUS` (`tiến độ`, `tieens ddooj`, `status`)
- **Agent Behavior**:
  - Emit an ultra-concise 4-bullet report:
    - **Phase**: Current operational phase (e.g. Decompilation, Protocol Analysis, SMT Solving).
    - **Verified Facts**: Concrete deterministic observations backed by logs.
    - **Next Probe**: Exactly what will be executed next.
    - **Blocker**: Any hard external roadblock (e.g. container expired, need network creds).

### 4. `REJECT_CANDIDATE` (`decoy`, `cái này là flag decoy`)
- **Agent Behavior**:
  - Mark active candidate as `REJECTED (decoy)` in `script/analysis.md`.
  - Preserve the audit trail and provenance so it is not re-tested.
  - Immediately backtrack to the branch point and explore secondary targets or alternative decoding pipelines.

### 5. `CONSTRAINT` (`server chỉ connect qua lan`, `dùng mcp`)
- **Agent Behavior**:
  - Amend active runtime environment parameters in memory and `NOTE.md`.
  - Do not reset or discard already verified decompilation or mathematical results.

## Resolution Scoring Algorithm

When resolving target strings:
1. Exact normalized name match (`name.casefold() == target.casefold()`)
2. Category shorthand + index (`pwn5` -> category starting with `pwn`, index 5)
3. Directory basename / slug match (`Lottery` -> `🎟_The_Lottery_Race`)
4. Single unambiguous substring match across active challenges
5. If resolution fails unambiguously: ask the user with the list of candidate matches.
