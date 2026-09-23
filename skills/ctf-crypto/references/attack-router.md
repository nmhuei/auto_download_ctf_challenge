# Crypto Attack Router

Use the observed structure to select a branch. The listed checks are ordered so cheap falsification precedes costly solving.

| Observable structure | First checks | Likely mathematical route |
| --- | --- | --- |
| Alphabet shifts, substitution, repeated groups, printable XOR output | encoding, frequency, known prefix, key-period tests | modular shifts, substitution constraints, XOR linear equations |
| Repeated blocks, IV/nonce handling, padding/timing/error distinction | block size, ECB repetition, nonce reuse, oracle consistency | block algebra, chosen-input differential, padding or timing inequalities |
| Hash/MAC/CRC or custom XOR-and-rotate digest | Merkle-Damgard state exposure, length extension, GF(2) linearity, collision space | state continuation, GF(2) Gaussian elimination, meet-in-the-middle |
| RSA values `n,e,c`, signatures, related moduli | bit sizes, exact roots, gcds, factor relations, shared primes, `gcd(e,phi)` | CRT, continued fractions, factoring, polynomial gcd, Coppersmith, interval-oracle math |
| ECC, DSA, ECDSA, DH, ElGamal | curve discriminant/order, subgroup factors, repeated `r`, invalid points, smooth order | modular linear equations, Pohlig-Hellman, BSGS, p-adic/singular reductions |
| Consecutive random outputs or state-derived tokens | generator family, time seed, truncation, output count, period | recurrence solving, GF(2) state recovery, Z3 bit-vectors, HNP/lattice |
| Linear equations modulo `q` with small noise or sparse values | what is small, bounds, centering, sample count, polynomial structure | LLL/BKZ/Babai, CVP embedding, kernel/orthogonal lattice, subset-sum lattice |
| Proof verifier, circuit, graph/color constraints, secret-sharing | unconstrained inputs, reused randomness, verifier constants, boolean vs integer domain | SMT/CP-SAT, interpolation, linear algebra over finite fields, pairing identities |
| Matrices, groups, semirings, homomorphic schemes, unusual algebra | operation law, identity/inverse, commutation, order, oracle homomorphism | invariant extraction, CRT, matrix normal form, polynomial/ring algebra, adaptive inequality recovery |
| Multiple layers or output encrypted after a math stage | solve dependency order and preserve intermediate values | one verified reduction per layer; no final decoding until the prior layer checks |

## Branch notes

### Classic and XOR

Check file magic bytes, known flag prefixes, periodicity, and whether operations are reversible per position. Model XOR as equations over GF(2); do not brute force a multi-byte key before scoring a key-length hypothesis.

### Symmetric modes and protocols

Record exact query/response pairs. Distinguish confidentiality, integrity, and format validation. Reused keystreams, predictable IVs, unauthenticated CBC bit flips, padding distinctions, and linear MACs reduce to bytewise XOR or a small oracle inequality; prove the condition with controlled samples before scaling queries.

### RSA and related public-key systems

Run root, gcd, factor-size, modulus-sharing, and parameter-relationship checks before general factoring. Translate only the resulting equations to the specialist: for example, a monic polynomial with a stated small-root bound, never raw challenge code or ciphertext context.

### ECC, signatures, and discrete logs

Check curve/order validity before discrete-log work. Repeated nonces yield direct modular linear equations; partial/biased nonces usually yield HNP. Keep scalar convention, hash reduction, and point encoding explicit in the local verifier.

### PRNG

Identify the generator from source or output behavior, then establish whether observations are exact, truncated, biased, or time-seeded. Use recurrences or bit-vector constraints first. Convert a truncated-state problem to bounded modular equations only after its state layout is proven.

### Lattice, LWE, and post-quantum constructions

Read the lattice branch in [orchestration.md](orchestration.md#lattice-and-post-quantum-branch). In particular: find the small quantity, center residues, test both basis orientations, flatten ring samples when appropriate, and verify every candidate in the original ring.

### ZKP, constraints, and secret sharing

Inspect what the verifier fails to bind: public inputs, nullifiers, randomness, subgroup membership, or degree. Pick exact domains—Boolean/bit-vector, integer, or finite field—before encoding a solver. Validate a model by replaying the verifier locally.

### Exotic algebraic structures

Start with the operation rather than the name: determine whether it is associative, commutative, linear, homomorphic, matrix-based, polynomial-based, or a finite group. Search for invariants, commuting elements, small/smooth order, CRT decomposition, and an oracle that leaks an inequality or one bit. Reduce the discovered relation to finite-field, integer, polynomial, or matrix equations.
