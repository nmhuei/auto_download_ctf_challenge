# Handoff Contracts

## 1. Sanitized workspace

The workspace is input-only and normally contains:

```text
formal_workspace/
├── TASK.md
└── instance.json
```

Additional UTF-8 `.json`, `.txt`, `.csv`, `.tsv`, `.md`, or `.bnf` data files are allowed when needed. Symlinks, hidden files, executables, archives, URLs, and source-tree copies are rejected by preflight.

`handoff.json` must be written **outside** this workspace.

## 2. TASK.md

Use this template:

```markdown
# Formal Task

Context: Authorized educational formal problem.

## Problem class
<one of: modular_linear, integer_linear, xor_system, boolean_cnf, negacyclic_product, finite_state_trace>

## Definitions
<domains, dimensions, modulus, bit widths, ordering conventions>

## Given
<what each artifact contains; use neutral filenames>

## Goal
<exact candidate object to return>

## Verification condition
<plain-language statement of the invariant checked by instance.json>

## Constraints and edge cases
<bounds, uniqueness, wraparound, normalization, tie-breaking>
```

Do not include source-specific identifiers that are irrelevant to the formal problem.

## 3. instance.json verifier forms

### modular_linear

Checks `A * x == b (mod modulus)`.

```json
{
  "type": "modular_linear",
  "modulus": 257,
  "A": [[1, 2], [3, 4]],
  "b": [5, 11]
}
```

Candidate uses `candidate.integers` as `x`.

### integer_linear

Checks exact integer equality `A * x == b`.

```json
{
  "type": "integer_linear",
  "A": [[2, 1], [1, -1]],
  "b": [7, 2]
}
```

### xor_system

Each row XORs selected Boolean variables and compares with `rhs`.

```json
{
  "type": "xor_system",
  "num_vars": 4,
  "rows": [
    {"vars": [0, 2], "rhs": 1},
    {"vars": [1, 2, 3], "rhs": 0}
  ]
}
```

Candidate may use `candidate.booleans` or 0/1 values in `candidate.integers`.

### boolean_cnf

Clauses use DIMACS-style signed 1-based literals.

```json
{
  "type": "boolean_cnf",
  "num_vars": 3,
  "clauses": [[1, -2], [2, 3], [-1, -3]]
}
```

Candidate uses `candidate.booleans` in variable order 1..N.

### negacyclic_product

Checks `negacyclic_mul(a, x) == b (mod modulus)` in `Z_q[x]/(x^n + 1)`.

```json
{
  "type": "negacyclic_product",
  "n": 4,
  "modulus": 17,
  "a": [1, 2, 3, 4],
  "b": [5, 6, 7, 8]
}
```

Candidate uses `candidate.integers` as the coefficients of `x` in ascending degree order.

### finite_state_trace

Checks a deterministic or nondeterministic labeled path from `start` into one of `accepting` states.

```json
{
  "type": "finite_state_trace",
  "start": "s0",
  "accepting": ["s2"],
  "transitions": [
    {"from": "s0", "symbol": "a", "to": "s1"},
    {"from": "s1", "symbol": "b", "to": "s2"}
  ]
}
```

Candidate uses `candidate.states` including the start state and `candidate.symbols` for edge labels.

## 4. Astra final-result schema

The runner constrains the final model message to this shape:

```json
{
  "status": "solved",
  "problem_type": "modular_linear",
  "candidate": {
    "integers": [1, 2],
    "booleans": [],
    "states": [],
    "symbols": []
  },
  "derivation_summary": ["short checkable step"],
  "assumptions": []
}
```

`status` is `solved` or `unsolved`. Unused candidate arrays are empty. The model's prose is advisory; only deterministic verification authorizes handoff.

## 5. handoff.json

On successful verification, the runner writes:

```json
{
  "schema_version": 1,
  "verified": true,
  "model": "gpt-6-astra",
  "reasoning_effort": "high",
  "workspace_digest": "sha256:...",
  "artifacts": {"TASK.md": "sha256:...", "instance.json": "sha256:..."},
  "solution": {"...": "Astra final result"},
  "verification": {
    "ok": true,
    "type": "modular_linear",
    "checks": ["2/2 equations satisfied"]
  }
}
```

The caller must map the verified candidate back to the original challenge itself. A verified formal solution proves only the sanitized invariant, so correctness of the extraction/mapping remains the caller's responsibility.
