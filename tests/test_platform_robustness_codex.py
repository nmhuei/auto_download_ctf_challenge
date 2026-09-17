from unittest.mock import MagicMock
import pytest

from ctf_downloader.platforms.noctf import NoCTFPlatform
from ctf_downloader.platforms.tfcctf import TFCCTFPlatform
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform


def test_noctf_handles_top_level_list_and_nested_list():
    mock_session = MagicMock()
    # Mock top-level list
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"id": 101, "title": "Chall 1", "value": 100, "solve_count": 5}
    ]
    mock_session.get.return_value = mock_resp

    platform = NoCTFPlatform("https://k17ctf.secso.cc", mock_session)
    challs = platform.fetch_challenges()
    assert len(challs) == 1
    assert challs[0].name == "Chall 1"

    # Mock {"data": [{...}]}
    mock_resp.json.return_value = {
        "data": [{"id": 102, "title": "Chall 2", "value": 200, "solve_count": 3}]
    }
    challs = platform.fetch_challenges()
    assert len(challs) == 1
    assert challs[0].name == "Chall 2"


def test_noctf_rejects_attacker_api_url():
    mock_session = MagicMock()
    mock_resp1 = MagicMock(status_code=200, text='<script src="/_app/immutable/entry/app.xyz.js"></script>')
    mock_resp2 = MagicMock(status_code=200, text='"../chunks/index.svelte.123.js"')
    mock_resp3 = MagicMock(status_code=200, text='const api = "https://api.attacker.evil/v1";')
    mock_session.get.side_effect = [mock_resp1, mock_resp2, mock_resp3]

    platform = NoCTFPlatform("https://k17ctf.secso.cc", mock_session)
    # The attacker URL should be rejected
    assert "attacker.evil" not in platform.api_url


def test_tfcctf_handles_top_level_list():
    mock_session = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = [
        {"challenge_id": 1, "challenge_name": "TFC 1", "amount_solves": 10}
    ]
    mock_session.get.return_value = mock_resp

    platform = TFCCTFPlatform("https://ctf.tfcctf.com", mock_session)
    challs = platform.fetch_challenges()
    assert len(challs) == 1
    assert challs[0].name == "TFC 1"


def test_ctfd_detail_timeout_does_not_lose_remaining_challenges():
    mock_session = MagicMock()
    list_resp = MagicMock(status_code=200)
    list_resp.headers = {"content-type": "application/json"}
    list_resp.url = "https://ctf.example.com/api/v1/challenges"
    list_resp.json.return_value = {
        "success": True,
        "data": [
            {"id": 1, "name": "Chall 1", "category": "Web"},
            {"id": 2, "name": "Chall 2", "category": "Pwn"},
        ],
    }
    detail_ok = MagicMock(status_code=200)
    detail_ok.json.return_value = {
        "success": True,
        "data": {"description": "Chall 1 description", "files": []},
    }

    teams_resp = MagicMock(status_code=404)
    # First call: list challenges; Second call: teams/me/solves; Third call: detail chall 1 (ok); Fourth call: detail chall 2 (raises Timeout)
    mock_session.get.side_effect = [
        list_resp,
        teams_resp,
        detail_ok,
        TimeoutError("Connection timed out"),
    ]

    platform = CTFdPlatform("https://ctf.example.com", mock_session)
    challs = platform.fetch_challenges()
    # Both challenges should be returned, with chall 2 falling back to list metadata
    assert len(challs) == 2
    assert challs[0].name == "Chall 1"
    assert challs[0].description == "Chall 1 description"
    assert challs[1].name == "Chall 2"


def test_ctfd_whale_async_acknowledgement():
    mock_session = MagicMock()
    resp = MagicMock(status_code=200)
    # Whale standard asynchronous start acknowledgement
    resp.json.return_value = {"success": True, "message": "Container is creating..."}
    mock_session.post.return_value = resp

    platform = CTFdPlatform("https://ctf.example.com", mock_session)
    success, info = platform.start_instance(123)

    assert success is True
    assert info.get("status") == "pending"


def test_gzctf_detail_timeout_does_not_drop_contest():
    mock_session = MagicMock()
    list_resp = MagicMock(status_code=200)
    list_resp.json.return_value = [
        {
            "category": "Web",
            "challenges": [
                {"id": 10, "title": "GZ Web 1", "score": 100},
                {"id": 11, "title": "GZ Web 2", "score": 200},
            ],
        }
    ]
    sb_resp = MagicMock(status_code=404)
    detail_ok = MagicMock(status_code=200)
    detail_ok.json.return_value = {"content": "Web 1 details"}

    mock_session.get.side_effect = [
        list_resp,
        sb_resp,
        detail_ok,
        TimeoutError("GZ detail timeout"),
    ]

    platform = GZCTFPlatform("https://gz.example.com/games/1", mock_session)
    challs = platform.fetch_challenges()
    assert len(challs) == 2
    assert challs[0].name == "GZ Web 1"
    assert challs[0].description == "Web 1 details"
    assert challs[1].name == "GZ Web 2"


def test_gzctf_public_game_does_not_authenticate_expired_profile():
    mock_session = MagicMock()
    profile_resp = MagicMock(status_code=401)
    game_resp = MagicMock(status_code=200)
    game_resp.json.return_value = {"title": "Public Contest 2026"}

    mock_session.get.side_effect = [profile_resp, game_resp]

    platform = GZCTFPlatform("https://gz.example.com/games/1", mock_session)
    # Even if game endpoint returns 200, unauthenticated profile must return False
    assert platform.authenticate() is False


def test_rctf_start_instance_rejects_bad_kind_or_empty_response():
    mock_session = MagicMock()
    resp_bad = MagicMock(status_code=200)
    resp_bad.json.return_value = {"kind": "badClientToken", "message": "Invalid token"}
    mock_session.put.return_value = resp_bad

    platform = RCTFPlatform("https://rctf.example.com", mock_session)
    success, info = platform.start_instance("chall-1")
    assert success is False
    assert "Invalid token" in info.get("message", "")

    # Empty dictionary
    resp_empty = MagicMock(status_code=200)
    resp_empty.json.return_value = {}
    mock_session.put.return_value = resp_empty

    success, info = platform.start_instance("chall-2")
    assert success is False
