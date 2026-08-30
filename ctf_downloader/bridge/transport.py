"""BrowserBridgeTransport: Transport adapter for sending requests via Browser Extension Bridge."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional
import requests
from requests.structures import CaseInsensitiveDict
import urllib.parse
import websockets

from .constants import DEFAULT_BRIDGE_HOST, DEFAULT_BRIDGE_PORT, BridgeMessageType
from .daemon import BridgeDaemon
from .messages import BridgeRequest, BridgeResponse, deserialize_message, serialize_message

logger = logging.getLogger(__name__)


class BrowserBridgeTransport:
    """Dispatches HTTP requests through local BridgeServer to the Browser Extension."""

    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        token: Optional[str] = None,
        auto_start_daemon: bool = True,
        timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.auto_start_daemon = auto_start_daemon
        self.timeout = timeout
        self.daemon = BridgeDaemon(host=host, port=port)

    def _dispatch_request(self, bridge_req: BridgeRequest) -> BridgeResponse:
        """Connect as a transient client to BridgeServer or direct send."""
        if self.auto_start_daemon and not self.daemon.is_port_open():
            self.daemon.ensure_running()

        token = self.token or self.daemon.get_or_create_token()

        async def _async_dispatch() -> BridgeResponse:
            uri = f"ws://{self.host}:{self.port}/ws"
            async with websockets.connect(uri) as ws:
                # 1. Handshake as internal CLI client
                handshake_data = {"token": token, "client": "cli-transport"}
                await ws.send(serialize_message(BridgeMessageType.HANDSHAKE, handshake_data))
                ack_raw = await ws.recv()
                msg_type, ack_data = deserialize_message(str(ack_raw))
                if msg_type != BridgeMessageType.HANDSHAKE_ACK:
                    raise RuntimeError(f"Bridge handshake failed: {ack_data}")

                # 2. Forward request through server to extension
                await ws.send(serialize_message(BridgeMessageType.REQUEST_FORWARD, bridge_req.to_dict()))

                # 3. Wait for response
                deadline = time.time() + (bridge_req.timeout_ms / 1000.0)
                while time.time() < deadline:
                    raw_res = await asyncio.wait_for(ws.recv(), timeout=bridge_req.timeout_ms / 1000.0)
                    mtype, res_data = deserialize_message(str(raw_res))
                    if mtype == BridgeMessageType.RESPONSE_FORWARD:
                        if res_data.get("id") == bridge_req.id:
                            return BridgeResponse.from_dict(res_data)
                    elif mtype == BridgeMessageType.ERROR:
                        raise RuntimeError(f"Bridge error: {res_data.get('message')}")

                raise TimeoutError(f"Timeout waiting for bridge response for {bridge_req.url}")

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(_async_dispatch()))
            return future.result()

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        """Convert requests.PreparedRequest -> BridgeRequest, dispatch, and return requests.Response."""
        req_id = f"req_{uuid.uuid4().hex[:12]}"
        method = str(request.method or "GET").upper()
        url = str(request.url or "")
        headers = dict(request.headers or {})

        # Determine if binary is requested
        is_binary = kwargs.get("binary", False)
        if not is_binary and any(ext in url.lower() for ext in (".zip", ".7z", ".tar", ".gz", ".exe", ".bin", ".raw", ".png", ".jpg")):
            is_binary = True

        body_str: Optional[str] = None
        if request.body:
            if isinstance(request.body, bytes):
                body_str = request.body.decode("utf-8", errors="replace")
            else:
                body_str = str(request.body)

        timeout_sec = kwargs.get("timeout", self.timeout)
        timeout_ms = int((timeout_sec if isinstance(timeout_sec, (int, float)) else self.timeout) * 1000)

        bridge_req = BridgeRequest(
            id=req_id,
            method=method,
            url=url,
            headers=headers,
            body=body_str,
            timeout_ms=timeout_ms,
            binary=is_binary,
        )

        bridge_res = self._dispatch_request(bridge_req)

        # Build requests.Response
        resp = requests.Response()
        resp.status_code = bridge_res.status_code
        resp.reason = bridge_res.status_text
        resp.url = url
        resp.request = request
        resp.headers = CaseInsensitiveDict(bridge_res.headers)

        content = bridge_res.get_bytes()
        resp._content = content
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers) or "utf-8"

        # If cookies in headers, update jar
        if "set-cookie" in resp.headers:
            cookie_headers = [v for k, v in bridge_res.headers.items() if k.lower() == "set-cookie"]
            for ch in cookie_headers:
                requests.cookies.extract_cookies_to_jar(resp.cookies, request, resp)

        return resp
