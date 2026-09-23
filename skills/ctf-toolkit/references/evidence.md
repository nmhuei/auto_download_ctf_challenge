# Evidence Contract & Anti-Hallucination Protocol

This document defines the strict local verification rules and evidence lifecycle required for all CTF solving operations.

## Mandatory Rules

1. **Deterministic Local Verification**:
   - Never assert a challenge is solved or declare a flag valid without deterministic local execution proof.
   - Guessing, hallucinating, or blindly copying old writeups without local reproduction is strictly forbidden.

2. **Flag Candidate Lifecycle**:
   Every prospective flag must be tracked through three explicit states:
   - `CANDIDATE`: Extracted string matching flag regex (`FLAG{...}`), but not yet verified against local verifier or server.
   - `VERIFIED`: Proven by deterministic solver script (`solver/solve.py`) with clean exit code 0 and valid server response / hash check.
   - `REJECTED`: Proven to be dummy placeholder (e.g. `flag{test_placeholder}`, `flag{dummy}`) or rejected by user as a decoy.

3. **Workspace Discipline**:
   - `script/`: Probes, scratch scripts, decompiled snippets, test harnesses, logs, and `analysis.md`.
   - `solver/`: Clean, self-contained, reproducible final solver (`solve.py`).
   - `challenge/`: Read-only source files, binaries, attachments, and authoritative `NOTE.md`.

4. **Reporting Candidate Flags**:
   - When a candidate flag is discovered, report it along with:
     - Extraction source (file, memory offset, network packet).
     - Local verification command used to reproduce it.
     - Ready-to-run manual command for the user to review.
   - **NEVER invoke automated flag submission.** Flag submission is reserved exclusively for the user.
