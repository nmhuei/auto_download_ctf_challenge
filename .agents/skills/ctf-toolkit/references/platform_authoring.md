# Platform Authoring & Extensibility Specification

This document defines the strict architectural standards, workflows, and templates for adding support for new or unknown CTF platforms to the CTF Toolkit.

---

## 1. Decision Matrix: Choose Path A or Path B

When encountering a new or unhandled platform (`CTF-PULL-D02`), **DO NOT blindly explore the codebase**. Follow this two-path decision hierarchy:

```mermaid
flowchart TD
    A[New / Unknown Platform Detected] --> B{Does it use a standard REST JSON API?}
    B -- Yes (90% of cases) --> C[Path A: Declarative JSON Schema (Zero Python Code)]
    B -- No (10% - custom crypto/HMAC, GraphQL, weird sockets) --> D[Path B: Python Adapter Subclassing BasePlatform]
```

- **Path A (Declarative JSON - Highly Preferred)**: Zero lines of Python. Drop a JSON file into `ctf_downloader/platforms/definitions/<key>.json` (or workspace `.ctf/platforms/<key>.json`). Handled automatically by `ConfigurablePlatform` and `PlatformSchemaStore`.
- **Path B (Custom Python Adapter)**: Only when Path A cannot handle authentication or custom challenge payloads.

---

## 2. Path A: Declarative JSON Specification (Zero Code)

### Target Location
- Built-in (permanent): `ctf_downloader/platforms/definitions/<key>.json`
- Workspace scope: `.ctf/platforms/<key>.json`
- Global user scope: `~/.config/ctf_toolkit/platforms/<key>.json`

### JSON Template (`<key>.json`)
```json
{
  "key": "myplatform",
  "label": "MyPlatform CTF",
  "throttle": 2.0,
  "html_markers": [
    "Powered by MyPlatform",
    "meta name=\"generator\" content=\"MyPlatform\"",
    "myplatform-root"
  ],
  "cookie_hints": [
    "myplatform_session",
    "mp_token"
  ],
  "url_patterns": [
    "/myplatform"
  ],
  "endpoints": {
    "auth_check": "/api/v1/user/me",
    "challenges": "/api/v1/challenges",
    "challenge_detail": "/api/v1/challenges/{id}",
    "submit": "/api/v1/challenges/{id}/submit",
    "scoreboard": "/api/v1/scoreboard"
  },
  "schema_mapping": {
    "challenges_root": "data.challenges",
    "id": "id",
    "name": "title",
    "category": "category",
    "points": "points",
    "description": "description",
    "files": "files",
    "solved": "solved",
    "connection_info": "connection_info",
    "solves_count": "solves"
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
  "description": "Declarative schema definition for MyPlatform."
}
```

### Path A Testing Checklist
Add a test in `tests/test_platform_definitions.py`:
```python
def test_myplatform_schema_loads():
    from ctf_downloader.platforms.registry import get_spec
    spec = get_spec("myplatform")
    assert spec.key == "myplatform"
    assert spec.label == "MyPlatform CTF"
```

---

## 3. Path B: Custom Python Adapter Specification

When Path A is insufficient, create a dedicated adapter module.

### Target Location
Create `ctf_downloader/platforms/<key>.py` and register it in `ctf_downloader/platforms/registry.py`.

