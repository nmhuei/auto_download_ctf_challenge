import pytest
from unittest.mock import patch, MagicMock
import requests
from ctf_downloader.bridge.transport import BrowserBridgeTransport
from ctf_downloader.bridge.messages import BridgeResponse
from ctf_downloader.utils.http_client import AdaptiveSession


def test_bridge_transport_response_conversion():
    mock_bridge_resp = BridgeResponse(
        id="test_req_1",
        status_code=200,
        status_text="OK",
        headers={"Content-Type": "application/json", "X-Custom": "abc"},
        body='{"success": true, "data": [1, 2, 3]}',
        is_base64=False,
        error=None,
    )
    transport = BrowserBridgeTransport(auto_start_daemon=False)
    with patch.object(transport, "_dispatch_request", return_value=mock_bridge_resp):
        req = requests.Request(
            "GET",
            "https://mirror-ctf.compfest.id/api/v1/challenges",
            headers={"Authorization": "Bearer test"},
        ).prepare()
        resp = transport.send(req)
        assert resp.status_code == 200
        assert resp.headers["X-Custom"] == "abc"
        assert resp.json() == {"success": True, "data": [1, 2, 3]}
        assert resp.text == '{"success": true, "data": [1, 2, 3]}'


def test_bridge_transport_binary_conversion():
    raw_payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    import base64
    b64_payload = base64.b64encode(raw_payload).decode("ascii")

    mock_bridge_resp = BridgeResponse(
        id="test_req_bin",
        status_code=200,
        status_text="OK",
        headers={"Content-Type": "image/png", "Content-Length": str(len(raw_payload))},
        body=b64_payload,
        is_base64=True,
        error=None,
    )
    transport = BrowserBridgeTransport(auto_start_daemon=False)
    with patch.object(transport, "_dispatch_request", return_value=mock_bridge_resp):
        req = requests.Request("GET", "https://mirror-ctf.compfest.id/files/chall.png").prepare()
        resp = transport.send(req)
        assert resp.status_code == 200
        assert resp.content == raw_payload


def test_adaptive_session_routes_to_bridge_on_cf_challenge():
    session = AdaptiveSession(enable_bridge=True)
    # Mock requests.Session.send returning 403 Cloudflare challenge
    cf_resp = requests.Response()
    cf_resp.status_code = 403
    cf_resp.headers["Server"] = "cloudflare"
    cf_resp._content = b"<html><title>Just a moment...</title>cf-chl-bypass</html>"

    success_resp = requests.Response()
    success_resp.status_code = 200
    success_resp._content = b'{"challenges": [{"id": 1, "name": "Sanity"}]}'

    with patch.object(requests.Session, "send", return_value=cf_resp), \
         patch.object(BrowserBridgeTransport, "send", return_value=success_resp):
        resp = session.get("https://mirror-ctf.compfest.id/api/v1/challenges")
        assert resp.status_code == 200
        assert resp.json()["challenges"][0]["name"] == "Sanity"
