"""Protocol constants and configurations for Browser Extension Bridge."""
from enum import Enum
import os

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18888
TOKEN_FILE_NAME = "bridge_token"
PID_FILE_NAME = "bridge.pid"
DEFAULT_REQUEST_TIMEOUT_MS = 30000


class BridgeMessageType(str, Enum):
    HANDSHAKE = "HANDSHAKE"
    HANDSHAKE_ACK = "HANDSHAKE_ACK"
    REQUEST_FORWARD = "REQUEST_FORWARD"
    RESPONSE_FORWARD = "RESPONSE_FORWARD"
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
