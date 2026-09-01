---
name: ctf-toolkit
description: Use when interacting with CTF competition platforms (CTFd, GZCTF, rCTF), downloading challenges, managing dynamic instance containers, submitting or hoarding flags, diagnosing platform health, bypassing Cloudflare Turnstile via browser bridge, or managing competition workspaces with the `ctf` CLI.
argument-hint: "[pull|status|instance|submit|hoard|doctor|bridge|watch|rank|git] [args...]"
---

# CTF Toolkit (`ctf` CLI)

Unified CLI toolkit for competitive Capture The Flag operations. Handles challenge downloading, dynamic container management, safe flag submission, real-time scoreboard tracking, and Cloudflare Turnstile bypass for CTFd, GZCTF, and rCTF platforms.

## When to Use

- Downloading challenges and file attachments from a CTF event
- Registering accounts or storing credentials in the persistent auth map
- Managing dynamic challenge instances / Docker containers (start, renew, stop)
- Submitting flags safely (with rate-limit protection and wrong-flag lock) or hoarding flags locally
- Diagnosing platform health, event windows, and local runtime dependencies (`ctf doctor`)
- Bypassing Cloudflare Turnstile / Managed Challenges via the WebSocket browser bridge
- Synchronizing multi-challenge Git branches per CTF competition

## Quick Command Reference

| Action | Canonical Command | Key Options |
|---|---|---|
| **Register** | `ctf register -u <URL> --tempmail` | `--cf-clearance <VAL>`, `--email <EMAIL>`, `--password <PASS>` |
| **Doctor** | `ctf doctor -u <URL>` · `ctf doctor --runtime` | `-c <COOKIE>`, `-t <TOKEN>`, `-w <WORKSPACE>` |
| **Pull** | `ctf pull -u <URL> -o <DIR>` | `-c <COOKIE>`, `-t <TOKEN>`, `--verify-downloads strict`, `--no-git` |
| **Status** | `ctf status -w <WORKSPACE> -u` | `--category <CAT>`, `--container`, `--unsolved`, `--solved` |
| **Instance** | `ctf instance start --id <ID> -w <WORKSPACE>` | `start`, `stop`, `restart`, `renew` · `--list`, `--auto-extend` |
| **Submit** | `ctf submit --id <ID> -f "FLAG{...}"` | `-w <WORKSPACE>`, `--auto` (submits all found in workspace) |
| **Hoard** | `ctf hoard <ID> "FLAG{...}"` | `--list`, `--all` (submit hoarded flags), `--remove <ID>` |
| **Bridge** | `ctf bridge start` · `ctf bridge status` | `start`, `stop`, `status`, `token` (port 18888 loopback) |
| **Watch** | `ctf watch -w <WORKSPACE>` | `--once`, `--start <ISO>`, `--end <ISO>`, `--no-scoreboard` |
| **Rank** | `ctf rank -w <WORKSPACE> -n 20` | `--top <N>`, `--no-docs` |
| **Sync** | `ctf sync -w <WORKSPACE> --verify` | Updates dynamic points/solves without re-downloading files |
| **History** | `ctf history -w <WORKSPACE> --tail 20` | `--all`, `--prune 'FLAG{...}'`, `--clear` |
| **Git** | `ctf git init -d <DIR> --remote-url <URL>` | `status`, `push`, `finish` (merges event branch to main) |
| **Menu** | `ctf menu` | Interactive full-screen TUI console |

---

## Core Workflows

### 1. Platform Setup & Authentication
The tool stores credentials in `~/.config/ctf_toolkit/config.json` (the **auth map**). Once configured or registered, commands do not require passing `-c`/`-t` again when `-w` or URL is known.

```bash
# Auto-generate temporary email + strong password and register on platform
ctf register -u https://ctf.example.com --tempmail

# Or pre-check connection, platform capabilities, and auth
ctf doctor -u https://ctf.example.com -c "session=xxx"

# Offline local environment & dependency inspection
ctf doctor --runtime
```

### 2. Pulling Challenges & Attachments
```bash
# Pull all challenges into a dedicated workspace directory
ctf pull -u https://ctf.example.com -c "session=xxx" -o ~/Workspace/CTF/Event_2026

# Strict verification: verifies ETag/Last-Modified and validates SHA-256
ctf pull -u https://ctf.example.com -o ~/Workspace/CTF/Event_2026 --verify-downloads strict
```
*Tip:* If shared git repo is configured, `ctf pull` automatically checks out a dedicated branch `ctf/<event_name>`, commits changes, and pushes to `origin`.

