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
async def test_cli_handshake_reports_extension_readiness():
    token = "readiness_token_123"
    server = BridgeServer(host="127.0.0.1", port=18897, token=token)
    await server.start()
    uri = "ws://127.0.0.1:18897/ws"

    async with websockets.connect(uri) as cli:
        await cli.send(
            serialize_message(
                BridgeMessageType.HANDSHAKE,
                {"token": token, "client": "cli-transport"},
            )
        )
        mtype, payload = deserialize_message(await cli.recv())
        assert mtype == BridgeMessageType.HANDSHAKE_ACK
        assert payload["extension_connected"] is False

    async with websockets.connect(uri) as extension:
        await extension.send(
            serialize_message(
                BridgeMessageType.HANDSHAKE,
                {"token": token, "client": "mock-extension"},
            )
        )
        await extension.recv()

        async with websockets.connect(uri) as cli:
            await cli.send(
                serialize_message(
                    BridgeMessageType.HANDSHAKE,
                    {"token": token, "client": "cli-transport"},
                )
            )
            mtype, payload = deserialize_message(await cli.recv())
            assert mtype == BridgeMessageType.HANDSHAKE_ACK
            assert payload["extension_connected"] is True

    await server.stop()


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



@pytest.mark.asyncio
async def test_server_rejects_web_origin_but_allows_extension_origin():
    token = "origin_token_123"
    server = BridgeServer(host="127.0.0.1", port=18895, token=token)
    await server.start()
    uri = "ws://127.0.0.1:18895/ws"

    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(uri, origin="https://evil.example"):
            pass

    extension_id = "abcdefghijklmnopabcdefghijklmnop"
    async with websockets.connect(
        uri, origin=f"chrome-extension://{extension_id}"
    ) as ws:
        await ws.send(
            serialize_message(
                BridgeMessageType.HANDSHAKE,
                {"token": token, "client": "chrome-extension"},
            )
        )
        raw = await ws.recv()
        mtype, payload = deserialize_message(raw)
        assert mtype == BridgeMessageType.HANDSHAKE_ACK
        assert payload["status"] == "ok"

    await server.stop()



