"""Protocol constants and configurations for Browser Extension Bridge."""
from enum import Enum
import os

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18888
TOKEN_FILE_NAME = "bridge_token"
PID_FILE_NAME = "bridge.pid"
DEFAULT_REQUEST_TIMEOUT_MS = 30000
# Keep each base64 JSON frame well below websockets' 1 MiB default limit.
BRIDGE_BINARY_CHUNK_BYTES = 192 * 1024


class BridgeMessageType(str, Enum):
    HANDSHAKE = "HANDSHAKE"
    HANDSHAKE_ACK = "HANDSHAKE_ACK"
    REQUEST_FORWARD = "REQUEST_FORWARD"
    RESPONSE_FORWARD = "RESPONSE_FORWARD"
    RESPONSE_START = "RESPONSE_START"
    RESPONSE_CHUNK = "RESPONSE_CHUNK"
    RESPONSE_END = "RESPONSE_END"
    COOKIE_UPDATE = "COOKIE_UPDATE"
    PING = "PING"
    PONG = "PONG"
    ERROR = "ERROR"


def get_bridge_token_path() -> str:
    from ..storage.global_config import CONFIG_DIR
    return os.path.join(CONFIG_DIR, TOKEN_FILE_NAME)


def get_bridge_pid_path() -> str:
    from ..storage.global_config import CONFIG_DIR
    return os.path.join(CONFIG_DIR, PID_FILE_NAME)


def get_bridge_cache_dir() -> str:
    """Directory for disk-backed streamed bridge responses."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "ctf_toolkit", "bridge")
