"""BrowserBridgeTransport: Transport adapter for sending requests via Browser Extension Bridge."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Any, Dict, Optional
import requests
from requests.structures import CaseInsensitiveDict
import urllib.parse
import websockets

from .constants import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    BridgeMessageType,
    get_bridge_cache_dir,
)
from .daemon import BridgeDaemon
from .messages import BridgeRequest, BridgeResponse, deserialize_message, serialize_message

logger = logging.getLogger(__name__)


class BridgeTransportError(RuntimeError):
    """Base class for actionable local Browser Bridge failures."""


class BridgeDaemonUnavailableError(BridgeTransportError):
    """The local daemon isn't reachable or couldn't be started."""


class BridgeAuthenticationError(BridgeTransportError):
    """CLI and daemon token state don't match."""


class BridgeExtensionUnavailableError(BridgeTransportError):
    """Daemon is alive, but no browser extension client is connected."""


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

    def _ensure_daemon_ready(self) -> None:
        """Fail early with a useful diagnosis instead of ConnectionRefusedError."""
        port_open = self.daemon.is_port_open()
        if port_open:
            if self.auto_start_daemon and not self.daemon.is_running():
                raise BridgeDaemonUnavailableError(
                    f"Bridge port {self.host}:{self.port} đang bị process khác "
                    "chiếm; daemon được tool quản lý không sở hữu port này. "
                    "Đổi port hoặc dừng process đang chiếm port."
                )
            return

        if self.auto_start_daemon:
            if self.daemon.ensure_running():
                return
            detail = self.daemon.last_error or "không có chi tiết từ daemon"
            raise BridgeDaemonUnavailableError(
                "Không khởi chạy được Browser Bridge daemon: "
                f"{detail} Chạy 'ctf bridge status' để xem chẩn đoán; "
                "sau khi xử lý nguyên nhân, thử lại bằng 'ctf bridge start'."
            )

        raise BridgeDaemonUnavailableError(
            "Browser Bridge daemon chưa chạy. Chạy 'ctf bridge start' trước "
            "hoặc bật auto_start_daemon."
        )

    async def _handshake(self, websocket: Any, token: str) -> Dict[str, Any]:
        handshake_data = {"token": token, "client": "cli-transport"}
        await websocket.send(
            serialize_message(BridgeMessageType.HANDSHAKE, handshake_data)
        )
        ack_raw = await websocket.recv()
        msg_type, ack_data = deserialize_message(str(ack_raw))
        if msg_type == BridgeMessageType.ERROR:
            message = str(ack_data.get("message") or "Bridge handshake rejected")
            if "token" in message.lower():
                raise BridgeAuthenticationError(
                    "Bridge token không khớp daemon. Chạy 'ctf bridge token' "
                    "và cập nhật token trong Browser Extension."
                )
            raise BridgeTransportError(f"Bridge handshake failed: {message}")
        if msg_type != BridgeMessageType.HANDSHAKE_ACK:
            raise BridgeTransportError(
                f"Bridge handshake failed: unexpected {msg_type.value}"
            )
        return ack_data

    def _connect_kwargs(self) -> Dict[str, Any]:
        # Local-only WS should never inherit an HTTP proxy. Bound the handshake
        # and close time independently from the potentially longer HTTP request.
        return {
            "open_timeout": min(max(float(self.timeout), 1.0), 5.0),
            "close_timeout": 2.0,
            "ping_interval": 20.0,
            "ping_timeout": 10.0,
            "max_size": 1024 * 1024,
            "proxy": None,
        }

    def probe(self) -> Dict[str, Any]:
        """Return daemon/extension readiness without forwarding an HTTP request."""
        self._ensure_daemon_ready()
        token = self.token or self.daemon.get_or_create_token()

        async def _probe() -> Dict[str, Any]:
            uri = f"ws://{self.host}:{self.port}/ws"
            try:
                async with websockets.connect(uri, **self._connect_kwargs()) as ws:
                    ack = await self._handshake(ws, token)
            except BridgeTransportError:
                raise
            except (OSError, asyncio.TimeoutError) as exc:
                raise BridgeDaemonUnavailableError(
                    f"Không kết nối được Bridge daemon tại {uri}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            return {
                "daemon_running": True,
                "extension_connected": bool(ack.get("extension_connected")),
                "host": self.host,
                "port": self.port,
            }

        return asyncio.run(_probe())

    def _dispatch_request(
        self, bridge_req: BridgeRequest, *, stream_response: bool = False
    ) -> BridgeResponse:
        """Connect as a transient client to BridgeServer or direct send."""
        self._ensure_daemon_ready()
        token = self.token or self.daemon.get_or_create_token()

        async def _async_dispatch() -> BridgeResponse:
            uri = f"ws://{self.host}:{self.port}/ws"
            websocket_ctx = websockets.connect(
                uri, **self._connect_kwargs()
            )
            async with websocket_ctx as ws:
                # 1. Handshake as internal CLI client.
                ack_data = await self._handshake(ws, token)
                if ack_data.get("extension_connected") is False:
                    raise BridgeExtensionUnavailableError(
                        "Bridge daemon đang chạy nhưng Browser Extension "
                        "chưa kết nối. Mở browser đã cài CTF Bridge Extension "
                        "và kiểm tra token extension."
                    )

                # 2. Forward request through server to extension
                await ws.send(serialize_message(BridgeMessageType.REQUEST_FORWARD, bridge_req.to_dict()))

                # 3. Wait for response. Binary payloads may arrive as a
                # bounded START -> CHUNK* -> END sequence so no individual
                # WebSocket JSON frame exceeds the library's default 1 MiB cap.
                deadline = time.monotonic() + (bridge_req.timeout_ms / 1000.0)
                chunk_meta: Optional[Dict[str, Any]] = None
                chunk_data = bytearray()
                chunk_file = None
                received_bytes = 0
                next_seq = 0

                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Timeout waiting for bridge response for {bridge_req.url}"
                            )

                        raw_res = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        mtype, res_data = deserialize_message(str(raw_res))
                        if res_data.get("id") not in (None, bridge_req.id):
                            continue

                        if mtype == BridgeMessageType.RESPONSE_FORWARD:
                            return BridgeResponse.from_dict(res_data)

                        if mtype == BridgeMessageType.RESPONSE_START:
                            chunk_meta = dict(res_data)
                            chunk_data.clear()
                            received_bytes = 0
                            next_seq = 0
                            if chunk_file is not None:
                                chunk_file.close()
                                chunk_file = None
                            if stream_response:
                                cache_dir = get_bridge_cache_dir()
                                os.makedirs(cache_dir, mode=0o700, exist_ok=True)
                                chunk_file = tempfile.TemporaryFile(
                                    mode="w+b", dir=cache_dir
                                )
                            continue

                        if mtype == BridgeMessageType.RESPONSE_CHUNK:
                            if chunk_meta is None:
                                raise RuntimeError("Bridge chunk received before RESPONSE_START")
                            try:
                                seq = int(str(res_data.get("seq")))
                            except (TypeError, ValueError) as exc:
                                raise RuntimeError("Bridge chunk has invalid sequence") from exc
                            if seq != next_seq:
                                raise RuntimeError(
                                    f"Bridge chunk out of order: expected {next_seq}, got {seq}"
                                )
                            encoded = res_data.get("body")
                            if not isinstance(encoded, str):
                                raise RuntimeError("Bridge chunk body must be base64 text")
                            try:
                                decoded = base64.b64decode(encoded, validate=True)
                            except Exception as exc:
                                raise RuntimeError("Bridge chunk contains invalid base64") from exc
                            received_bytes += len(decoded)
                            if chunk_file is not None:
                                chunk_file.write(decoded)
                            else:
                                chunk_data.extend(decoded)
                            next_seq += 1
                            continue

                        if mtype == BridgeMessageType.RESPONSE_END:
                            if chunk_meta is None:
                                raise RuntimeError("Bridge end received before RESPONSE_START")
                            expected_bytes = res_data.get("bytes")
                            if expected_bytes is not None:
                                try:
                                    expected = int(expected_bytes)
                                except (TypeError, ValueError) as exc:
                                    raise RuntimeError("Bridge end has invalid byte count") from exc
                                if expected != received_bytes:
                                    raise RuntimeError(
                                        "Bridge binary length mismatch: "
                                        f"expected {expected}, got {received_bytes}"
                                    )
                            if chunk_file is not None:
                                chunk_file.flush()
                                chunk_file.seek(0)
                                response_file = chunk_file
                                # Ownership moves to requests.Response.raw. The
                                # failure cleanup below must not close a successful
                                # streamed response before the downloader reads it.
                                chunk_file = None
                                return BridgeResponse(
                                    id=bridge_req.id,
                                    status_code=int(chunk_meta.get("status_code", 0)),
                                    status_text=str(chunk_meta.get("status_text", "OK")),
                                    headers=dict(chunk_meta.get("headers") or {}),
                                    body=None,
                                    is_base64=False,
                                    error=chunk_meta.get("error"),
                                    body_file=response_file,
                                )
                            return BridgeResponse(
                                id=bridge_req.id,
                                status_code=int(chunk_meta.get("status_code", 0)),
                                status_text=str(chunk_meta.get("status_text", "OK")),
                                headers=dict(chunk_meta.get("headers") or {}),
                                body=None,
                                is_base64=False,
                                error=chunk_meta.get("error"),
                                body_bytes=bytes(chunk_data),
                            )

                        if mtype == BridgeMessageType.ERROR:
                            message = str(res_data.get("message") or "unknown Bridge error")
                            lower = message.lower()
                            if "extension" in lower and (
                                "disconnect" in lower or "no extension" in lower
                            ):
                                raise BridgeExtensionUnavailableError(message)
                            raise BridgeTransportError(f"Bridge error: {message}")
                finally:
                    if chunk_file is not None:
                        try:
                            chunk_file.close()
                        except Exception:
                            pass
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(_async_dispatch()))
            try:
                return future.result()
            except BridgeTransportError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                raise BridgeDaemonUnavailableError(
                    "Bridge WebSocket đóng giữa request; daemon có thể đã dừng "
                    "hoặc restart. Chạy 'ctf bridge status' rồi thử lại."
                ) from exc
            except TimeoutError as exc:
                raise BridgeTransportError(
                    f"Bridge response timeout cho {bridge_req.url}: {exc}"
                ) from exc
            except OSError as exc:
                raise BridgeDaemonUnavailableError(
                    f"Bridge socket lỗi: {type(exc).__name__}: {exc}"
                ) from exc
            except RuntimeError as exc:
                raise BridgeTransportError(
                    f"Bridge protocol error: {exc}"
                ) from exc

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        """Convert requests.PreparedRequest -> BridgeRequest, dispatch, and return requests.Response."""
        req_id = f"req_{uuid.uuid4().hex[:12]}"
        method = str(request.method or "GET").upper()
        url = str(request.url or "")
        headers = dict(request.headers or {})

        # stream=True is used by the downloader for attachment bodies even
        # when the URL has no useful extension. Treat it as binary so the
        # extension forwards raw bytes through bounded chunk frames.
        stream_response = bool(kwargs.get("stream", False))
        is_binary = bool(kwargs.get("binary", False) or stream_response)
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

        bridge_res = self._dispatch_request(
            bridge_req, stream_response=stream_response
        )

        # Build requests.Response
        resp = requests.Response()
        resp.status_code = bridge_res.status_code
        resp.reason = bridge_res.status_text
        resp.url = url
        resp.request = request
        resp.headers = CaseInsensitiveDict(bridge_res.headers)

        if bridge_res.body_file is not None:
            resp.raw = bridge_res.body_file
            setattr(resp, "_content", False)
            setattr(resp, "_content_consumed", False)
        else:
            resp._content = bridge_res.get_bytes()
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers) or "utf-8"

        # If cookies in headers, update jar
        if "set-cookie" in resp.headers:
            cookie_headers = [v for k, v in bridge_res.headers.items() if k.lower() == "set-cookie"]
            for ch in cookie_headers:
                requests.cookies.extract_cookies_to_jar(resp.cookies, request, resp)

        return resp
