"""WebSocket Bridge Server for routing HTTP requests through browser extension."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any, Callable, Dict, Optional, Set

try:
    from websockets.asyncio.server import Server as WebSocketServer, serve
except ImportError:
    from websockets.server import WebSocketServer, serve  # type: ignore

import websockets

from .constants import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    BridgeMessageType,
)
from .messages import (
    BridgeRequest,
    BridgeResponse,
    deserialize_message,
    serialize_message,
)

logger = logging.getLogger(__name__)

# Browser WebSocket handshakes carry the extension origin. CLI transport
# connections created by the local Python client don't send Origin, so None is
# intentionally allowed while ordinary web origins are rejected at handshake.
_EXTENSION_ORIGIN_RE = re.compile(
    r"^(?:chrome-extension://[a-p]{32}|moz-extension://[0-9a-fA-F-]{16,64})$"
)
_ALLOWED_ORIGINS = [None, _EXTENSION_ORIGIN_RE]


class BridgeServer:
    """Asyncio WebSocket Server that acts as a local RPC bridge to Chrome Extension."""

    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        token: Optional[str] = None,
        on_cookie_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.on_cookie_update = on_cookie_update
        self._server: Optional[Any] = None
        self._extension_clients: Set[Any] = set()
        self._cli_clients: Set[Any] = set()
        self._pending_futures: Dict[str, asyncio.Future[BridgeResponse]] = {}
        self._chunked_pending: Dict[str, Dict[str, Any]] = {}
        self._request_sources: Dict[str, Any] = {}
        # Track which extension actually owns each forwarded request. Without
        # this, an extension disconnect leaves CLI/in-process callers waiting
        # until their full HTTP timeout and leaks request bookkeeping.
        self._request_executors: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        if self._server is None:
            return False
        if hasattr(self._server, "is_serving"):
            return self._server.is_serving()
        return True

    @property
    def has_active_client(self) -> bool:
        return len(self._extension_clients) > 0

    async def start(self) -> None:
        """Start the WebSocket server on host:port."""
        if self.is_running:
            return

        self._server = await serve(
            self._handle_client,
            self.host,
            self.port,
            origins=_ALLOWED_ORIGINS,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info(f"BridgeServer started on ws://{self.host}:{self.port}/ws")

    async def stop(self) -> None:
        """Stop the WebSocket server and cancel pending requests."""
        if self._server:
            self._server.close()
            if hasattr(self._server, "wait_closed"):
                await self._server.wait_closed()
            self._server = None

        async with self._lock:
            for req_id, fut in list(self._pending_futures.items()):
                if not fut.done():
                    fut.cancel()
            self._pending_futures.clear()
            self._chunked_pending.clear()
            self._request_sources.clear()
            self._request_executors.clear()
            self._extension_clients.clear()
            self._cli_clients.clear()

    async def _cleanup_client_requests(
        self, websocket: Any, client_type: str
    ) -> None:
        """Resolve or discard request state owned by a disconnected client."""
        notifications = []
        async with self._lock:
            if client_type == "cli-transport":
                abandoned = [
                    req_id
                    for req_id, source in self._request_sources.items()
                    if source is websocket
                ]
                for req_id in abandoned:
                    self._request_sources.pop(req_id, None)
                    self._request_executors.pop(req_id, None)
                    self._chunked_pending.pop(req_id, None)
                return

            affected = [
                req_id
                for req_id, executor in self._request_executors.items()
                if executor is websocket
            ]
            for req_id in affected:
                self._request_executors.pop(req_id, None)
                self._chunked_pending.pop(req_id, None)
                fut = self._pending_futures.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_exception(
                        RuntimeError(
                            "Browser Extension disconnected while processing "
                            f"Bridge request {req_id}"
                        )
                    )
                source = self._request_sources.pop(req_id, None)
                if source is not None:
                    notifications.append((source, req_id))

        # Never await another socket while holding the server state lock.
        for source, req_id in notifications:
            try:
                await source.send(
                    serialize_message(
                        BridgeMessageType.ERROR,
                        {
                            "id": req_id,
                            "message": (
                                "Browser Extension disconnected while processing "
                                f"Bridge request {req_id}"
                            ),
                        },
                    )
                )
            except Exception:
                pass

    async def _handle_client(self, websocket: Any) -> None:
        """Handle incoming WebSocket connection from browser extension or CLI client."""
        authenticated = False
        client_type = "extension"
        try:
            async for raw_message in websocket:
                try:
                    msg_type, data = deserialize_message(str(raw_message))
                except Exception as exc:
                    err_msg = serialize_message(
                        BridgeMessageType.ERROR, {"message": f"Malformed envelope: {exc}"}
                    )
                    await websocket.send(err_msg)
                    continue

                if msg_type == BridgeMessageType.HANDSHAKE:
                    client_token = data.get("token")
                    if self.token and client_token != self.token:
                        err_msg = serialize_message(
                            BridgeMessageType.ERROR, {"message": "Invalid token"}
                        )
                        await websocket.send(err_msg)
                        await websocket.close(code=4001, reason="Authentication failed")
                        return

                    authenticated = True
                    client_type = data.get("client", "extension")
                    async with self._lock:
                        if client_type == "cli-transport":
                            self._cli_clients.add(websocket)
                        else:
                            self._extension_clients.add(websocket)

                    ack_payload = {
                        "status": "ok",
                        "version": "3.0.0",
                        "client_type": client_type,
                    }
                    if client_type == "cli-transport":
                        ack_payload["extension_connected"] = bool(
                            self._extension_clients
                        )
                    ack_msg = serialize_message(
                        BridgeMessageType.HANDSHAKE_ACK,
                        ack_payload,
                    )
                    await websocket.send(ack_msg)

                elif not authenticated:
                    err_msg = serialize_message(
                        BridgeMessageType.ERROR, {"message": "Unauthenticated"}
                    )
                    await websocket.send(err_msg)
                    await websocket.close(code=4002, reason="Handshake required")
                    return

                elif msg_type == BridgeMessageType.REQUEST_FORWARD:
                    # Request coming from CLI client over WS.
                    req_id = data.get("id")
                    executor = None
                    async with self._lock:
                        if self._extension_clients:
                            executor = next(iter(self._extension_clients))
                            if req_id:
                                self._request_sources[req_id] = websocket
                                self._request_executors[req_id] = executor

                    if executor is None:
                        await websocket.send(
                            serialize_message(
                                BridgeMessageType.ERROR,
                                {
                                    "id": req_id,
                                    "message": (
                                        "No extension client connected to "
                                        "BridgeServer."
                                    ),
                                },
                            )
                        )
                        continue

                    # Forward to extension outside the state lock. The extension
                    # may disappear between selection and send; fail this
                    # request immediately instead of dropping the CLI socket.
                    req_envelope = serialize_message(
                        BridgeMessageType.REQUEST_FORWARD, data
                    )
                    try:
                        await executor.send(req_envelope)
                    except websockets.exceptions.ConnectionClosed:
                        async with self._lock:
                            if req_id:
                                self._request_sources.pop(req_id, None)
                                self._request_executors.pop(req_id, None)
                        await websocket.send(
                            serialize_message(
                                BridgeMessageType.ERROR,
                                {
                                    "id": req_id,
                                    "message": (
                                        "Browser Extension disconnected before "
                                        "the Bridge request could be forwarded."
                                    ),
                                },
                            )
                        )

                elif msg_type == BridgeMessageType.RESPONSE_FORWARD:
                    req_id = data.get("id")
                    if req_id:
                        async with self._lock:
                            # 1. In-process future
                            fut = self._pending_futures.get(req_id)
                            if fut and not fut.done():
                                resp = BridgeResponse.from_dict(data)
                                fut.set_result(resp)

                            # 2. Remote CLI client WebSocket
                            source_ws = self._request_sources.pop(req_id, None)
                            self._request_executors.pop(req_id, None)

                        if source_ws:
                            try:
                                res_envelope = serialize_message(
                                    BridgeMessageType.RESPONSE_FORWARD, data
                                )
                                await source_ws.send(res_envelope)
                            except Exception:
                                pass

                elif msg_type in (
                    BridgeMessageType.RESPONSE_START,
                    BridgeMessageType.RESPONSE_CHUNK,
                    BridgeMessageType.RESPONSE_END,
                ):
                    req_id = data.get("id")
                    if req_id:
                        async with self._lock:
                            source_ws = self._request_sources.get(req_id)
                            fut = self._pending_futures.get(req_id)

                            if fut and not fut.done():
                                try:
                                    if msg_type == BridgeMessageType.RESPONSE_START:
                                        self._chunked_pending[req_id] = {
                                            "meta": dict(data),
                                            "buffer": bytearray(),
                                            "next_seq": 0,
                                        }
                                    elif msg_type == BridgeMessageType.RESPONSE_CHUNK:
                                        state = self._chunked_pending.get(req_id)
                                        if state is None:
                                            raise RuntimeError(
                                                "Bridge chunk received before RESPONSE_START"
                                            )
                                        seq = int(str(data.get("seq")))
                                        if seq != state["next_seq"]:
                                            raise RuntimeError(
                                                "Bridge chunk out of order: "
                                                f"expected {state['next_seq']}, got {seq}"
                                            )
                                        encoded = data.get("body")
                                        if not isinstance(encoded, str):
                                            raise RuntimeError(
                                                "Bridge chunk body must be base64 text"
                                            )
                                        state["buffer"].extend(
                                            base64.b64decode(encoded, validate=True)
                                        )
                                        state["next_seq"] += 1
                                    else:
                                        state = self._chunked_pending.pop(req_id, None)
                                        if state is None:
                                            raise RuntimeError(
                                                "Bridge end received before RESPONSE_START"
                                            )
                                        expected_raw = data.get("bytes")
                                        if expected_raw is not None:
                                            expected = int(expected_raw)
                                            if expected != len(state["buffer"]):
                                                raise RuntimeError(
                                                    "Bridge binary length mismatch: "
                                                    f"expected {expected}, "
                                                    f"got {len(state['buffer'])}"
                                                )
                                        meta = state["meta"]
                                        fut.set_result(
                                            BridgeResponse(
                                                id=str(req_id),
                                                status_code=int(
                                                    meta.get("status_code", 0)
                                                ),
                                                status_text=str(
                                                    meta.get("status_text", "OK")
                                                ),
                                                headers=dict(
                                                    meta.get("headers") or {}
                                                ),
                                                body=None,
                                                is_base64=False,
                                                error=meta.get("error"),
                                                body_bytes=bytes(state["buffer"]),
                                            )
                                        )
                                except Exception as exc:
                                    self._chunked_pending.pop(req_id, None)
                                    if not fut.done():
                                        fut.set_exception(
                                            RuntimeError(
                                                f"Invalid chunked bridge response: {exc}"
                                            )
                                        )

                            if msg_type == BridgeMessageType.RESPONSE_END:
                                self._request_sources.pop(req_id, None)
                                self._request_executors.pop(req_id, None)

                        if source_ws:
                            try:
                                await source_ws.send(serialize_message(msg_type, data))
                            except Exception:
                                async with self._lock:
                                    self._request_sources.pop(req_id, None)

                elif msg_type == BridgeMessageType.COOKIE_UPDATE:
                    if self.on_cookie_update:
                        try:
                            self.on_cookie_update(data)
                        except Exception as e:
                            logger.warning(f"Error in on_cookie_update callback: {e}")

                elif msg_type == BridgeMessageType.PING:
                    await websocket.send(serialize_message(BridgeMessageType.PONG, {}))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._cleanup_client_requests(websocket, str(client_type))
            async with self._lock:
                self._extension_clients.discard(websocket)
                self._cli_clients.discard(websocket)

    async def send_request(
        self, request: BridgeRequest, timeout_seconds: float = 30.0
    ) -> BridgeResponse:
        """Send BridgeRequest in-process to an active extension client and wait for response."""
        async with self._lock:
            if not self._extension_clients:
                raise RuntimeError(
                    "No extension client connected to BridgeServer. "
                    "Vui lòng mở trình duyệt có cài đặt CTF Bridge Extension."
                )
            client = next(iter(self._extension_clients))
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[BridgeResponse] = loop.create_future()
            self._pending_futures[request.id] = fut
            self._request_executors[request.id] = client

        try:
            req_msg = serialize_message(BridgeMessageType.REQUEST_FORWARD, request.to_dict())
            await client.send(req_msg)
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Bridge request timed out after {timeout_seconds}s for URL: {request.url}"
            )
        finally:
            async with self._lock:
                self._pending_futures.pop(request.id, None)
                self._chunked_pending.pop(request.id, None)
                self._request_executors.pop(request.id, None)
