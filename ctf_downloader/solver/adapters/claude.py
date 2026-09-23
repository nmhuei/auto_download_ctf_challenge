"""Claude Code CLI Solver Adapter (Placeholder & Integration spec).

Prepares non-interactive execution for Anthropic Claude Code using 'claude -p'.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, List, Optional, Sequence

from ..base import (
    EngineCapabilities,
    EngineProbeResult,
    InvocationSpec,
    NormalizedSolverEvent,
    SolverAdapter,
)


class ClaudeSolverAdapter(SolverAdapter):
    engine_id: str = "claude"
    display_name: str = "Claude Code"

    def __init__(self, binary_override: Optional[str] = None):
        self.binary_override = binary_override

    def probe(self) -> EngineProbeResult:
        binary = self.binary_override or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        usable = bool(binary and os.path.isfile(binary) and os.access(binary, os.X_OK))
        return EngineProbeResult(
            engine_id=self.engine_id,
            display_name=self.display_name,
            installed=usable,
            usable=usable,
            binary_path=binary if usable else None,
            version=None,
            status_message="Ready" if usable else "claude binary not found in PATH or ~/.local/bin",
        )

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            supports_streaming=True,
            supports_stdin_prompt=True,
            supports_headless=True,
            supports_session_resume=True,
            supports_session_fork=False,
            supports_tool_execution=True,
            default_log_filename="claude.log",
        )

    def build_invocation(
        self,
        job: Any,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        timeout: int = 900,
        extra_args: Optional[Sequence[str]] = None,
    ) -> InvocationSpec:
        binary = self.binary_override or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        argv = list(extra_args) if extra_args else [
            binary, "-p",
            "--output-format", "stream-json",
        ]
        argv.append(prompt)

        env = os.environ.copy()
        return InvocationSpec(
            argv=argv,
            cwd=job.path,
            env=env,
            stdin_payload=None,
            log_filename="worker.log",
            legacy_log_filename="agy.log",
        )

    def decode_stream_line(self, line: str) -> List[NormalizedSolverEvent]:
        events: List[NormalizedSolverEvent] = []
        raw = line.strip()
        if not raw:
            return events

        if "@@CTF_PROGRESS@@" in raw:
            idx = raw.find("@@CTF_PROGRESS@@")
            cand = raw[idx + len("@@CTF_PROGRESS@@"):].strip()
            try:
                import json
                p_data = json.loads(cand)
                if isinstance(p_data, dict):
                    events.append(NormalizedSolverEvent(
                        event_type="progress",
                        phase=p_data.get("phase"),
                        message=p_data.get("message"),
                        candidate_flag=p_data.get("candidate_flag"),
                        raw=p_data,
                    ))
            except Exception:
                pass
        return events

    def classify_exit(self, exit_code: int, events: List[NormalizedSolverEvent]) -> str:
        if exit_code == 0:
            return "completed"
        return "error"
