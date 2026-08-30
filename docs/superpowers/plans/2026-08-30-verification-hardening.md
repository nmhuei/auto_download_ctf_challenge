# Verification-driven hardening — 2026-08-30

## Goal

Verify and harden the real CTF lifecycle without resetting the current WIP:

1. platform detection/auth contracts;
2. challenge/file download integrity;
3. workspace persistence/incremental update;
4. flag submit/hoard/history safety;
5. watch/instance/scoreboard fault handling;
6. CLI/docs/completion consistency.

The rule for this effort is **test first for every bug found** and preserve existing
CTF-lab compatibility (private/loopback hosts remain valid).

## Implemented P0/P1

### HTTP/session contract

- Standard `requests.Session` is the default even when optional `curl_cffi`
  happens to be installed.
- Automatic urllib3 retries are restricted to idempotent
  `HEAD/GET/OPTIONS`; POST is never auto-retried.
- Browser TLS impersonation is explicit opt-in.

### Downloader integrity

- Real localhost fault-injection server covers normal, chunked, Range,
  If-Range/ETag, changed validator, and invalid Content-Range behavior.
- Partial downloads persist ETag/Last-Modified beside `.part` and send
  `If-Range` on resume.
- A mismatched resume Content-Range discards the partial instead of appending.
- Finalization uses `os.replace` on the same filesystem.
- Large-file segment downloads validate every `Content-Range`.
- Parallel-range failure re-enters the normal validated GET pipeline; it no
  longer uses an ad-hoc fallback response that bypasses status/content checks.
- Direct HTTP downloader rejects non-HTTP(S), missing-host, and embedded
  username/password URLs. Private/loopback is intentionally allowed.

### Submit safety

Typed verdicts now distinguish:

`correct`, `incorrect`, `already_solved`, `ratelimited`,
`auth_failed`, `event_not_started`, `event_paused`, `event_closed`,
`cheat_detected`, `challenge_not_found`, `unknown`.

- Transient/policy verdicts never become wrong-flag blacklist entries.
- `already_solved` promotes solve state but does not claim the submitted value
  was a proven correct flag.
- Per-challenge cross-process submit lock spans the final state re-check,
  network POST, and local commit.
- A multiprocessing regression test proves two racing processes produce exactly
  one network submit.

### Workspace/history safety

- Workspace URL fallback may scan metadata only when the root itself has a
  `challenges.json`; a parent directory containing multiple workspaces can no
  longer impersonate a child workspace.
- `history --prune` is exact-match only (no destructive substring deletion).
- `history --prune` and `--clear` are mutually exclusive.
- Bash/zsh completion + README + man page are synchronized.
- Downloaded CTF workspaces, local cookie dumps, and core files are ignored at
  repo root.

### ASIS contract coverage

Dedicated tests cover detection probe, auth, challenge mapping, attachments,
connection info, typed submit results/XSRF, and scoreboard parsing.

## Completed P1/P2 hardening

### Final-file verification and redirect policy

- `ctf pull --verify-downloads {fast,normal,strict}` is wired end-to-end:
  - `fast`: legacy presence-only skip;
  - `normal`: revalidate with persisted ETag/Last-Modified/size;
  - `strict`: normal verification + local SHA-256 against the persisted
    baseline.
- Successful downloads persist final metadata beside the payload and inside
  workspace download metadata, including source/final URL, validators, size,
  verification timestamp, and SHA-256 when strict mode is used.
- Initial private/loopback attachment URLs remain valid for intentional CTF lab
  topology.
- A redirect from a known-public origin to private/loopback is blocked by
  default and requires explicit `--allow-private-redirects`.
- Redirect chains are followed manually so every hop is validated before the
  request is sent. Cross-origin hops explicitly suppress Authorization/API-key
  style session headers while retaining transport-safe Range/validator headers.
- Service downloaders share the same final metadata/verification behavior;
  compatibility is preserved for older downloader/plugin probe signatures.

### Watch / scoreboard / instance resilience

- Scoreboard adapters expose conditional-response metadata while keeping the
  ordinary `ctf rank` result shape backward-compatible.
- Watch handles scoreboard 304 as healthy/no-change without erasing the
  previous snapshot; repeated no-change polls still drive adaptive idle timing.
- 429 respects Retry-After or the existing one-shot exponential rate-limit
  backoff; 5xx/transport failures use normal task backoff.
- 401/403 surfaces auth expiry without silently converting it into an empty
  scoreboard; a public scoreboard may continue while warning that personal rank
  is unavailable.
- Wall-clock scheduler/window tests cover suspend jumps and backward clock
  adjustments.
- CTFd Whale v1/legacy/generic container responses are normalized to one
  `entry/time_left/raw` contract; status now returns `unknown` for
  transport/auth/ambiguous failures instead of falsely reporting `stopped`.
- GZCTF instance operations reject missing game_id before constructing invalid
  URLs and distinguish auth/transport failures from a confirmed stopped
  instance.
- PID-reuse detection ignores interpreter path names and inspects command
  arguments, preventing a venv path containing the word `ctf` from being
  mistaken for a live watch process.

