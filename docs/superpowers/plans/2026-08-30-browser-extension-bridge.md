# Browser Extension Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** X\u00e2y d\u1ef1ng h\u1ec7 th\u1ed1ng Browser Extension Manifest V3 v\u00e0 Local WebSocket Bridge Daemon k\u1ebft n\u1ed1i v\u1edbi `AdaptiveSession` trong CLI \u0111\u1ec3 chuy\u1ec3n h\u01b0\u1edbng to\u00e0n b\u1ed9 c\u00e1c request m\u1ea1ng qua tr\u00ecnh duy\u1ec7t, v\u01b0\u1ee3t 100% Cloudflare Turnstile / Managed Challenge.

**Architecture:** CLI ch\u1ea1y m\u1ed9t loopback WebSocket server c\u1ee5c b\u1ed9 (`127.0.0.1:18888`) b\u1ea3o v\u1ec7 b\u1eb1ng token. Browser Extension (Manifest V3) k\u1ebft n\u1ed1i t\u1edbi bridge n\u00e0y. Khi CLI g\u1eb7p Cloudflare ho\u1eb7c \u0111\u01b0\u1ee3c c\u1ea5u h\u00ecnh qua Bridge, `AdaptiveSession` g\u1eedi `REQUEST_FORWARD` qua WebSocket -> Extension th\u1ef1c thi `fetch()` v\u1edbi context c\u1ee7a tr\u00ecnh duy\u1ec7t v\u00e0 tr\u1ea3 `RESPONSE_FORWARD` (status, headers, body/binary) v\u1ec1 cho CLI.

**Tech Stack:** Python (asyncio, websockets, requests, pytest), JavaScript (Manifest V3 Chrome Extension, Service Worker, Fetch API).

**Spec:** `docs/superpowers/specs/2026-08-30-browser-extension-bridge-design.md`

## Global Constraints

- **Python Floor:** Python 3.9+ (h\u1ed7 tr\u1ee3 Python 3.13)
- **Port:** M\u1eb7c \u0111\u1ecbnh `18888` tr\u00ean `127.0.0.1` (loopback only)
- **Token Path:** `~/.config/ctf_toolkit/bridge_token`
- **PID File:** `~/.config/ctf_toolkit/bridge.pid`
- **Zero Regression:** To\u00e0n b\u1ed9 test suite c\u0169 (41+ tests) ph\u1ea3i ti\u1ebfp t\u1ee5c PASS 100%

---

### Task 1: Protocol Constants & Bridge Message Schema

**Files:**
- Create: `ctf_downloader/bridge/__init__.py`
- Create: `ctf_downloader/bridge/constants.py`
- Create: `ctf_downloader/bridge/messages.py`
- Test: `test_bridge_protocol.py`

**Interfaces:**
- Produces:
  - `BridgeMessageType`: Enum (`HANDSHAKE`, `HANDSHAKE_ACK`, `REQUEST_FORWARD`, `RESPONSE_FORWARD`, `COOKIE_UPDATE`, `PING`, `PONG`, `ERROR`)
  - `BridgeRequest(id, method, url, headers, body, timeout_ms, binary)`
  - `BridgeResponse(id, status_code, status_text, headers, body, is_base64, error)`
  - `serialize_message(msg_type, payload) -> str`
  - `deserialize_message(raw_json) -> tuple[BridgeMessageType, dict]`

- [ ] **Step 1: Write failing tests in `test_bridge_protocol.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `ctf_downloader/bridge/constants.py` and `messages.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 2: Local WebSocket Bridge Server & Daemon Lifecycle

**Files:**
- Create: `ctf_downloader/bridge/server.py`
- Create: `ctf_downloader/bridge/daemon.py`
- Test: `test_bridge_server.py`

**Interfaces:**
- Consumes: `BridgeMessageType`, `BridgeRequest`, `BridgeResponse` from Task 1
- Produces:
  - `BridgeServer(host="127.0.0.1", port=18888, token=None)`
  - `BridgeServer.start()` / `BridgeServer.stop()`
  - `BridgeServer.send_request(request: BridgeRequest) -> BridgeResponse`
  - `BridgeDaemon.ensure_running() -> BridgeServerInfo`
  - `BridgeDaemon.stop()`

- [ ] **Step 1: Write failing tests in `test_bridge_server.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `server.py` and `daemon.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 3: BrowserBridgeTransport & AdaptiveSession Integration

**Files:**
- Create: `ctf_downloader/bridge/transport.py`
- Modify: `ctf_downloader/utils/http_client.py`
- Test: `test_bridge_transport.py`

**Interfaces:**
- Consumes: `BridgeDaemon`, `BridgeRequest`, `BridgeResponse` from Tasks 1 & 2
- Produces:
  - `BrowserBridgeTransport.send(request, **kwargs) -> requests.Response`
  - `AdaptiveSession` auto-routing to `BrowserBridgeTransport` when Cloudflare Challenge is triggered or when bridge mode is active.

- [ ] **Step 1: Write failing tests in `test_bridge_transport.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `transport.py` and modify `http_client.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 4: Browser Extension (Manifest V3)

**Files:**
- Create: `extension/manifest.json`
- Create: `extension/background/service_worker.js`
- Create: `extension/background/bridge_client.js`
- Create: `extension/background/cookie_tracker.js`
- Create: `extension/popup/popup.html`
- Create: `extension/popup/popup.css`
- Create: `extension/popup/popup.js`
- Create: `extension/icons/icon16.png`, `extension/icons/icon48.png`, `extension/icons/icon128.png`
- Create: `extension/README.md`
- Test: `test_extension_manifest.py`

**Interfaces:**
- Produces:
  - Working Manifest V3 extension ready to load via `chrome://extensions` (Developer mode).
  - Background Service Worker executing `fetch()` on behalf of CLI and streaming base64 chunks.

- [ ] **Step 1: Write validation test `test_extension_manifest.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement all files in `extension/`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 5: CLI Subcommand, Doctor Check, & Global Configuration

**Files:**
- Modify: `ctf_downloader/cli.py`
- Modify: `ctf_downloader/cli_commands.py`
- Modify: `ctf_downloader/services/health_service.py`
- Test: `test_cli_bridge_commands.py`

**Interfaces:**
- Produces:
  - `ctf bridge` subcommand (`status`, `start`, `stop`, `token`)
  - `ctf doctor` diagnostics reporting Bridge daemon status & Extension connection
  - `--bridge` option on `ctf pull` / `ctf sync` / `ctf submit`

- [ ] **Step 1: Write failing tests in `test_cli_bridge_commands.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement CLI commands and doctor integration**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

### Task 6: Full Regression Verification & End-to-End Test

**Files:**
- Test: `test_bridge_e2e.py`
- Run entire test suite

- [ ] **Step 1: Write and run `test_bridge_e2e.py`**
- [ ] **Step 2: Run full pytest suite across entire repo**
- [ ] **Step 3: Commit final integration verification**
