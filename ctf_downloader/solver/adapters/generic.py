"""Generic CLI Solver Adapter.

Allows running any custom AI agent executable via command-line arguments.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, List, Optional, Sequence

from ..base import (
    EngineCapabilities,
    EngineProbeResult,
    InvocationSpec,
    NormalizedSolverEvent,
    SolverAdapter,
)


class GenericCliAdapter(SolverAdapter):
    engine_id: str = "generic"
    display_name: str = "Generic CLI Agent"

    def __init__(self, command: Sequence[str] = ("python3", "solve.py")):
        self.command = list(command)

    def probe(self) -> EngineProbeResult:
        binary = self.command[0] if self.command else "python3"
        usable = bool(shutil.which(binary) or (os.path.isfile(binary) and os.access(binary, os.X_OK)))
        return EngineProbeResult(
            engine_id=self.engine_id,
            display_name=self.display_name,
            installed=usable,
            usable=usable,
            binary_path=binary if usable else None,
            version=None,
            status_message="Ready" if usable else f"Command '{binary}' not found",
        )

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            supports_streaming=False,
            supports_stdin_prompt=False,
            supports_headless=True,
            supports_session_resume=False,
            supports_session_fork=False,
            supports_tool_execution=False,
            default_log_filename="worker.log",
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
        argv = list(extra_args) if extra_args else list(self.command)
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