### CLI/doc drift prevention

- Bash and zsh completions include the new verification/redirect flags.
- `scripts/generate_cli_option_index.py` derives a canonical long-option index
  from `build_unified_parser()` and embeds it into README + man page.
- `test_cli_surface_consistency.py` fails if argparse, completions, or the
  generated README/man option index drift.
- Long atomic literals such as workspace paths are no longer character-wrapped
  by Rich after the renderer has already performed manual word wrapping, so
  paths remain copy/paste exact.

## Final verification evidence

A disposable verification venv was created outside the repository using
`requirements.txt` + `requirements-dev.txt`; the system Python was not
modified.

Functional gates:

- downloader verification/redirect regression group: **106 passed**;
- watch/container/keepalive regression group: **191 passed**;
- CLI surface consistency: **3 passed**;
- targeted seven failures discovered by randomized parallel execution:
  **7/7 passed** after fixes;
- full randomized + parallel suite:
  **1532 passed, 1 skipped, 62 subtests passed** with
  `pytest -n 4 --dist=loadfile --randomly-seed=20260830`;
- full randomized sequential suite:
  **1532 passed, 1 skipped, 62 subtests passed in 102.04s**.

Static/security gates, packaged in `scripts/verify_quality.py`:

- `compileall`: PASS;
- generated CLI docs freshness: PASS;
- Ruff correctness classes: PASS;
- mypy scoped reliability core: PASS, **0 issues in 6 source files**;
- Bandit high-severity scan: PASS;
- `pip-audit -r requirements.txt`: PASS, **no known vulnerabilities**;
- `git diff --check`: PASS.

The repository still contains historical whole-tree typing/style debt outside
the reliability-core mypy scope; this plan does **not** claim a whole-tree
strict-mypy cleanup. Watch/adapters outside that type scope are protected by
the dedicated contract/fault matrices above.

## Deep-debug follow-up — registration and every high-risk path

A second path-by-path audit was completed after the original hardening plan.

Upstream protocol verification used exact source snapshots:

- GZCTF develop: `77f26c3f234ff230859d48bca94f13a7ab671f67`;
- rCTF: `589fe9be98e4efd35d9e2149b8452b19ef4a1761`.

Confirmed/fixed findings:

- modern GZCTF API encryption is now implemented for account password and flag
  submit (X25519 → SHA-256 shared key → AES-GCM wire frame);
- HashPow hashes the upstream `challenge` bytes, not ticket `id`; answer is
  always 8 bytes / 16 hex even at difficulty zero;
- GZCTF config accepts current camelCase and legacy PascalCase, gets a bounded
  cache TTL, uses a fresh captcha ticket per protected action, and handles
  register status/email/admin confirmation explicitly;
- rCTF v2 auto-registration, public runtime config, captcha gate, rate-limit,
  email verification, auth token and team-token persistence are implemented;
- CTFd registration supports hidden nonce inputs and fails closed on captcha or
  unknown required form fields;
- automatic registration reserves the one-account attempt durably before any
  platform register POST, preventing duplicate side effects under concurrency;
- tempmail scanning ignores unrelated messages, supports CTFd/GZCTF/rCTF
  verification links, respects explicit 429 Retry-After once, and does not
  blind-retry ambiguous POST network failures;
- sniper no longer consumes attempts on 429/deferred event verdicts and handles
  typed terminal/transient verdicts correctly;
- one-shot rank surfaces normalized HTTP/transport failures instead of showing
  a false empty-scoreboard success;
- sync partial persistence/corrupt-local failures return `ok=False`;
- Git finish is idempotent after a partial remote-delete failure and refuses to
  delete diverged work or resurrect an already-merged event branch;
- doctor rejects ended/empty/inverted event windows and can use a validated
  workspace flag-format baseline when rules are unavailable;
- shared HTTP auth preserves explicit schemes, uses Token only for `ctfd_*`,
  and treats other opaque tokens as Bearer;
- GZCTF successful-but-empty solve attribution now returns `net_clean`, so an
  empty successful snapshot is TTL-cached instead of polling continuously;
- randomized execution exposed and fixed a test descriptor leak that restored a
  `staticmethod` as a normal bound function;
- multiprocessing lock/race tests use `spawn` under Python 3.13 so xdist no
  longer emits unsafe fork-from-thread warnings;
- storage ZIP export symlink containment was re-audited and confirmed already
  protected by lstat/containment/O_NOFOLLOW/inode checks and dedicated tests.

Additional release verification:

- real localhost integration servers exercise current GZCTF encryption +
  HashPow + cookie + encrypted flag submit, rCTF v2 register/captcha/rate-limit,
  CTFd hidden nonce/captcha/required-field gates, verification-email paths, and
  concurrent registration reservation;
- a real wheel was built and installed into a fresh venv; ASIS/GZCTF/rCTF,
  Git workflow, GZ crypto module and console entry point all import/run;
- wheel metadata contains `cryptography>=41.0.0`.

Final snapshot evidence:

