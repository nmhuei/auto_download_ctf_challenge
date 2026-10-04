"""Adapter template for custom CTF Platform.

Usage:
1. Copy this template to `ctf_downloader/platforms/<platform_key>.py`.
2. Replace `example_ctf` with your platform key and `Example CTF` with the display label.
3. Implement `authenticate`, `fetch_challenges`, `get_full_file_url`, and `submit_flag`.
4. Register the module in `ctf_downloader/platforms/registry.py` under the import block.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .base import BasePlatform, safe_get, safe_get_json
from .registry import register
from ..models import Challenge
from ..utils.logger import Logger


@register(
    "example_ctf",
    label="Example CTF",
    throttle=2.0,
    html_markers=("regex:<meta[^>]+content=[\"']ExampleCTF", "Example CTF"),
    cookie_hints=("example_session",),
    supports_container=False,
    supports_scoreboard=True,
)
class ExampleCTFAdapter(BasePlatform):
    """Platform driver for Example CTF."""

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
