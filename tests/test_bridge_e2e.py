import asyncio
import base64
import json
import threading
import pytest
import websockets
from unittest.mock import patch

from ctf_downloader.bridge.constants import BridgeMessageType
from ctf_downloader.bridge.messages import deserialize_message, serialize_message
from ctf_downloader.bridge.server import BridgeServer
from ctf_downloader.bridge.transport import BrowserBridgeTransport
from ctf_downloader.utils.http_client import AdaptiveSession


class BackgroundServerThread(threading.Thread):
    def __init__(self, port, token):
        super().__init__(daemon=True)
        self.port = port
        self.token = token
        self.server = None
        self.loop = None
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = BridgeServer(host="127.0.0.1", port=self.port, token=self.token)

        async def _run():
            await self.server.start()
            self.ready_event.set()

            # Mock extension client inside server loop
            async def mock_extension():
                uri = f"ws://127.0.0.1:{self.port}/ws"
                async with websockets.connect(uri) as ws:
                    await ws.send(serialize_message(BridgeMessageType.HANDSHAKE, {
                        "token": self.token,
                        "client": "chrome-extension",
                        "version": "1.0.0"
                    }))
                    ack = json.loads(await ws.recv())
                    assert ack["type"] == "HANDSHAKE_ACK"

                    while not self.stop_event.is_set():
                        try:
                            msg_raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break

                        mtype, req_data = deserialize_message(msg_raw)
                        if mtype == BridgeMessageType.REQUEST_FORWARD:
                            req_id = req_data["id"]
                            url = req_data["url"]
                            if "api/v1/challenges" in url:
                                res_data = {
                                    "id": req_id,
                                    "status_code": 200,
                                    "status_text": "OK",
                                    "headers": {"content-type": "application/json"},
                                    "body": json.dumps({"success": True, "data": [{"id": 10, "name": "The 67th Line"}]}),
                                    "is_base64": False,
                                    "error": None,
                                }
                            elif "attachment.zip" in url:
                                # Deliberately exceed websockets' default 1 MiB
                                # message limit, then stream it in bounded chunks.
                                raw_zip = b"PK\x03\x04" + (bytes(range(256)) * 5000)
                                await ws.send(serialize_message(
                                    BridgeMessageType.RESPONSE_START,
                                    {
                                        "id": req_id,
                                        "status_code": 200,
                                        "status_text": "OK",
                                        "headers": {
                                            "content-type": "application/zip",
                                            "content-length": str(len(raw_zip)),
                                        },
                                        "is_base64": True,
                                        "error": None,
                                    },
                                ))
                                chunk_size = 128 * 1024
                                for seq, offset in enumerate(range(0, len(raw_zip), chunk_size)):
                                    chunk = raw_zip[offset:offset + chunk_size]
                                    await ws.send(serialize_message(
                                        BridgeMessageType.RESPONSE_CHUNK,
                                        {
                                            "id": req_id,
                                            "seq": seq,
                                            "body": base64.b64encode(chunk).decode("ascii"),
                                        },
                                    ))
                                await ws.send(serialize_message(
                                    BridgeMessageType.RESPONSE_END,
                                    {"id": req_id, "bytes": len(raw_zip)},
                                ))
                                continue
                            else:
                                res_data = {
                                    "id": req_id,
                                    "status_code": 200,
                                    "status_text": "OK",
                                    "headers": {"content-type": "text/html"},
                                    "body": "<html>CTF Page</html>",
                                    "is_base64": False,
                                    "error": None,
                                }
                            await ws.send(serialize_message(BridgeMessageType.RESPONSE_FORWARD, res_data))

            ext_task = asyncio.create_task(mock_extension())
            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)

            ext_task.cancel()
            await self.server.stop()

        self.loop.run_until_complete(_run())
        self.loop.close()

    def stop(self):
        self.stop_event.set()
        self.join(timeout=2.0)


def test_bridge_e2e_full_flow():
    port = 18898
    token = "secret_e2e_token_12345"
    srv_thread = BackgroundServerThread(port=port, token=token)
    srv_thread.start()
    assert srv_thread.ready_event.wait(timeout=3.0)

    # Wait for mock extension to connect
    import time
    time.sleep(0.3)

    transport = BrowserBridgeTransport(host="127.0.0.1", port=port, token=token, auto_start_daemon=False)
    session = AdaptiveSession(enable_bridge=True)
    session._bridge_transport = transport

    # Mock super().request returning 403 CF challenge so session routes to bridge
    import requests
    cf_resp = requests.Response()
    cf_resp.status_code = 403
    cf_resp.headers["Server"] = "cloudflare"
    cf_resp._content = b"<html><title>Just a moment...</title>cf-chl-bypass</html>"

    with patch.object(requests.Session, "send", return_value=cf_resp):
        # 1. API GET Request routed through Bridge
        resp = session.get("https://mirror-ctf.compfest.id/api/v1/challenges")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"][0]["name"] == "The 67th Line"

        # 2. Binary Download Request routed through Bridge
        bin_resp = session.get(
            "https://mirror-ctf.compfest.id/files/attachment.zip",
            stream=True,
        )
        assert bin_resp.status_code == 200
        assert bin_resp._content is False
        streamed = b"".join(bin_resp.iter_content(chunk_size=128 * 1024))
        assert streamed == b"PK\x03\x04" + (bytes(range(256)) * 5000)
        bin_resp.close()

    srv_thread.stop()
