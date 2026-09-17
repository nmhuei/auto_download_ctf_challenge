# Solve Orchestration

## The common loop

1. Create `work/<event>/<challenge>/` and retain the supplied artifacts unchanged.
2. Establish a baseline: run the supplied program, parse every output, identify binary and text encodings, and collect deterministic samples where the service permits it.
3. Name the primitive and state exactly what should be secret, random, bounded, or authenticated.
4. Compare the implementation with the normal primitive. The intended opening is usually a parameter weakness, state reuse, a missing check, a side channel, a linear relation, or an algebraic degeneracy.
5. Write the smallest script that tests that opening. Keep the script's inputs and output deterministic whenever possible.
6. If the opening survives, derive the full equations and solve them. If it fails, record why and return to classification instead of escalating a guess.
7. Verify candidates against every original sample. For a remote oracle, predict at least one unused observation when safe and permitted.
8. Save `solve.py` or `solve.sage`, inputs, a command to reproduce, and a short `RESULT.md`. Do not submit a recovered flag automatically.

## Evidence that must survive each stage

| Stage | Required evidence |
| --- | --- |
| Classification | Primitive family and the observable facts supporting it |
| Weakness | A minimal calculation, counterexample, or source-level condition |
| Model | Variables, domains, equations, modulus/ring, bounds, and conventions |
| Solve | Deterministic script and its versioned input artifacts |
| Verification | Exact equations checked and their result for every sample |

## Technique selection

Prefer the lowest-cost proof of the weakness. Factor and check gcds before Coppersmith; check nonce reuse before lattice reduction; test algebraic cancellation before brute force; test whether data is merely encoded before treating it as cryptography.

When a route fails, diagnose the model before changing algorithms: endianness, sign convention, coefficient centering, wrong modulus, row/column orientation, missing samples, or a mistaken primitive are more common than a genuinely hard instance.

## Lattice and post-quantum branch

This branch applies to partial nonce leakage, noisy modular equations, truncated state, small/sparse secrets, subset sum, LWE, Ring-LWE, Module-LWE, or hidden-subspace instances.

1. **Singularity audit.** Identify the nonstandard feature first: zero/noisy samples, biased or repeated nonce, small coefficients, fixed Hamming weight, subfield collapse, or mismatched moduli.
2. **Separate variables.** Partition public values, ephemeral values, and target variables. Remove ephemeral variables by subtraction, division in the appropriate field, or a shared invariant where justified.
3. **Linearize and reduce dimension.** Convert nonlinear relations to a kernel, eliminate dependent coordinates, flatten polynomial rings to coefficient vectors, and use a metric matching the error distribution.
4. **Budget the reduction.** Use LLL first. Try scaling, centering and Babai/CVP before BKZ. If the reduced dimension is still too large for the available budget, return to the algebraic reduction rather than launching an unbounded BKZ job.
5. **Validate a vector.** Check every bound, norm, congruence, polynomial identity, and original sample. A short vector alone is not evidence of the target.
6. **Finish ambiguity.** Resolve signs, rotations, roots, carries, or a small remaining search only after the mathematical constraints narrow it to a bounded set.

Typical lattice failures are wrong scaling, coefficients left in `[0,q)` rather than centered form, transposed bases, too few equations, an error bound that is too large, or a problem that was actually ordinary linear algebra/CRT/encoding.

## When to ask Astra

Ask the expert only after local work has produced a complete mathematics-only system and one of these remains:

- a difficult Diophantine or polynomial system;
- an integer kernel, structured lattice, CVP/SVP, or parameterized BKZ design;
- a proof that an algebraic reduction is valid;
- a constrained optimization/SMT formulation whose correctness needs review.

Do not escalate for primitive identification, artifact parsing, source review, raw oracle interaction, flag recovery, or a routine implementation task. Those belong to the local solve loop.

## Result review after Astra

Treat `solution.json` and a solver script as hypotheses. Rebuild the candidate in the challenge workspace, then run independent checks against original values. If a check fails, translate the failed equation or residual into the existing math workspace, update the handoff, and resume the same Astra conversation only if the handoff digest is unchanged; otherwise start a new mathematical session.
