import json
from unittest.mock import MagicMock
import pytest
import requests

from ctf_downloader.platforms.detector import PlatformDetector
from ctf_downloader.platforms.tfcctf import TFCCTFPlatform

def make_resp(status=200, data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {"content-type": "application/json"}
    r.text = text if text else (json.dumps(data) if data is not None else "")
    if data is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = data
    return r

def test_tfcctf_detection():
    session = MagicMock()
    session.get.return_value = make_resp(200, text="<meta name='keywords' content='tfcctf'>")
    session.cookies = MagicMock()
    session.cookies.keys.return_value = []

    platform, info = PlatformDetector.detect_platform_info("https://ctf.thefewchosen.com", session, quiet=True)
    assert info.platform_type == "tfcctf"
    assert info.confidence == "high"
    assert isinstance(platform, TFCCTFPlatform)

def test_tfcctf_fetch_challenges():
    session = MagicMock()
    session.headers = {"Authorization": "Bearer dummy_token"}
    platform = TFCCTFPlatform("https://ctf.thefewchosen.com", session)

    mock_data = {
        "categories": [
            {"id": "cat1", "name": "Web"},
            {"id": "cat2", "name": "Crypto"}
        ],
        "difficulties": [
            {"id": "diff1", "name": "Easy"}
        ],
        "challenges": [
            {
                "challenge_id": "c1",
                "challenge_name": "Web Warmup",
                "category_id": "cat1",
                "difficulty_id": "diff1",
                "description": "Solve this web chall",
                "challenge_author": "admin",
                "amount_solves": 42,
                "flags": [
                    {"flag_id": "f1", "flag_points": 100, "is_solved": False}
                ],
                "files": [
                    {"file_name": "app.py", "file_url": "https://api.ctf.thefewchosen.com/files/app.py"}
                ],
                "connection_mode": "instance",
                "is_dynamic": True
            }
        ]
    }

    session.get.return_value = make_resp(200, data=mock_data)

    challs = platform.fetch_challenges()
    assert len(challs) == 1
    c = challs[0]
    assert c.id == "c1"
    assert c.name == "Web Warmup"
    assert c.category == "Web"
    assert c.points == 100
    assert c.solves_count == 42
    assert "container" in c.tags
    assert len(c.files) == 1
    assert c.files[0][1] == "app.py"

def test_tfcctf_submit_flag():
    session = MagicMock()
    session.headers = {"Authorization": "Bearer dummy_token"}
    platform = TFCCTFPlatform("https://ctf.thefewchosen.com", session)

    session.post.return_value = make_resp(200, data={"ok": True})

    ok, msg = platform.submit_flag("c1", "TFCCTF{dummy}")
    assert ok is True
    assert platform.last_verdict == "correct"
