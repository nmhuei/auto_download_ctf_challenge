import asyncio
import pytest
from unittest.mock import patch, MagicMock
import requests
from ctf_downloader.bridge.transport import (
    BridgeAuthenticationError,
    BridgeDaemonUnavailableError,
    BridgeTransportError,
    BrowserBridgeTransport,
)
from ctf_downloader.bridge.messages import (
    BridgeRequest,
    BridgeResponse,
    serialize_message,
)
from ctf_downloader.bridge.constants import BridgeMessageType
from ctf_downloader.utils.http_client import AdaptiveSession


class _ScriptedBridgeSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if not self.responses:
            raise AssertionError("scripted bridge socket exhausted")
        return self.responses.pop(0)


class _FakeTempFile:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False
        self.pos = 0

    def write(self, data):
        self.buf.extend(data)
        self.pos = len(self.buf)

    def flush(self):
        pass

    def seek(self, pos):
        self.pos = pos

    def read(self, size=-1):
        data = bytes(self.buf)
        return data[self.pos:] if size < 0 else data[self.pos:self.pos + size]

    def tell(self):
        return self.pos

    def close(self):
        self.closed = True


class _FakeHandshakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        return self.response


def test_bridge_daemon_start_failure_is_actionable():
    transport = BrowserBridgeTransport(auto_start_daemon=True)
    with patch.object(transport.daemon, "is_port_open", return_value=False), \
         patch.object(transport.daemon, "ensure_running", return_value=False):
        with pytest.raises(
            BridgeDaemonUnavailableError,
            match=r"ctf bridge start",
        ):
            transport._ensure_daemon_ready()


def test_bridge_foreign_port_is_reported_before_websocket_connect():
    transport = BrowserBridgeTransport(auto_start_daemon=True)
    with patch.object(transport.daemon, "is_port_open", return_value=True), \
         patch.object(transport.daemon, "is_running", return_value=False):
        with pytest.raises(
            BridgeDaemonUnavailableError,
            match=r"process khác.*chiếm",
        ):
            transport._ensure_daemon_ready()


def test_bridge_invalid_token_has_specific_remediation():
    from ctf_downloader.bridge.constants import BridgeMessageType
    from ctf_downloader.bridge.messages import serialize_message

    ws = _FakeHandshakeSocket(
        serialize_message(BridgeMessageType.ERROR, {"message": "Invalid token"})
    )
    transport = BrowserBridgeTransport(auto_start_daemon=False)
    with pytest.raises(BridgeAuthenticationError, match=r"ctf bridge token"):
        asyncio.run(transport._handshake(ws, "wrong"))


def test_bridge_local_ws_disables_proxy_and_bounds_open_timeout():
    transport = BrowserBridgeTransport(timeout=30)
    kwargs = transport._connect_kwargs()
    assert kwargs["proxy"] is None
    assert kwargs["open_timeout"] == 5.0
    assert kwargs["close_timeout"] == 2.0
    assert kwargs["max_size"] == 1024 * 1024


def test_streamed_bridge_protocol_error_closes_temp_file():
    import base64

    req = BridgeRequest(
        id="req_bad_chunk",
        method="GET",
        url="https://ctf.example/file.bin",
        binary=True,
    )
    ws = _ScriptedBridgeSocket([
        serialize_message(
            BridgeMessageType.HANDSHAKE_ACK,
            {"status": "ok", "extension_connected": True},
        ),
        serialize_message(
            BridgeMessageType.RESPONSE_START,
            {
                "id": req.id,
                "status_code": 200,
                "headers": {"content-type": "application/octet-stream"},
            },
        ),
        serialize_message(
            BridgeMessageType.RESPONSE_CHUNK,
            {"id": req.id, "seq": 0, "body": "%%%not-base64%%%"},
        ),
    ])
    temp = _FakeTempFile()
    transport = BrowserBridgeTransport(auto_start_daemon=False)
    with patch.object(transport, "_ensure_daemon_ready"), \
         patch("ctf_downloader.bridge.transport.websockets.connect", return_value=ws), \
         patch("ctf_downloader.bridge.transport.tempfile.TemporaryFile", return_value=temp):
        with pytest.raises(BridgeTransportError, match="invalid base64"):
            transport._dispatch_request(req, stream_response=True)

    assert temp.closed is True


def test_successful_stream_transfers_temp_file_ownership_to_response():
    import base64

    payload = b"streamed-bridge-body"
    req = BridgeRequest(
        id="req_good_chunk",
        method="GET",
        url="https://ctf.example/file.bin",
        binary=True,
    )
    ws = _ScriptedBridgeSocket([
        serialize_message(
            BridgeMessageType.HANDSHAKE_ACK,
            {"status": "ok", "extension_connected": True},
        ),
        serialize_message(
            BridgeMessageType.RESPONSE_START,
            {
                "id": req.id,
                "status_code": 200,
                "headers": {"content-type": "application/octet-stream"},
            },
        ),
        serialize_message(
            BridgeMessageType.RESPONSE_CHUNK,
            {
                "id": req.id,
                "seq": 0,
                "body": base64.b64encode(payload).decode("ascii"),
            },
        ),
        serialize_message(
            BridgeMessageType.RESPONSE_END,
            {"id": req.id, "bytes": len(payload)},
        ),
    ])
    temp = _FakeTempFile()
    transport = BrowserBridgeTransport(auto_start_daemon=False)
    with patch.object(transport, "_ensure_daemon_ready"), \
         patch("ctf_downloader.bridge.transport.websockets.connect", return_value=ws), \
         patch("ctf_downloader.bridge.transport.tempfile.TemporaryFile", return_value=temp):
        response = transport._dispatch_request(req, stream_response=True)

    assert response.body_file is temp
    assert temp.closed is False
    assert response.get_bytes() == payload
    temp.close()


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


def test_adaptive_session_surfaces_bridge_failure_once():
    from ctf_downloader.utils.logger import Logger

    session = AdaptiveSession(enable_bridge=True)
    failing = MagicMock()
    failing.send.side_effect = BridgeDaemonUnavailableError("daemon offline")
    session._bridge_transport = failing

    with patch.object(Logger, "warning") as warn:
        assert session._bridge_request("GET", "https://ctf.example/api") is None
        assert session._bridge_request("GET", "https://ctf.example/api") is None

    assert "BridgeDaemonUnavailableError" in (session.bridge_last_error or "")
    assert warn.call_count == 1
    message = warn.call_args.args[0]
    assert "ctf bridge status" in message
    assert "ctf bridge start" in message


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
