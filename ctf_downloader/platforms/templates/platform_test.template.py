"""Unit test template for custom CTF platform adapter.

Usage:
1. Copy this template to `tests/test_platform_<platform_key>.py`.
2. Replace `example_ctf` with your platform key and verify deterministic behavior.
3. Run `pytest tests/test_platform_<platform_key>.py`.
"""
from unittest.mock import MagicMock
from ctf_downloader.platforms.registry import get_spec


def test_platform_lifecycle():
    base_url = "https://ctf.example.org"

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
                "name": "Sanity Check",
                "category": "Misc",
                "points": 100,
                "description": "Welcome challenge.",
                "files": ["files/welcome.txt"],
                "solved": False
            }
        ]
    }

    resp_submit = MagicMock()
    resp_submit.json.return_value = {"correct": True, "message": "Flag accepted!"}

    def fake_get(url, **kwargs):
        if "user/me" in url:
            return resp_auth
        if "challenges" in url:
            return resp_challs
        return None

    session.get.side_effect = fake_get
    session.post.return_value = resp_submit

    spec = get_spec("example_ctf")
    platform = spec.cls(base_url, session)

    # 1. Test authentication
    assert platform.authenticate() is True
    assert platform.ctf_info.user_name == "testuser"

    # 2. Test challenges fetching
    challs = platform.fetch_challenges()
    assert len(challs) == 1
    assert challs[0].id == "1"
    assert challs[0].name == "Sanity Check"
    assert challs[0].category == "Misc"
    assert challs[0].points == 100

    # 3. Test attachment URL resolution
    assert platform.get_full_file_url("files/welcome.txt") == "https://ctf.example.org/files/welcome.txt"

    # 4. Test flag submission
    ok, msg = platform.submit_flag("1", "FLAG{example}")
    assert ok is True
    assert platform.last_verdict == "correct"
