import asyncio
import json
import pytest
import websockets

from ctf_downloader.bridge.constants import BridgeMessageType
from ctf_downloader.bridge.messages import (
    BridgeRequest,
    deserialize_message,
    serialize_message,
)
from ctf_downloader.bridge.server import BridgeServer
from ctf_downloader.bridge.daemon import BridgeDaemon


@pytest.mark.asyncio
async def test_server_handshake_and_request_forward():
    token = "test_secret_token_12345"
    server = BridgeServer(host="127.0.0.1", port=18891, token=token)
    await server.start()
    assert server.is_running
    assert not server.has_active_client

    async def mock_extension():
        uri = "ws://127.0.0.1:18891/ws"
        async with websockets.connect(uri) as ws:
            # 1. Send Handshake
            await ws.send(serialize_message(BridgeMessageType.HANDSHAKE, {"token": token, "client": "mock"}))
            ack_raw = await ws.recv()
            mtype, ack_data = deserialize_message(ack_raw)
            assert mtype == BridgeMessageType.HANDSHAKE_ACK
            assert ack_data.get("status") == "ok"

            # 2. Wait for Request Forward
            req_raw = await ws.recv()
            mtype, req_data = deserialize_message(req_raw)
            assert mtype == BridgeMessageType.REQUEST_FORWARD
            req_id = req_data["id"]

            # 3. Reply with Response Forward
            res_payload = {
                "id": req_id,
                "status_code": 200,
                "status_text": "OK",
                "headers": {"content-type": "application/json"},
                "body": '{"result": "ok"}',
                "is_base64": False,
                "error": None,
            }
            await ws.send(serialize_message(BridgeMessageType.RESPONSE_FORWARD, res_payload))
            await asyncio.sleep(0.1)

    client_task = asyncio.create_task(mock_extension())
    # Give connection a tick
    for _ in range(20):
        if server.has_active_client:
            break
        await asyncio.sleep(0.05)

    assert server.has_active_client

    req = BridgeRequest(id="req_test_1", method="GET", url="https://example.com/api/test")
    resp = await server.send_request(req, timeout_seconds=5.0)

    assert resp.status_code == 200
    assert resp.body == '{"result": "ok"}'

    await client_task
    await server.stop()
    assert not server.is_running


@pytest.mark.asyncio
async def test_server_invalid_handshake():
    token = "correct_token_123"
    server = BridgeServer(host="127.0.0.1", port=18892, token=token)
    await server.start()

    uri = "ws://127.0.0.1:18892/ws"
    async with websockets.connect(uri) as ws:
        # Send bad token
        await ws.send(serialize_message(BridgeMessageType.HANDSHAKE, {"token": "wrong_token"}))
        ack_raw = await ws.recv()
        mtype, ack_data = deserialize_message(ack_raw)
        assert mtype == BridgeMessageType.ERROR
        assert "Invalid token" in ack_data.get("message", "")

    await server.stop()


@pytest.mark.asyncio
async def test_server_request_timeout_when_no_client():
    server = BridgeServer(host="127.0.0.1", port=18893, token="tok")
    await server.start()

    req = BridgeRequest(id="req_no_client", method="GET", url="https://example.com/api")
    with pytest.raises(RuntimeError) as exc_info:
        await server.send_request(req, timeout_seconds=0.5)

    assert "No extension client connected" in str(exc_info.value)
    await server.stop()


def test_daemon_token_and_pid_lifecycle(tmp_path):
    token_file = str(tmp_path / "bridge_token")
    pid_file = str(tmp_path / "bridge.pid")

    daemon = BridgeDaemon(token_path=token_file, pid_path=pid_file, port=18894)
    token = daemon.get_or_create_token()
    assert len(token) >= 32
    assert daemon.get_or_create_token() == token

    daemon._write_pid(12345)
    assert daemon.read_pid() == 12345
    daemon._clear_pid()
    assert daemon.read_pid() is None
