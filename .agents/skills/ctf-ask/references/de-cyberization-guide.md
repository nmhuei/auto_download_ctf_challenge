# Formal Abstraction Guide

## Goal

Translate a challenge-specific roadblock into the smallest **lossless formal problem** that preserves the mathematics or program semantics needed to solve it. The purpose is better reasoning and deterministic verification, not concealment from safeguards.

A good abstraction removes names and operational context while preserving invariants. A bad abstraction removes details that change the solution set.

## Abstraction dictionary

| Original roadblock shape | Formal representation | Preserve exactly |
|---|---|---|
| Algebra over a cipher or ring construction | Finite-field/ring equations | modulus, polynomial modulus, coefficient order, endianness, dimensions |
| Hidden linear relation | Integer or modular linear system | signedness, modulus, bounds, rank-relevant rows |
| XOR-heavy transform | GF(2) / XOR equation system | bit indexing, width, constants, variable order |
| Branch-heavy checker | SAT/SMT-style Boolean/arithmetic constraints | branch predicates, widths, overflow semantics, comparisons |
| Repeated state update / generator | Recurrence or finite-state transition system | initial state constraints, update order, truncation, output projection |
| Reverse-engineered validation logic | Pure function / constraint system | constants, operation order, casts, wraparound, lookup tables |
| Parser disagreement | Grammar/transducer differential | tokenization, normalization, precedence, acceptance conditions |
| Memory-layout puzzle | Indexed state transition model | object sizes, offsets, allocation/free order, aliasing relationships; omit operational delivery details |
| Search/optimization puzzle | Objective + constraints | exact objective, bounds, tie-breaking, admissibility conditions |
| Cyclotomic/lattice subproblem | Vector/ring instance | basis/order convention, norm, modulus, dimension, embedding convention |

## Naming rules

Use neutral symbols: `x`, `y`, `state`, `row`, `token`, `transition`, `candidate`, `constraint_17`. Avoid source-specific names that carry no mathematical meaning.

Recommended filenames:

```text
TASK.md
instance.json
matrix.csv
constraints.json
grammar.bnf
trace.txt
```

Do not place original executable names, hostnames, URLs, credentials, challenge names, or answer-token formats in the formal workspace.

## Losslessness checklist

Before preflight, verify all of the following:

1. **Domain:** integer, rational, Boolean, bit-vector, finite field/ring, or string alphabet is explicit.
2. **Width:** every fixed-width value states its width and signed/unsigned interpretation.
3. **Arithmetic:** overflow/wraparound behavior and modulus are explicit.
4. **Order:** byte order, coefficient order, row/column order, and iteration order are explicit.
5. **Bounds:** variable ranges and uniqueness/distinctness constraints are explicit.
6. **State:** initial state, transition order, and terminal condition are explicit.
7. **Projection:** if only part of a state is observed, define the exact projection.
8. **Normalization:** parser/string normalization, case folding, whitespace, and encoding are explicit where relevant.
9. **Objective:** optimization tasks define minimize/maximize and tie-breaking.
10. **Verifier:** the proposed candidate can be checked from the sanitized artifacts alone.

## Example: branch logic to constraints

Source logic conceptually performs fixed-width checks. The sanitized version should describe only the formal semantics, for example:

```text
Variables: x0..x7 are unsigned 8-bit integers.
Constraints:
- (x0 + x3) mod 256 = 191
- x1 XOR x4 = 0x2d
- x2 < x7
- all xi are in [32, 126]
Goal: return one satisfying vector [x0..x7].
```

The important property is not that the prose sounds academic; it is that every solution of the sanitized system corresponds to a solution of the extracted checker relation, and vice versa for the variables being handed off.

## Example: ring relation

For `R_q = Z_q[x]/(x^n + 1)`, define arrays in coefficient order `[a0, a1, ..., a(n-1)]` and state the target relation explicitly:

```text
Find x such that negacyclic_mul(a, x) = b (mod q).
```

The verifier must recompute the negacyclic product locally. Do not accept a vector merely because a model says it is short or plausible.

## What not to abstract away

Never remove a constant because it “looks challenge-specific” if it participates in an equation. Never rename away signedness, endian conventions, or bit widths. Never replace a stateful process with independent equations unless you have proved the equivalence. Never summarize a lookup table when exact entries determine satisfiability.
