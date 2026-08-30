"""Daemon manager for BridgeServer background process & credentials."""
from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from typing import Optional

from .constants import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    get_bridge_pid_path,
    get_bridge_token_path,
)


class BridgeDaemon:
    """Manages the background bridge server process lifecycle and authentication token."""

    def __init__(
        self,
        token_path: Optional[str] = None,
        pid_path: Optional[str] = None,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
    ):
        self.token_path = token_path or get_bridge_token_path()
        self.pid_path = pid_path or get_bridge_pid_path()
        self.host = host
        self.port = port

    def get_or_create_token(self) -> str:
        """Read existing token or generate a new 32-byte hex token."""
        if os.path.exists(self.token_path):
            try:
                with open(self.token_path, "r", encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
            except Exception:
                pass

        token = secrets.token_hex(32)
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(self.token_path, 0o600)
        except Exception:
            pass
        return token

    def read_pid(self) -> Optional[int]:
        """Read PID from PID file if exists."""
        if not os.path.exists(self.pid_path):
            return None
        try:
            with open(self.pid_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            return int(content) if content else None
        except Exception:
            return None

    def _write_pid(self, pid: int) -> None:
        os.makedirs(os.path.dirname(self.pid_path), exist_ok=True)
        with open(self.pid_path, "w", encoding="utf-8") as f:
            f.write(str(pid))

    def _clear_pid(self) -> None:
        if os.path.exists(self.pid_path):
            try:
                os.remove(self.pid_path)
            except Exception:
                pass

    def is_port_open(self) -> bool:
        """Check if bridge server is accepting TCP connections on port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((self.host, self.port)) == 0

    def is_running(self) -> bool:
        """Check if process is alive and port is active."""
        pid = self.read_pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            self._clear_pid()
            return False

    def ensure_running(self) -> bool:
        """Ensure the BridgeServer daemon is running; start in background if not."""
        if self.is_running() and self.is_port_open():
            return True

        token = self.get_or_create_token()
        # Spawn background process
        cmd = [
            sys.executable,
            "-m",
            "ctf_downloader.bridge.runner",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--token",
            token,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._write_pid(proc.pid)

        # Wait up to 3 seconds for port to open
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self.is_port_open():
                return True
            time.sleep(0.1)

        return self.is_port_open()

    def stop(self) -> bool:
        """Stop the running BridgeServer process."""
        pid = self.read_pid()
        if not pid:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
        except Exception:
            pass
        finally:
            self._clear_pid()
        return True
