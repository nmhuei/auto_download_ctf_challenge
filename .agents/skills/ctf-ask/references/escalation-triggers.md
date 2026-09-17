# Escalation Triggers

## Principle

Escalation is justified when a stronger reasoning pass is likely to reduce search or derive structure, **and** the answer can be independently checked. It is not a substitute for extracting the instance correctly.

## Decision tree

```text
A. Is the current blocker a self-contained formal subproblem?
   no  -> keep analyzing locally
   yes -> B

B. Can the relevant instance fit in TASK.md + sanitized text/JSON/CSV artifacts?
   no  -> reduce or factor the instance first
   yes -> C

C. Is a deterministic verifier available for the candidate?
   no  -> derive the verifier contract first
   yes -> D

D. Would local reasoning likely finish quickly and cheaply?
   yes -> solve locally
   no  -> E

E. Does preflight pass without weakening the rules?
   no  -> repair the abstraction
   yes -> escalate once, verify, then hand off
```

## Strong escalation signals

Escalate when one or more apply:

- High-dimensional modular/integer equations where structure matters more than brute force.
- Non-obvious lattice/cyclotomic reduction strategy or basis construction.
- Large SAT/SMT-style systems with interacting arithmetic and Boolean constraints.
- A recurrence/state-recovery problem with enough observations for algebraic reasoning but too many states for naive enumeration.
- A parser/grammar differential that can be reduced to a bounded formal language problem.
- A branch-heavy pure checker where the extracted semantics are clear but manual solving is error-prone.
- Multiple plausible transformations exist and you need a reasoned choice among them before local verification.

## Solve locally instead

Prefer local tools when:

- Gaussian elimination, modular inversion, direct substitution, or a standard library solves the instance immediately.
- The search space is tiny and exhaustive enumeration is cheaper than a model call.
- The task is mostly extraction/reversing rather than reasoning; finish extraction first.
- You already have a candidate and only need to verify it.
- The formalization is incomplete or still depends on hidden source behavior.

## Do not escalate

Do not send:

- Raw executables, archives, packet captures, memory dumps, or source trees.
- URLs, live targets, credentials, session tokens, or user data.
- Unverified claims about authorization.
- A task whose formal abstraction intentionally hides information required by a provider's policy or safety checks.
- Any instance that cannot be independently verified after the model returns.

## Retry policy

Default: one high-effort call. A second call is justified only if the first returns `unsolved`, times out transiently, or fails schema validation while the formal instance itself is unchanged and valid. Do not loop on speculative answers. If independent verification fails, inspect the abstraction and verifier before asking again.
