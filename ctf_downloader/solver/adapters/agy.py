"""Antigravity (Agy) CLI Solver Adapter.

Encapsulates all agy-specific command-line arguments, JSON stream parsing,
print timeout detection, and session management.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, List, Optional, Sequence

from ..base import (
    EngineCapabilities,
    EngineProbeResult,
    InvocationSpec,
    NormalizedSolverEvent,
    SolverAdapter,
)
from ...utils.agy_resolver import is_agy_available, resolve_agy_binary

_AGY_PRINT_TIMEOUT_RE = re.compile(r"\[agy\]\s+print timeout after", re.IGNORECASE)
_FILTER_KEYWORDS = ("cybersecurity", "safety policy", "content policy", "harmful content")
_QUOTA_KEYWORDS = ("quota exceeded", "rate limit exceeded", "exhausted resource")


class AgySolverAdapter(SolverAdapter):
    engine_id: str = "agy"
    display_name: str = "Antigravity CLI (Agy)"

    def __init__(self, binary_override: Optional[str] = None):
        self.binary_override = binary_override

    def probe(self) -> EngineProbeResult:
        binary = self.binary_override or resolve_agy_binary("agy")
        usable = is_agy_available(binary)
        return EngineProbeResult(
            engine_id=self.engine_id,
            display_name=self.display_name,
            installed=usable,
            usable=usable,
            binary_path=binary if usable else None,
            version=None,
            status_message="Ready" if usable else "agy binary not found in PATH or standard paths",
        )

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            supports_streaming=True,
            supports_stdin_prompt=False,
            supports_headless=True,
            supports_session_resume=True,
            supports_session_fork=True,
            supports_tool_execution=True,
            default_log_filename="agy.log",
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
        binary = self.binary_override or resolve_agy_binary("agy")
        base_cmd = list(extra_args) if extra_args else [
            binary,
            "--mode", "accept-edits",
            "--dangerously-skip-permissions",
        ]
        if base_cmd:
            base_cmd[0] = resolve_agy_binary(base_cmd[0])

        argv = list(base_cmd)
        if session_id and "--conversation" not in argv:
            argv.extend(["--conversation", str(session_id)])

        print_timeout = max(60, int(timeout) + 60)
        argv.extend([
            "--output-format", "stream-json",
            "--print-timeout", f"{print_timeout}s",
            "--print", prompt,
        ])

        env = os.environ.copy()
        env["AGY_FORCE_COLOR"] = "0"
        env["TERM"] = "dumb"

        return InvocationSpec(
            argv=argv,
            cwd=job.path,
            env=env,
            stdin_payload=None,
            log_filename="agy.log",
            legacy_log_filename="agy.log",
        )

    def decode_stream_line(self, line: str) -> List[NormalizedSolverEvent]:
        events: List[NormalizedSolverEvent] = []
        raw_text = line.strip()
        if not raw_text:
            return events

        # Check for agy print timeout
        if _AGY_PRINT_TIMEOUT_RE.search(raw_text):
            events.append(NormalizedSolverEvent(
                event_type="timeout",
                message="Agy print timeout triggered.",
                raw=raw_text,
            ))

        # Check for safety filter trigger
        low = raw_text.lower()
        if any(kw in low for kw in _FILTER_KEYWORDS):
            events.append(NormalizedSolverEvent(
                event_type="refusal",
                message="Safety / cybersecurity filter triggered.",
                raw=raw_text,
            ))

        if any(kw in low for kw in _QUOTA_KEYWORDS):
            events.append(NormalizedSolverEvent(
                event_type="quota",
                message="API quota or rate limit exceeded.",
                raw=raw_text,
            ))

        # Parse JSON stream format
        try:
            payload = json.loads(raw_text)
            if isinstance(payload, dict):
                # Conversation ID notification
                if "conversation_id" in payload:
                    events.append(NormalizedSolverEvent(
                        event_type="session_started",
                        message=str(payload["conversation_id"]),
                        raw=payload,
                    ))

                # Step / Progress update
                content = payload.get("content") or payload.get("delta") or payload.get("agent_response") or ""
                if isinstance(content, str) and "@@CTF_PROGRESS@@" in content:
                    idx = content.find("@@CTF_PROGRESS@@")
                    cand = content[idx + len("@@CTF_PROGRESS@@"):].strip()
                    try:
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
        except Exception:
            pass

        return events

    def classify_exit(self, exit_code: int, events: List[NormalizedSolverEvent]) -> str:
        for ev in events:
            if ev.event_type == "refusal":
                return "refusal"
            if ev.event_type == "quota":
                return "quota_exceeded"
            if ev.event_type == "timeout":
                return "timeout"

        if exit_code == 0:
            return "completed"
        return "error"
