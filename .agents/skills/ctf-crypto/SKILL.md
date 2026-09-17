---
name: ctf-crypto
description: "Use when solving a CTF cryptography challenge involving ciphers, RSA, ECC, PRNG, lattices, LWE, hashes, signatures, ZKP, or unusual algebraic systems. Routes the analysis, creates a reproducible solver, and hands only a mathematics-only model to a persistent Astra expert session when needed."
---

# CTF Crypto

Solve the challenge locally first, turn observations into a falsifiable model, and verify every candidate against the original data. Treat a CTF cryptography challenge as an engineering problem with a mathematical core, not as a technique lookup exercise.

## Non-negotiable rules

- Never run `ctf submit`, `ctf submit --auto`, or `ctf hoard --all`. Recovering and recording a candidate flag is allowed; flag submission always remains a separate human action.
- Keep the challenge workspace separate from `math_workspace`. Raw source, binaries, network captures, cookies, tokens, URLs, flags, writeups, and challenge identifiers stay outside `math_workspace`.
- An Astra expert receives **only** a complete mathematical handoff. Do not ask the expert to inspect, classify, reverse, decrypt, exploit, or solve a CTF challenge.
- The local solver must independently verify every returned candidate against the original challenge equations or oracle observations before treating it as solved.
- Use one Astra conversation per unchanged mathematical handoff. Resume it for corrections; regenerate the handoff and start a new conversation only when the mathematical model changes.

## Workflow

1. **Preserve evidence.** Copy challenge files into a per-challenge workspace, identify file formats, record commands and oracle samples, and reproduce the generator or verifier locally when possible.
2. **Classify before attacking.** Read [attack-router.md](references/attack-router.md) and choose a branch from observable structure. Run the branch's cheap discriminating checks before expensive attacks.
3. **Audit the construction.** Identify the intended primitive, compare parameters and implementation behavior with the standard construction, and find the deliberate weakness: reused state, small value, leakage, malformed validation, linearity, or structural relation.
4. **Model and solve locally.** Build equations, keep units and modular conventions explicit, and write a reproducible script. For lattice problems, read [lattice workflow](references/orchestration.md#lattice-and-post-quantum-branch) before running LLL or BKZ.
5. **Escalate only as mathematics.** When local reduction leaves a hard integer, polynomial, lattice, CVP/SVP, algebraic, or constraint system, build the handoff defined in [mathematical-handoff.md](references/mathematical-handoff.md). Run its validator before calling the expert.
6. **Use the specialist.** The primary solver is `agy` at its default model. Run `scripts/run_astra_expert.py math_workspace` only after the mathematical handoff is ready; it calls Codex CLI with `gpt-6-astra` and high reasoning, then stores its thread in that workspace. The exact prompt and a full example are in [expert-prompts.md](references/expert-prompts.md).
7. **Verify and record.** Re-run the local verifier on all original observations, check plaintext/file-format/flag-format only after mathematical verification, save the script and evidence, then report the candidate without submitting it.

## Reference routing

- Read [attack-router.md](references/attack-router.md) for recognition signals and the first inexpensive tests across classic, symmetric, RSA, ECC, PRNG, lattice, ZKP, and exotic constructions.
- Read [orchestration.md](references/orchestration.md) for the complete solve loop, evidence requirements, failure recovery, and the six-stage lattice branch.
- Read [mathematical-handoff.md](references/mathematical-handoff.md) before involving Astra. It defines the allowed artifact schema and conversion examples.
- Read [expert-prompts.md](references/expert-prompts.md) when creating `TASK.md`, reviewing an expert result, or resuming an expert session.

## Completion standard

Do not conclude from a plausible string, a partially matching vector, or an expert assertion. Completion requires a reproducible local script, a candidate that satisfies every applicable original constraint, and a concise record of the model, technique, verification command, and output.
