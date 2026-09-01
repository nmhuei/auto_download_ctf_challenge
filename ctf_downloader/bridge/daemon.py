"""Daemon manager for BridgeServer background process & credentials."""
from __future__ import annotations

import importlib.util
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


class BridgeDaemonError(RuntimeError):
    """Actionable local daemon/token lifecycle failure."""


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
        self.last_error: Optional[str] = None

    def read_token(self) -> Optional[str]:
        """Read the existing token without creating or mutating any files."""
        if not os.path.exists(self.token_path):
            return None
        with open(self.token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None

    def get_or_create_token(self) -> str:
        """Read or create the token without silently replacing unreadable state."""
        if os.path.exists(self.token_path):
            try:
                existing = self.read_token()
            except OSError as exc:
                raise BridgeDaemonError(
                    f"Không đọc được Bridge token '{self.token_path}': "
                    f"{type(exc).__name__}: {exc}. Kiểm tra owner/quyền file."
                ) from exc
            if existing:
                return existing

        token = secrets.token_hex(32)
        try:
            os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(token)
            os.chmod(self.token_path, 0o600)
        except OSError as exc:
            raise BridgeDaemonError(
                f"Không tạo/ghi được Bridge token '{self.token_path}': "
                f"{type(exc).__name__}: {exc}. Kiểm tra quyền thư mục config."
            ) from exc
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

    def _pid_matches_bridge(self, pid: int) -> bool:
        """Verify that *pid* is this bridge runner, not a reused PID.

        Linux exposes argv via /proc. The daemon is launched as Python module
        ctf_downloader.bridge.runner with an explicit --port argument; matching
        both values prevents a stale PID file from targeting another process.
        """
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                argv = [part.decode("utf-8", errors="replace")
                        for part in f.read().split(b"\0") if part]
        except (OSError, ValueError):
            return False

        if "ctf_downloader.bridge.runner" not in argv:
            return False
        try:
            port_index = argv.index("--port") + 1
            return int(argv[port_index]) == int(self.port)
        except (ValueError, IndexError):
            return False

    def is_running(self) -> bool:
        """Check whether the tracked PID is a live bridge runner."""
        pid = self.read_pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            self._clear_pid()
            return False

        if not self._pid_matches_bridge(pid):
            self._clear_pid()
            return False
        return True

    def inspect_status(self) -> dict:
        """Return one ownership snapshot for Bridge diagnostics.

        A listening port only proves that something accepts TCP. A live
        tracked PID proves the process is our bridge runner. Keeping both
        facts together lets status/start/doctor agree on port conflicts.
        """
        pid_running = self.is_running()
        tracked_pid = self.read_pid() if pid_running else None
        port_open = self.is_port_open()
        owned = bool(pid_running and port_open)
        return {
            "pid_running": bool(pid_running),
            "port_open": bool(port_open),
            "owned": owned,
            "port_conflict": bool(port_open and not pid_running),
            "pid": tracked_pid,
            "host": self.host,
            "port": self.port,
        }

    def ensure_running(self) -> bool:
        """Ensure the BridgeServer daemon is running; start it if safe.

        A listening TCP port alone isn't proof that our bridge owns it. If the
        configured port is already occupied without a live PID tracked by this
        daemon, fail closed instead of spawning and then mistaking the foreign
        listener for a successful bridge startup.
        """
        self.last_error = None
        if importlib.util.find_spec("websockets") is None:
            self.last_error = (
                "Thiếu Python dependency 'websockets>=15' cho Browser Bridge. "
                "Chạy 'python -m pip install -r requirements.txt'."
            )
            return False

        status = self.inspect_status()
        if status["pid_running"]:
            if not status["port_open"]:
                self.last_error = (
                    f"Bridge PID {status['pid']} đang sống nhưng port "
                    f"{self.host}:{self.port} chưa listen."
                )
            return bool(status["port_open"])
        if status["port_conflict"]:
            self.last_error = (
                f"Port {self.host}:{self.port} đang bị process khác chiếm; "
                "PID bridge hợp lệ không tồn tại."
            )
            return False

        try:
            token = self.get_or_create_token()
        except BridgeDaemonError as exc:
            self.last_error = str(exc)
            return False

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

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.last_error = (
                f"Không spawn được Bridge daemon bằng {sys.executable}: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

        try:
            self._write_pid(proc.pid)
        except OSError as exc:
            self.last_error = (
                f"Bridge child đã spawn nhưng không ghi được PID file "
                f"'{self.pid_path}': {type(exc).__name__}: {exc}"
            )
            try:
                proc.terminate()
            except OSError:
                pass
            return False

        # Wait up to 3 seconds for *this child* to open the port. If the child
        # exits (for example EADDRINUSE), clear the stale PID immediately.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            returncode = proc.poll()
            if returncode is not None:
                self._clear_pid()
                self.last_error = (
                    f"Bridge daemon thoát sớm (exit {returncode}) trước khi "
                    f"listen tại {self.host}:{self.port}."
                )
                return False
            if self.is_port_open():
                return True
            time.sleep(0.1)

        child_alive = proc.poll() is None
        if not child_alive:
            self._clear_pid()
        if child_alive and self.is_port_open():
            return True

        self.last_error = (
            f"Bridge daemon không mở port {self.host}:{self.port} trong 3 giây."
        )
        return False

    def stop(self) -> bool:
        """Stop the tracked BridgeServer without signaling a reused PID."""
        if not self.is_running():
            return False
        pid = self.read_pid()
        if not pid:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
        except (ProcessLookupError, PermissionError):
            return False
        finally:
            self._clear_pid()
        return True
