# [CTF-PULL-D02] Unknown or Unsupported CTF Platform

Incident class: Unrecognized, unknown or unsupported CTF platform.
Target objective: Enable platform detection and challenge pulling for this target.

---

## ⚡ DO NOT READ THE ENTIRE CODEBASE BLINDLY. FOLLOW THIS DIRECTIVE:

You have two paths to add support for this platform:

### 🌟 Path A: Declarative JSON Schema (Recommended - 90% of cases, ZERO Python code)
If the detector evidence shows standard REST JSON endpoints (e.g. `/api/challenges`, `/api/v1/challenges`), DO NOT write a Python adapter!
Simply create a declarative schema file at `ctf_downloader/platforms/definitions/<key>.json`:

```json
{
  "key": "<platform_key>",
  "label": "<Display Name CTF>",
  "throttle": 2.0,
  "html_markers": ["<Keyword from HTML title or meta generator>"],
  "cookie_hints": ["<session_cookie_name>"],
  "endpoints": {
    "auth_check": "/api/v1/user/me",
    "challenges": "/api/v1/challenges",
    "challenge_detail": "/api/v1/challenges/{id}",
    "submit": "/api/v1/challenges/{id}/submit",
    "scoreboard": "/api/v1/scoreboard"
  },
  "schema_mapping": {
    "challenges_root": "<path_to_array, e.g. data.challenges or data or items>",
    "id": "id",
    "name": "title",
    "category": "category",
    "points": "points",
    "description": "description",
    "files": "files",
    "solved": "solved",
    "connection_info": "connection_info"
  },
  "submit_spec": {
    "method": "POST",
    "flag_param": "flag",
    "content_type": "json",
    "success_field": "data.correct",
    "message_field": "message"
  },
  "supports_container": false,
  "supports_scoreboard": true,
  "source": "builtin",
  "description": "Declarative schema definition for <Display Name>."
}
```
Add a simple schema loading test in `tests/test_platform_<key>.py` to verify that `get_spec("<key>")` loads the schema.

---

### 🛠️ Path B: Dedicated Python Adapter (When custom request signing, GraphQL, or complex logic is required)
1. Create `ctf_downloader/platforms/<key>.py` inheriting from `BasePlatform`:
   - Decorate class with `@register("<key>", label="...", html_markers=(...), cookie_hints=(...))`.
   - Implement `authenticate() -> bool`, `fetch_challenges() -> List[Challenge]`, `get_full_file_url(file_path: str) -> str`, `submit_flag(challenge_id, flag: str) -> Tuple[bool, str]`.
2. Register the module by adding `from . import <key>` in `ctf_downloader/platforms/registry.py` (around line 98).
3. Write a mock-based test in `tests/test_platform_<key>.py` using standard library `unittest.mock.MagicMock` to verify `fetch_challenges()` without network calls.

---

## 🧪 Verification Requirement
Run the newly created unit test before finishing:
```bash
pytest tests/test_platform_<key>.py
```
After tests pass, BQA will automatically retry the original pull command.
