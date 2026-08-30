"""WebSocket Bridge Server for routing HTTP requests through browser extension."""
from __future__ import annotations

import asyncio
import logging
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
        self._request_sources: Dict[str, Any] = {}
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
            self._request_sources.clear()
            self._extension_clients.clear()
            self._cli_clients.clear()

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

                    ack_msg = serialize_message(
                        BridgeMessageType.HANDSHAKE_ACK,
                        {"status": "ok", "version": "3.0.0", "client_type": client_type},
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
                    # Request coming from CLI client over WS
                    req_id = data.get("id")
                    async with self._lock:
                        if not self._extension_clients:
                            err_msg = serialize_message(
                                BridgeMessageType.ERROR,
                                {
                                    "id": req_id,
                                    "message": "No extension client connected to BridgeServer.",
                                },
                            )
                            await websocket.send(err_msg)
                            continue

                        executor = next(iter(self._extension_clients))
                        if req_id:
                            self._request_sources[req_id] = websocket

                    # Forward to extension
                    req_envelope = serialize_message(BridgeMessageType.REQUEST_FORWARD, data)
                    await executor.send(req_envelope)

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

                        if source_ws:
                            try:
                                res_envelope = serialize_message(
                                    BridgeMessageType.RESPONSE_FORWARD, data
                                )
                                await source_ws.send(res_envelope)
                            except Exception:
                                pass

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
