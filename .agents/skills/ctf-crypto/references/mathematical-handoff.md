# Mathematics-Only Handoff

The handoff is a boundary between the local CTF workflow and an expert numerical-algebra workflow. Build it only after the local solver has identified the primitive, derived the equations, and proved that the supplied artifacts are sufficient for the mathematical task.

## Required workspace

```text
math_workspace/
├── handoff.json
├── TASK.md
├── matrix_A.json          # Example numerical artifact
├── vector_b.json          # Example numerical artifact
└── .ctf-crypto-codex-astra-session.json  # Created by the runner
```

Only place derived arrays, matrices, polynomials, integer bounds, and verification data in this directory. Do not copy source code, binaries, packet captures, raw service responses, web addresses, credentials, flags, or writeups.

## Required `handoff.json` schema

```json
{
  "schema_version": 1,
  "objective": "Find an integer vector x satisfying A*x = b modulo q.",
  "domain": "integer linear algebra",
  "variables": [
    {"name": "x", "domain": "Z^n", "bounds": "|x_i| <= 2"}
  ],
  "constraints": [
    "A*x = b (mod q)",
    "||x||_2^2 = 64"
  ],
  "artifacts": ["matrix_A.json", "vector_b.json"],
  "verification": ["Substitute x into A*x = b (mod q)."],
  "requested_output": ["solution.json", "reproduce.sage"]
}
```

Every field is mandatory. State dimensions, modulus, coefficient ordering, ring relation, bounds, and any normalization in the objective or constraints. An expert must be able to solve and verify the system without asking what challenge produced it.

## Conversion examples

### Partial-signature leakage to HNP

Local analysis proves equations of the form `r_i*d - s_i*delta_i = t_i (mod q)` with `|delta_i| < 2^k`. The handoff becomes:

```json
{
  "schema_version": 1,
  "objective": "Recover d and bounded corrections delta_i from modular linear equations.",
  "domain": "integer lattice reduction",
  "variables": [
    {"name": "d", "domain": "Z/qZ", "bounds": "0 <= d < q"},
    {"name": "delta", "domain": "Z^m", "bounds": "|delta_i| < 2^k"}
  ],
  "constraints": ["r_i*d - s_i*delta_i = t_i (mod q) for i=0..m-1"],
  "artifacts": ["coefficients.json"],
  "verification": ["Check every congruence and each correction bound."],
  "requested_output": ["solution.json", "reproduce.sage"]
}
```

### Ring equation to a kernel and CVP task

After local derivation, a polynomial relation has been flattened into an integer kernel `K` and a weighted metric `G`. The handoff states the ring and ordering explicitly:

```json
{
  "schema_version": 1,
  "objective": "Find a bounded integer vector z in the row lattice of K minimizing z*G*z^T.",
  "domain": "lattice reduction over integers",
  "variables": [{"name": "z", "domain": "Z^d", "bounds": "z has the stated coefficient bounds"}],
  "constraints": ["z is in row_lattice(K)", "z*G*z^T <= 2", "coefficient ordering is ascending degree"],
  "artifacts": ["kernel_K.json", "metric_G.json"],
  "verification": ["Check lattice membership, metric norm, and all coefficient bounds."],
  "requested_output": ["solution.json", "reproduce.sage"]
}
```

## Validation and launch

From the skill directory:

```bash
python scripts/validate_handoff.py /absolute/path/to/math_workspace/handoff.json
python scripts/run_astra_expert.py /absolute/path/to/math_workspace
```

The validator rejects absent fields, unsafe artifact paths/extensions, and CTF/security/secret vocabulary. This is an intentional gate: revise the mathematical model instead of weakening it.

## Before and after the expert

Before the call, make a local verification script capable of consuming `solution.json`. After the call, run that script against the original challenge data. If it fails, turn the residual into a new mathematical constraint. Do not copy the original challenge material into the existing workspace to “help” the expert.