### Python Boilerplate Template (`ctf_downloader/platforms/<key>.py`)
```python
"""Adapter for <Platform Name> CTF Platform."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .base import BasePlatform, safe_get, safe_get_json
from .registry import register
from ..models import Challenge
from ..utils.logger import Logger


@register(
    "myplatform",
    label="MyPlatform CTF",
    throttle=2.0,
    html_markers=("regex:<meta[^>]+content=[\"']MyPlatform", "MyPlatform CTF"),
    cookie_hints=("myplatform_session",),
    supports_container=False,
    supports_scoreboard=True,
)
class MyPlatformAdapter(BasePlatform):
    """Platform driver for MyPlatform."""

    def authenticate(self) -> bool:
        """Validate active session credentials."""
        url = f"{self.base_url}/api/v1/user/me"
        data, status = safe_get_json(self.session, url, statuses=(200, 401, 403))
        if status == 200 and data and isinstance(data, dict):
            self.ctf_info.user_name = data.get("username", "")
            return True
        return False

    def fetch_challenges(self) -> List[Challenge]:
        """Fetch and normalize challenges into Challenge dataclass instances."""
        url = f"{self.base_url}/api/v1/challenges"
        data, status = safe_get_json(self.session, url, statuses=(200,))
        if not data or not isinstance(data, dict):
            return []

        challenges: List[Challenge] = []
        raw_items = data.get("data", [])
        for item in raw_items:
            cid = str(item.get("id", ""))
            name = str(item.get("name", item.get("title", f"Challenge {cid}")))
            category = str(item.get("category", "General"))
            points = int(item.get("points", item.get("value", 0)))
            description = str(item.get("description", ""))
            files = [str(f) for f in item.get("files", [])]
            solved_by_me = bool(item.get("solved", item.get("solved_by_me", False)))
            connection_info = item.get("connection_info")

            ch = Challenge(
                id=cid,
                name=name,
                category=category,
                points=points,
                description=description,
                files=files,
                solved_by_me=solved_by_me,
                connection_info=connection_info,
            )
            challenges.append(ch)
        return challenges

    def get_full_file_url(self, file_path: str) -> str:
        """Convert relative attachment path to full URL."""
        if file_path.startswith(("http://", "https://")):
            return file_path
        return urljoin(f"{self.base_url}/", file_path.lstrip("/"))

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """Submit flag and return (is_correct, response_message)."""
        url = f"{self.base_url}/api/v1/challenges/{challenge_id}/submit"
        payload = {"flag": flag}
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            data = resp.json()
            is_correct = bool(data.get("correct", False))
            self.last_verdict = "correct" if is_correct else "incorrect"
            msg = str(data.get("message", "Flag submitted."))
            return is_correct, msg
        except Exception as e:
            return False, f"Submit error: {e}"
```

### Registration Step in `ctf_downloader/platforms/registry.py`
Add `<key>` to the import list in line 98:
```python
from . import asisctf, ctfd, custom_rest, generic_html, gzctf, myplatform, noctf, rctf, tfcctf  # noqa: E402,F401
```

---

## 4. Deterministic Unit Test Template (`tests/test_platform_<key>.py`)

Agents MUST add a unit test using standard library `unittest.mock` (zero external dependency) so verification passes in < 2 seconds:

```python
"""Unit tests for MyPlatform adapter."""
from unittest.mock import MagicMock
from ctf_downloader.platforms.registry import get_spec


def test_myplatform_lifecycle():
    base_url = "https://ctf.myplatform.org"

    # Setup session mock
    session = MagicMock()
    resp_auth = MagicMock()
    resp_auth.status_code = 200
    resp_auth.json.return_value = {"username": "testuser"}

    resp_challs = MagicMock()
    resp_challs.status_code = 200
    resp_challs.json.return_value = {
        "data": [
            {
                "id": 1,
                "name": "Baby Reversible",
                "category": "Rev",
                "points": 100,
                "description": "Crack me.",
                "files": ["files/baby.bin"],
                "solved": False
            }
        ]
    }

    resp_submit = MagicMock()
    resp_submit.json.return_value = {"correct": True, "message": "Correct flag!"}

    def fake_get(url, **kwargs):
        if "user/me" in url:
            return resp_auth
        if "challenges" in url:
            return resp_challs
        return None

    session.get.side_effect = fake_get
    session.post.return_value = resp_submit

    spec = get_spec("myplatform")
    platform = spec.cls(base_url, session)

    assert platform.authenticate() is True
    assert platform.ctf_info.user_name == "testuser"

    challs = platform.fetch_challenges()
    assert len(challs) == 1
    assert challs[0].id == "1"
    assert challs[0].name == "Baby Reversible"
    assert challs[0].category == "Rev"
    assert challs[0].points == 100
    assert platform.get_full_file_url("files/baby.bin") == "https://ctf.myplatform.org/files/baby.bin"

    ok, msg = platform.submit_flag("1", "FLAG{123}")
    assert ok is True
```

Run test:
```bash
pytest tests/test_platform_myplatform.py
```
