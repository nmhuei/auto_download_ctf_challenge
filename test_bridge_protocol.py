import pytest
import base64
from ctf_downloader.bridge.constants import (
    DEFAULT_BRIDGE_PORT,
    DEFAULT_BRIDGE_HOST,
    BridgeMessageType,
    TOKEN_FILE_NAME,
    PID_FILE_NAME,
)
from ctf_downloader.bridge.messages import (
    BridgeRequest,
    BridgeResponse,
    serialize_message,
    deserialize_message,
)

def test_bridge_constants():
    assert DEFAULT_BRIDGE_PORT == 18888
    assert DEFAULT_BRIDGE_HOST == "127.0.0.1"
    assert TOKEN_FILE_NAME == "bridge_token"
    assert PID_FILE_NAME == "bridge.pid"
    assert BridgeMessageType.HANDSHAKE.value == "HANDSHAKE"
    assert BridgeMessageType.HANDSHAKE_ACK.value == "HANDSHAKE_ACK"
    assert BridgeMessageType.REQUEST_FORWARD.value == "REQUEST_FORWARD"
    assert BridgeMessageType.RESPONSE_FORWARD.value == "RESPONSE_FORWARD"
    assert BridgeMessageType.COOKIE_UPDATE.value == "COOKIE_UPDATE"
    assert BridgeMessageType.PING.value == "PING"
    assert BridgeMessageType.PONG.value == "PONG"
    assert BridgeMessageType.ERROR.value == "ERROR"

def test_request_serialization_roundtrip():
    req = BridgeRequest(
        id="req_123",
        method="POST",
        url="https://mirror-ctf.compfest.id/api/v1/challenges/attempt",
        headers={"Content-Type": "application/json", "Authorization": "Bearer abc"},
        body='{"challenge_id": 4, "submission": "FLAG{test}"}',
        timeout_ms=5000,
        binary=False,
    )
    raw = serialize_message(BridgeMessageType.REQUEST_FORWARD, req.to_dict())
    mtype, data = deserialize_message(raw)
    assert mtype == BridgeMessageType.REQUEST_FORWARD
    assert data["id"] == "req_123"
    assert data["method"] == "POST"
    assert data["url"] == "https://mirror-ctf.compfest.id/api/v1/challenges/attempt"
    assert data["headers"]["Content-Type"] == "application/json"
    assert data["body"] == '{"challenge_id": 4, "submission": "FLAG{test}"}'
    assert data["binary"] is False

    rebuilt = BridgeRequest.from_dict(data)
    assert rebuilt.id == req.id
    assert rebuilt.method == req.method
    assert rebuilt.url == req.url
    assert rebuilt.headers == req.headers
    assert rebuilt.body == req.body

def test_response_serialization_with_binary():
    raw_bytes = b"PK\x03\x04\x14\x00\x00\x00\x08\x00_test_zip_payload"
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    res = BridgeResponse(
        id="req_bin_1",
        status_code=200,
        status_text="OK",
        headers={"content-type": "application/zip", "content-length": str(len(raw_bytes))},
        body=b64_str,
        is_base64=True,
        error=None,
    )
    raw = serialize_message(BridgeMessageType.RESPONSE_FORWARD, res.to_dict())
    mtype, data = deserialize_message(raw)
    assert mtype == BridgeMessageType.RESPONSE_FORWARD
    assert data["status_code"] == 200
    assert data["is_base64"] is True

    rebuilt = BridgeResponse.from_dict(data)
    assert rebuilt.get_bytes() == raw_bytes

def test_deserialize_invalid_message():
    with pytest.raises(ValueError):
        deserialize_message("not valid json")
    
    with pytest.raises(ValueError):
        deserialize_message('{"type": "UNKNOWN_TYPE", "data": {}}')
