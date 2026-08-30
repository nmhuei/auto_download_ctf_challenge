"""Data classes and serializers for Bridge WebSocket RPC protocol."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import json
from typing import Any, Dict, Optional, Tuple

from .constants import BridgeMessageType, DEFAULT_REQUEST_TIMEOUT_MS


@dataclass
class BridgeRequest:
    id: str
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None
    timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    binary: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BridgeRequest:
        return cls(
            id=str(data.get("id", "")),
            method=str(data.get("method", "GET")).upper(),
            url=str(data.get("url", "")),
            headers=dict(data.get("headers") or {}),
            body=data.get("body"),
            timeout_ms=int(data.get("timeout_ms") or DEFAULT_REQUEST_TIMEOUT_MS),
            binary=bool(data.get("binary", False)),
        )


@dataclass
class BridgeResponse:
    id: str
    status_code: int
    status_text: str = "OK"
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[str] = None
    is_base64: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def get_bytes(self) -> bytes:
        if not self.body:
            return b""
        if self.is_base64:
            return base64.b64decode(self.body)
        return self.body.encode("utf-8")

    def get_text(self) -> str:
        if not self.body:
            return ""
        if self.is_base64:
            return base64.b64decode(self.body).decode("utf-8", errors="replace")
        return str(self.body)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BridgeResponse:
        return cls(
            id=str(data.get("id", "")),
            status_code=int(data.get("status_code", 0)),
            status_text=str(data.get("status_text", "OK")),
            headers=dict(data.get("headers") or {}),
            body=data.get("body"),
            is_base64=bool(data.get("is_base64", False)),
            error=data.get("error"),
        )


def serialize_message(msg_type: BridgeMessageType, payload: Dict[str, Any]) -> str:
    """Serialize a message envelope to JSON string."""
    envelope = {
        "type": msg_type.value if isinstance(msg_type, BridgeMessageType) else str(msg_type),
        "data": payload,
    }
    return json.dumps(envelope)


def deserialize_message(raw_json: str) -> Tuple[BridgeMessageType, Dict[str, Any]]:
    """Deserialize a JSON string into (BridgeMessageType, data)."""
    try:
        parsed = json.loads(raw_json)
    except Exception as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc

    if not isinstance(parsed, dict) or "type" not in parsed:
        raise ValueError("Missing 'type' in message envelope")

    type_str = parsed["type"]
    try:
        msg_type = BridgeMessageType(type_str)
    except ValueError as exc:
        raise ValueError(f"Unknown message type: {type_str}") from exc

    data = parsed.get("data")
    if not isinstance(data, dict):
        data = {}

    return msg_type, data