### 3. Dynamic Instances (Containers)
For web/pwn/cloud challenges that require on-demand remote container spawning:
```bash
# List active containers
ctf instance --list -w ~/Workspace/CTF/Event_2026

# Start container for challenge 12
ctf instance start --id 12 -w ~/Workspace/CTF/Event_2026

# Renew / extend container lifetime
ctf instance renew --id 12 -w ~/Workspace/CTF/Event_2026

# Stop container when finished
ctf instance stop --id 12 -w ~/Workspace/CTF/Event_2026
```

### 4. Safe Flag Submission & Hoarding
`ctf submit` includes cross-process concurrency locking, typed verdicts, and automatic rate-limit backoff:
```bash
# Submit single flag
ctf submit -w ~/Workspace/CTF/Event_2026 --id 12 -f "FLAG{example_flag}"

# Scan all challenge subdirectories for local flag.txt files and submit them
ctf submit -w ~/Workspace/CTF/Event_2026 --auto

# Hoard flag locally (saved in metadata, NOT sent to platform yet)
ctf hoard 12 "FLAG{example_flag}" -w ~/Workspace/CTF/Event_2026

# Review hoarded flags
ctf hoard --list -w ~/Workspace/CTF/Event_2026

# Submit all hoarded flags at once (e.g. before competition freeze)
ctf hoard --all -w ~/Workspace/CTF/Event_2026
```

### 5. Cloudflare & Browser Extension Bridge
When platforms protect endpoints with Cloudflare Turnstile or Managed Challenges:
1. `AdaptiveSession` automatically attempts browser TLS impersonation via `curl_cffi`.
2. If Turnstile remains blocking, start the local WebSocket bridge daemon:
```bash
# Start background bridge daemon (127.0.0.1:18888)
ctf bridge start

# Check bridge status and connected browser extension
ctf bridge status

# View or regenerate pairing token
ctf bridge token
```
3. Load `extension/` in Chrome/Brave/Edge (`chrome://extensions` -> *Load unpacked*). The extension connects to the daemon and routes CLI network requests transparently through real browser sessions with valid Turnstile clearance cookies.
4. Add `--bridge` to any command to force request execution through the browser bridge:
```bash
ctf pull -u https://ctf.example.com --bridge
```

### 6. Event Git Lifecycle
Manage competition workspaces under a single central repository:
```bash
# One-time setup for root directory
ctf git init -d ~/Workspace/CTF --remote-url git@github.com:org/ctf-archive.git

# Check status of current event branch
ctf git status -w ~/Workspace/CTF/Event_2026

# Commit and push current progress
ctf git push -w ~/Workspace/CTF/Event_2026 -m "solved crypto 12"

# Competition finish: merge event branch into main (--no-ff) and prune branch
ctf git finish -w ~/Workspace/CTF/Event_2026
```

---

## Common Mistakes & Traps

| Mistake | Correction |
|---|---|
| Using `ctf flag submit ...` or `ctf challenge pull ...` | Subcommands are top-level: `ctf submit --id <ID> -f <FLAG>`, `ctf pull -u <URL>` |
| Passing positional flags to submit: `ctf submit "FLAG{...}"` | Requires `--id <ID> -f "<FLAG>"` or `--name "<NAME>"` |
| Using `ctf container ...` | Correct subcommand is `ctf instance [start\|stop\|renew\|restart]` |
| Guessing `--temp-email` or `--disposable` | Correct flag is `ctf register ... --tempmail` |
| Attempting blind POST retries on network drop | Never force-replay POST requests. Check `ctf status` or `ctf history` to confirm solve verdict first. |
| Re-entering cookies on every command | Once registered or pulled, the auth token is persisted in `~/.config/ctf_toolkit/config.json`. Simply pass `-w <workspace>`. |

---

## Integration with Solver Pipelines

`ctf-toolkit` manages platform interaction and workspace structure (fetching challenges, attachments, container endpoints, live status, and verified submits).

When used alongside local solver agents (such as the autonomous CTF solver pipeline):
1. **Fetch & prepare:** `ctf pull -u <URL> -o ~/Workspace/CTF/<Event>`
2. **Launch instances if required:** `ctf instance start --id <ID> -w ~/Workspace/CTF/<Event>`
3. **Solve:** Solvers work directly inside challenge subdirectories (`~/Workspace/CTF/<Event>/<category>/<chall>/`) and save outputs to `flag.txt`.
4. **Auto-submit:** Run `ctf submit -w ~/Workspace/CTF/<Event> --auto` to automatically detect, validate, and submit newly recovered flags across all categories without manual intervention.
