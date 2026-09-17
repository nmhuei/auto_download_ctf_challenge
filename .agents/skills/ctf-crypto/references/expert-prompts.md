# Astra Expert Session

## Invocation contract

The primary solver runs through `agy` with its default model. `scripts/run_astra_expert.py` validates `handoff.json`, then invokes one persistent Codex CLI session for the math workspace using:

```bash
codex exec -m gpt-6-astra -c 'model_reasoning_effort="high"' \
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --json
```

It stores the returned thread ID in `.ctf-crypto-codex-astra-session.json`. A matching handoff digest resumes that thread. A changed mathematical model creates a new session so the expert does not reason from stale constraints.

## `TASK.md` template

```markdown
# Numerical Algebra Task

Read `handoff.json` and only the numerical artifacts it names.

You are an expert computational number theorist. Solve the stated finite-dimensional mathematical system. Work only inside this directory.

Required deliverables:

1. `reproduce.sage` or `reproduce.py`, which loads the provided artifacts and derives the candidate deterministically.
2. `solution.json`, containing every unknown in the schema's stated order and representation.
3. `verification.md`, showing each constraint, bound, and residual check.

Use the cheapest exact method that fits the stated system. For a lattice instance, justify the basis, scaling, reduction method, and candidate extraction. Do not use facts outside the provided mathematical artifacts. Do not inspect parent directories or request non-mathematical context.

Before finishing, execute the reproduction script and confirm the generated candidate satisfies all constraints from `handoff.json`.
```

## Complete example

`handoff.json` contains a modular system with a bounded vector. `matrix_A.json` holds an integer matrix, `vector_b.json` holds the target vector, and `TASK.md` is the template above. The local solver validates the handoff, launches Astra, then verifies `solution.json` by recomputing every congruence. If one equation has residual `17 mod q`, it appends the corrected algebraic constraint to a new handoff and starts a new math session.

## Reviewing an answer

Check for all of the following locally:

- every named artifact is loaded with the stated dimension and ordering;
- every congruence/polynomial identity is exact;
- every range, sparsity, norm, and integrality bound holds;
- any decoded result derives only after those mathematical checks pass;
- the reproduction script runs from a clean copy of `math_workspace`.

An Astra explanation without those artifacts is not a solution. A candidate passing only a subset of equations is a lead, not a result.
