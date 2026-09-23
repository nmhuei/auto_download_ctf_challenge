"""Abstract base classes and contracts for Pluggable AI Solver Adapters.

Provides a unified interface so the CTF Toolkit can dispatch challenges
to diverse AI coding agents (Antigravity agy, Codex CLI, Claude Code, Kimi, etc.)
without hardcoding agent-specific arguments or log formats in core services.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class EngineCapabilities:
    """Declared runtime capabilities of a specific solver engine."""
    supports_streaming: bool = True
    supports_stdin_prompt: bool = True
    supports_headless: bool = True
    supports_session_resume: bool = True
    supports_session_fork: bool = False
    supports_tool_execution: bool = True
    default_log_filename: str = "worker.log"


@dataclass(frozen=True)
class EngineProbeResult:
    """Diagnostics and availability status of a solver engine on host system."""
    engine_id: str
    display_name: str
    installed: bool
    usable: bool
    binary_path: Optional[str] = None
    version: Optional[str] = None
    status_message: str = ""


@dataclass
class InvocationSpec:
    """Concrete execution specification for spawning an AI agent worker."""
    argv: List[str]
    cwd: Path
    env: Dict[str, str] = field(default_factory=dict)
    stdin_payload: Optional[str] = None
    log_filename: str = "worker.log"
    legacy_log_filename: Optional[str] = "agy.log"


@dataclass
class NormalizedSolverEvent:
    """Normalized telemetry event decoded from an engine's output stream."""
    event_type: str  # "progress", "text_delta", "tool_call", "refusal", "quota", "timeout", "done", "error"
    phase: Optional[str] = None
    message: Optional[str] = None
    candidate_flag: Optional[str] = None
    raw: Any = None


class SolverAdapter(ABC):
    """Abstract adapter governing integration with a specific AI agent CLI."""

    engine_id: str = "generic"
    display_name: str = "Generic AI Agent"

    @abstractmethod
    def probe(self) -> EngineProbeResult:
        """Probe host system to determine if this engine is installed and usable."""
        ...

    @abstractmethod
    def capabilities(self) -> EngineCapabilities:
        """Return the capabilities supported by this engine adapter."""
        ...

    @abstractmethod
    def build_invocation(
        self,
        job: Any,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        timeout: int = 900,
        extra_args: Optional[Sequence[str]] = None,
    ) -> InvocationSpec:
        """Construct a validated InvocationSpec containing exact argv, cwd, and env."""
        ...

    @abstractmethod
    def decode_stream_line(self, line: str) -> List[NormalizedSolverEvent]:
        """Decode a single stdout/stderr line into zero or more normalized solver events."""
        ...

    @abstractmethod
    def classify_exit(self, exit_code: int, events: List[NormalizedSolverEvent]) -> str:
        """Map process exit code and observed events to standard outcome string."""
        ...