@pytest.mark.asyncio
async def test_server_inprocess_chunked_response():
    import base64

    token = "chunk_token_123"
    server = BridgeServer(host="127.0.0.1", port=18896, token=token)
    await server.start()
    raw_payload = b"PK\x03\x04" + (bytes(range(256)) * 32)

    async def mock_extension():
        uri = "ws://127.0.0.1:18896/ws"
        async with websockets.connect(uri) as ws:
            await ws.send(
                serialize_message(
                    BridgeMessageType.HANDSHAKE,
                    {"token": token, "client": "mock"},
                )
            )
            await ws.recv()
            req_type, req_data = deserialize_message(await ws.recv())
            assert req_type == BridgeMessageType.REQUEST_FORWARD
            req_id = req_data["id"]

            await ws.send(
                serialize_message(
                    BridgeMessageType.RESPONSE_START,
                    {
                        "id": req_id,
                        "status_code": 200,
                        "status_text": "OK",
                        "headers": {"content-type": "application/zip"},
                        "is_base64": True,
                        "error": None,
                    },
                )
            )
            chunk_size = 1024
            for seq, off in enumerate(range(0, len(raw_payload), chunk_size)):
                chunk = raw_payload[off:off + chunk_size]
                await ws.send(
                    serialize_message(
                        BridgeMessageType.RESPONSE_CHUNK,
                        {
                            "id": req_id,
                            "seq": seq,
                            "body": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                )
            await ws.send(
                serialize_message(
                    BridgeMessageType.RESPONSE_END,
                    {"id": req_id, "bytes": len(raw_payload)},
                )
            )

    task = asyncio.create_task(mock_extension())
    for _ in range(20):
        if server.has_active_client:
            break
        await asyncio.sleep(0.05)

    response = await server.send_request(
        BridgeRequest(
            id="req_chunked_inprocess",
            method="GET",
            url="https://example.com/file.zip",
            binary=True,
        ),
        timeout_seconds=2.0,
    )
    assert response.status_code == 200
    assert response.get_bytes() == raw_payload

    await task
    await server.stop()



def test_daemon_inspect_status_distinguishes_stopped_owned_and_conflict(tmp_path):
    from unittest.mock import patch

    daemon = BridgeDaemon(
        token_path=str(tmp_path / "bridge_token"),
        pid_path=str(tmp_path / "bridge.pid"),
        port=18907,
    )
    with patch.object(daemon, "is_running", return_value=False), \
         patch.object(daemon, "is_port_open", return_value=False):
        stopped = daemon.inspect_status()
    assert stopped["port_conflict"] is False
    assert stopped["owned"] is False

    with patch.object(daemon, "is_running", return_value=False), \
         patch.object(daemon, "is_port_open", return_value=True):
        conflict = daemon.inspect_status()
    assert conflict["port_conflict"] is True
    assert conflict["owned"] is False

    with patch.object(daemon, "is_running", return_value=True), \
         patch.object(daemon, "read_pid", return_value=4242), \
         patch.object(daemon, "is_port_open", return_value=True):
        owned = daemon.inspect_status()
    assert owned["owned"] is True
    assert owned["port_conflict"] is False
    assert owned["pid"] == 4242


def test_daemon_missing_websockets_fails_before_spawn(tmp_path):
    from unittest.mock import patch

    daemon = BridgeDaemon(
        token_path=str(tmp_path / "bridge_token"),
        pid_path=str(tmp_path / "bridge.pid"),
        port=18906,
    )
    with patch(
        "ctf_downloader.bridge.daemon.importlib.util.find_spec",
        return_value=None,
    ), patch("ctf_downloader.bridge.daemon.subprocess.Popen") as popen:
        assert daemon.ensure_running() is False

    popen.assert_not_called()
    assert "websockets>=15" in (daemon.last_error or "")
    assert "requirements.txt" in (daemon.last_error or "")


def test_daemon_spawn_failure_records_actionable_error(tmp_path):
    from unittest.mock import patch

    daemon = BridgeDaemon(
        token_path=str(tmp_path / "bridge_token"),
        pid_path=str(tmp_path / "bridge.pid"),
        host="127.0.0.1",
        port=18902,
    )
    with patch.object(daemon, "is_port_open", return_value=False), \
         patch("ctf_downloader.bridge.daemon.subprocess.Popen",
               side_effect=PermissionError("exec denied")):
        assert daemon.ensure_running() is False
    assert "PermissionError" in (daemon.last_error or "")
    assert "spawn" in (daemon.last_error or "").lower()


def test_daemon_unreadable_existing_token_is_not_silently_replaced(tmp_path):
    from unittest.mock import patch
    from ctf_downloader.bridge.daemon import BridgeDaemonError

    token_path = tmp_path / "bridge_token"
    token_path.write_text("existing-secret", encoding="utf-8")
    daemon = BridgeDaemon(
        token_path=str(token_path),
        pid_path=str(tmp_path / "bridge.pid"),
        port=18903,
    )
    real_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(token_path) and args and "r" in str(args[0]):
            raise PermissionError("read denied")
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=guarded_open):
        with pytest.raises(BridgeDaemonError, match=r"Không đọc được Bridge token"):
            daemon.get_or_create_token()

    assert token_path.read_text(encoding="utf-8") == "existing-secret"


@pytest.mark.asyncio
async def test_inprocess_request_fails_immediately_when_extension_disconnects():
    token = "disconnect_inprocess_token"
    server = BridgeServer(host="127.0.0.1", port=18904, token=token)
    await server.start()

    async def extension():
        async with websockets.connect("ws://127.0.0.1:18904/ws") as ws:
            await ws.send(
                serialize_message(
                    BridgeMessageType.HANDSHAKE,
                    {"token": token, "client": "mock-extension"},
                )
            )
            await ws.recv()
            msg_type, _data = deserialize_message(await ws.recv())
            assert msg_type == BridgeMessageType.REQUEST_FORWARD
            # Exit context without a response: simulates browser tab/extension
            # disappearing after it accepted work.

    task = asyncio.create_task(extension())
    for _ in range(40):
        if server.has_active_client:
            break
        await asyncio.sleep(0.025)

    req = BridgeRequest(
        id="req_disconnect_inprocess",
        method="GET",
        url="https://example.com/file",
    )
    with pytest.raises(RuntimeError, match="Extension disconnected"):
        await asyncio.wait_for(
            server.send_request(req, timeout_seconds=10.0),
            timeout=2.0,
        )

    await task
    assert req.id not in server._pending_futures
    assert req.id not in server._chunked_pending
    assert req.id not in server._request_executors
    await server.stop()


@pytest.mark.asyncio
async def test_remote_cli_gets_error_when_extension_disconnects_mid_request():
    token = "disconnect_remote_token"
    server = BridgeServer(host="127.0.0.1", port=18905, token=token)
    await server.start()

    async def extension():
        async with websockets.connect("ws://127.0.0.1:18905/ws") as ws:
            await ws.send(
                serialize_message(
                    BridgeMessageType.HANDSHAKE,
                    {"token": token, "client": "mock-extension"},
                )
            )
            await ws.recv()
            msg_type, data = deserialize_message(await ws.recv())
            assert msg_type == BridgeMessageType.REQUEST_FORWARD
            assert data["id"] == "req_disconnect_remote"

    extension_task = asyncio.create_task(extension())
    for _ in range(40):
        if server.has_active_client:
            break
        await asyncio.sleep(0.025)

    async with websockets.connect("ws://127.0.0.1:18905/ws") as cli:
        await cli.send(
            serialize_message(
                BridgeMessageType.HANDSHAKE,
                {"token": token, "client": "cli-transport"},
            )
        )
        ack_type, ack = deserialize_message(await cli.recv())
        assert ack_type == BridgeMessageType.HANDSHAKE_ACK
        assert ack["extension_connected"] is True

        await cli.send(
            serialize_message(
                BridgeMessageType.REQUEST_FORWARD,
                {
                    "id": "req_disconnect_remote",
                    "method": "GET",
                    "url": "https://example.com/file",
                    "headers": {},
                },
            )
        )
        err_type, error = deserialize_message(
            await asyncio.wait_for(cli.recv(), timeout=2.0)
        )
        assert err_type == BridgeMessageType.ERROR
        assert error["id"] == "req_disconnect_remote"
        assert "Extension disconnected" in error["message"]

    await extension_task
    assert "req_disconnect_remote" not in server._request_sources
    assert "req_disconnect_remote" not in server._request_executors
    await server.stop()


def test_daemon_refuses_foreign_process_on_bridge_port(tmp_path):
    import socket
    from unittest.mock import patch

    token_file = str(tmp_path / "bridge_token")
    pid_file = str(tmp_path / "bridge.pid")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    daemon = BridgeDaemon(
        token_path=token_file,
        pid_path=pid_file,
        host="127.0.0.1",
        port=port,
    )
    try:
        with patch("ctf_downloader.bridge.daemon.subprocess.Popen") as popen:
            assert daemon.ensure_running() is False
            popen.assert_not_called()
    finally:
        listener.close()



def test_daemon_rejects_reused_pid_and_never_stops_unrelated_process(tmp_path):
    import os
    from unittest.mock import patch

    token_file = str(tmp_path / "bridge_token")
    pid_file = str(tmp_path / "bridge.pid")
    daemon = BridgeDaemon(token_path=token_file, pid_path=pid_file, port=18901)

    signals = []

    def fake_kill(pid, sig):
        assert pid == os.getpid()
        signals.append(sig)
        # Signal 0 is the harmless liveness probe. Never deliver SIGTERM from
        # this regression even while the implementation is still RED.
        return None

    # The current pytest process is alive but is definitely not the bridge
    # runner. A stale PID file must not make it look like a bridge daemon.
    daemon._write_pid(os.getpid())
    with patch("ctf_downloader.bridge.daemon.os.kill", side_effect=fake_kill):
        assert daemon.is_running() is False
    assert signals == [0]
    assert daemon.read_pid() is None

    # Recreate the stale file and verify stop() refuses to signal that PID.
    signals.clear()
    daemon._write_pid(os.getpid())
    with patch("ctf_downloader.bridge.daemon.os.kill", side_effect=fake_kill):
        assert daemon.stop() is False
    assert signals == [0]
    assert daemon.read_pid() is None



def test_daemon_pid_signature_accepts_real_runner_argv(tmp_path):
    import io
    from unittest.mock import patch

    daemon = BridgeDaemon(
        token_path=str(tmp_path / "bridge_token"),
        pid_path=str(tmp_path / "bridge.pid"),
        port=18888,
    )
    argv = b"\0".join(
        [
            b"/usr/bin/python3",
            b"-m",
            b"ctf_downloader.bridge.runner",
            b"--host",
            b"127.0.0.1",
            b"--port",
            b"18888",
            b"--token",
            b"secret",
        ]
    ) + b"\0"

    with patch("builtins.open", return_value=io.BytesIO(argv)):
        assert daemon._pid_matches_bridge(12345) is True
