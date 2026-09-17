import json
from unittest.mock import MagicMock
import pytest
import requests

from ctf_downloader.platforms.detector import PlatformDetector
from ctf_downloader.platforms.noctf import NoCTFPlatform


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


def test_noctf_detection():
    session = MagicMock()
    session.get.return_value = make_resp(200, text="<title>noCTF</title>")
    session.cookies = MagicMock()
    session.cookies.keys.return_value = []

    platform, info = PlatformDetector.detect_platform_info("https://scoreboard.k17ctf.secso.cc", session, quiet=True)
    assert info.platform_type == "noctf"
    assert info.confidence == "high"
    assert isinstance(platform, NoCTFPlatform)


def test_noctf_authenticate():
    session = MagicMock()
    session.headers = {"Authorization": "Bearer dummy_token"}
    platform = NoCTFPlatform("https://scoreboard.k17ctf.secso.cc", session)

    session.get.return_value = make_resp(200, data={"data": {"id": 1, "name": "bqa", "team_name": "AI_slop"}})
    assert platform.authenticate() is True
    assert platform.ctf_info.user_name == "bqa"
    assert platform.ctf_info.team_name == "AI_slop"


def test_noctf_fetch_challenges():
    session = MagicMock()
    session.headers = {"Authorization": "Bearer dummy_token"}
    platform = NoCTFPlatform("https://scoreboard.k17ctf.secso.cc", session)

    list_data = {
        "data": {
            "challenges": [
                {
                    "id": 10,
                    "slug": "cry-pto",
                    "title": "cry-pto",
                    "tags": {"categories": "beginner,crypto", "difficulty": "beginner"},
                    "value": 100,
                    "solve_count": 236,
                    "solved_by_me": False,
                }
            ]
        }
    }

    detail_data = {
        "data": {
            "id": 10,
            "description": "Connection command: `nc chal.secso.cc 2000`",
            "metadata": {
                "files": [
                    {
                        "filename": "chal.py",
                        "size": 1579,
                        "url": "files/local/n3UV9inPNFkgnbehz9BPq",
                    }
                ],
                "hints": ["Check matrix dimensions"],
            },
        }
    }

    def mock_get(url, *args, **kwargs):
        if url.endswith("/challenges"):
            return make_resp(200, data=list_data)
        elif url.endswith("/challenges/10"):
            return make_resp(200, data=detail_data)
        return make_resp(404)

    session.get.side_effect = mock_get

    challs = platform.fetch_challenges()
    assert len(challs) == 1
    c = challs[0]
    assert c.id == 10
    assert c.name == "cry-pto"
    assert c.category == "Crypto"
    assert "beginner" in c.tags
    assert len(c.files) == 1
    assert c.files[0][1] == "chal.py"
    assert c.connection_info == "chal.secso.cc:2000"


def test_noctf_submit_flag():
    session = MagicMock()
    session.headers = {"Authorization": "Bearer dummy_token"}
    platform = NoCTFPlatform("https://scoreboard.k17ctf.secso.cc", session)

    session.post.return_value = make_resp(200, data={"data": {"status": "correct"}})
    ok, msg = platform.submit_flag(10, "K17{test_flag}")
    assert ok is True
    assert platform.last_verdict == "correct"

    session.post.return_value = make_resp(200, data={"data": {"status": "incorrect"}})
    ok, msg = platform.submit_flag(10, "K17{wrong_flag}")
    assert ok is False
    assert platform.last_verdict == "incorrect"