- full randomized + xdist (`-n 4 --dist=loadfile --randomly-seed=20260830`):
  **1594 passed, 1 skipped, 65 subtests, zero warnings**;
- full randomized sequential:
  **1594 passed, 1 skipped, 65 subtests in 118.31s**;
- deep cross-path regression batch:
  **359 passed, 21 subtests**;
- registration regression + real-local-HTTP deep integration:
  all targeted suites green;
- quality gate mypy scope expanded from 6 to **16 reliability source files**,
  with **0 issues**;
- real wheel build + fresh-venv install/import/CLI smoke: PASS.

## Cloudflare + packaging reproducibility follow-up

A final transport/release audit covered Cloudflare-proxied CTF servers and real
wheel installation rather than source-checkout imports only.

Implemented/fixed:

- shared `requests.Session` upgraded adaptively after proven Cloudflare signals;
- official `cf-mitigated: challenge` is the primary Challenge Page detector,
  with conservative Cloudflare HTML markers only as fallback;
- `curl_cffi` browser impersonation is a declared runtime dependency and is
  activated lazily; normal sites still use the requests/urllib3 retry policy;
- idempotent GET/HEAD/OPTIONS may replay once after an initial CF challenge;
  before the first POST/PUT/PATCH/DELETE to each origin, a bounded HEAD
  preflight can activate browser transport/clearance before any side effect;
  a confirmed challenge that survives preflight raises
  `CloudflareChallengeError` before mutation, while a challenge that appears
  only on the mutation is surfaced without replaying that mutation;
- cookies are bridged between transports, including `cf_clearance`, and
  auto-register persists clearance together with the platform session cookie;
- `ctf register --cf-clearance` provides an explicit recovery path for Managed
  Challenge/Turnstile flows that require a browser-obtained clearance cookie;
- doctor surfaces a Cloudflare Challenge diagnosis/action instead of a generic
  HTTP failure;
- thread-local download sessions inherit Cloudflare-active state from the
  master session, while large-file segment fan-out is disabled under the
  browser backend to avoid sharing one curl session across segment threads;
- credential scoping is origin-aware: CLI/auth-map cookies are bound to the
  platform host (`localhost.local` compatibility included), inherited
  Authorization/API-key headers are removed outside the platform origin, and
  the browser backend uses isolated cached curl sessions for foreign origins;
  this closes the HTTP-cookie port gap where `127.0.0.1:8000` cookies would
  otherwise be sent to `127.0.0.1:9000`. Both requests and force-browser paths
  are covered by live two-server regressions;
- cross-origin download redirects and direct first-hop third-party URLs both
  suppress platform credentials; explicit per-request credentials remain an
  intentional caller override;
- `use_browser_impersonation=True` now activates the same adaptive policy
  wrapper instead of returning a raw curl session that bypassed origin and
  mutation safeguards;
- binary attachment streaming no longer assumes a requests response context
  manager; `contextlib.closing` supports both requests and curl_cffi, and a
  backend-aware iterator avoids curl_cffi's ignored-`chunk_size` warning;
- curl_cffi transport exceptions are normalized to `requests.RequestException`
  so existing downloader/watch retry contracts remain valid;
- a full CTFd detector integration starts with an actual local 403 Challenge
  response, replays through browser fingerprint, receives `cf_clearance`, and
  still resolves CTFd with high confidence;
- a clean wheel install exposed a stale `build/lib` zero-byte module packaging
  failure. `ForceBuildPy` now ignores stale timestamps and recopies source;
- `scripts/verify_wheel_contents.py` builds a wheel and verifies all packaged
  Python module bytes exactly match source; `verify_quality.py` now runs it;
- dev requirements explicitly include `build`, `setuptools`, and `wheel` so the
  wheel-integrity gate is offline/no-isolation reproducible.

Release evidence on the final Cloudflare snapshot:

- focused Cloudflare/origin/streaming suites: latest curl_cffi green; exact
  minimum `curl_cffi==0.7.4` also passes mutation preflight, no-replay,
  binary streaming, and same-host/different-port credential isolation;
- full randomized xdist (`-n 4 --dist=loadfile --randomly-seed=20260830`):
  **1628 passed, 1 skipped, 65 subtests**;
- sequential coverage over all 85 top-level test files: **1628 passed,
  1 skipped, 65 subtests** total (groups: 376 + 247 + 273 + 246 + 486);
- wheel source-integrity gate: **76 Python modules byte-identical to source**;
- definitive wheel clean-install from outside the source tree: PASS. Installed
  package localhost smoke observes two safe HEAD probes, then exactly one POST,
  persists the returned `cf_clearance`, and a force-browser request to the same
  host on a different port arrives with no Cookie, Authorization, or X-API-Key;
  `ctf register --help` exposes `--cf-clearance`;
- quality/security gate: compileall, generated docs, wheel integrity, Ruff,
  scoped mypy expanded to **22 reliability files with 0 issues**, Bandit high
  severity, and pip-audit: PASS;
- `git diff --check`: PASS.

## Status

**Verification-hardening + deep-debug + Cloudflare/release follow-up complete.**
No known finding from these audits is intentionally left unfixed.
