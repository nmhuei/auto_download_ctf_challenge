---
name: ctf-ask
description: Use when an authorized puzzle or CTF task is blocked on a hard mathematical, cryptographic, reverse-engineering, parser, SAT/SMT, lattice, or algorithmic subproblem that can be expressed as a self-contained formal problem and independently verified.
---

# ctf-ask

## Purpose

Use this skill to separate a difficult reasoning subproblem from the surrounding challenge, express it as a minimal formal instance, ask a stronger reasoning model for a candidate solution, and accept the result only after deterministic local verification.

The abstraction step exists to improve reasoning quality, reduce irrelevant context, and make verification possible. **Never use abstraction to disguise intent, bypass safeguards, or omit authorization/context that a provider requires.** If the task is not an authorized puzzle, benchmark, lab, or other permitted setting, stop and handle it under the applicable rules instead of sanitizing it.

## Mandatory pipeline

1. **Classify the roadblock.** Read `references/escalation-triggers.md`. Solve locally when the subproblem is routine or cheaper to verify directly.
2. **Abstract without changing semantics.** Read `references/de-cyberization-guide.md`. Preserve every equation, width, modulus, ordering rule, state transition, grammar rule, and bound that affects the answer.
3. **Create a fresh isolated workspace.** Use `math_workspace/` or `formal_workspace/`. It must contain `TASK.md` and only the sanitized data artifacts required by the formal problem. Do not copy executables, URLs, credentials, challenge identifiers, solution tokens, or unrelated repository files.
4. **Make the problem verifiable.** Prefer `instance.json` in one of the supported verifier forms documented in `references/handoff-schema.md`. If no deterministic verifier contract can be expressed, do not use Astra as an oracle; either derive a verifier first or continue locally.
5. **Preflight the workspace.** Run:
   `ctf ask --workspace <workspace> --preflight-only`
   *(Or direct script: `python ~/.gemini/config/skills/ctf-ask/scripts/validate_sanitized_handoff.py preflight --workspace <workspace>`)*
   Continue only on exit code `0` and JSON field `ok: true`.
6. **Escalate once, read-only.** Run the runner as a black box:
   `ctf ask --workspace <workspace> --output <outside-workspace>/handoff.json`
   *(Or direct script: `python ~/.gemini/config/skills/ctf-ask/scripts/run_astra_expert.py --workspace <workspace> --output <outside-workspace>/handoff.json`)*
   The default is `gpt-6-astra` with reasoning effort `high`; both are configurable. The runner invokes Codex in an ephemeral, read-only, no-web-search session with user/project rules ignored.
7. **Require independent verification.** The runner calls the validator in `verify` mode as a separate process. A candidate is rejected unless the supplied values satisfy the original sanitized instance exactly.
8. **Re-integrate only proven values.** Read `handoff.json`. Use only `solution.candidate` plus the verified invariants. Re-derive how those values map back into the original challenge. Do not copy Astra prose into an operational step without checking the mapping yourself.

## Decision rule

```text
Hard subproblem?
  no  -> solve locally
  yes -> can preserve semantics in a formal instance?
           no  -> continue local analysis
           yes -> deterministic verifier supported?
                    no  -> build verifier contract first
                    yes -> preflight passes?
                             no  -> fix abstraction, do not weaken linter
                             yes -> Astra -> independent verify -> handoff
```

## Non-negotiable rules

- Fail closed. A failed linter, timeout, malformed JSON, unsupported verifier type, or failed invariant means **no handoff**.
- The sanitized workspace is input-only. `handoff.json`, logs, and temporary schemas must be written outside it.
- Do not ask Astra to inspect parent directories, the original repository, network resources, or raw executable files.
- Do not weaken the sanitizer merely to make a task pass. Rewrite the abstraction correctly.
- Prefer exact integers, rationals, modular arithmetic, bit-widths, explicit state machines, and declarative constraints over prose.
- Treat model output as an untrusted candidate, not a proof.

## References

- `references/de-cyberization-guide.md` — semantic abstraction patterns and losslessness checklist.
- `references/escalation-triggers.md` — when to escalate and when not to.
- `references/handoff-schema.md` — `TASK.md`, `instance.json`, Astra result, and `handoff.json` contracts.

## Script usage

Run each script with `--help` before use. Prefer invoking the scripts rather than reading their source into context unless debugging the skill itself.
