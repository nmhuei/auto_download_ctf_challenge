"""ChatGPT Web Wire Conduit (GPT-5.6 Thinking) Solver Adapter.

Encapsulates headless execution for ChatGPT Web Conduit CLI (gpt)
running gpt-5-6-thinking (extended effort) with local MCP tools via bridge.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Sequence

from ..base import (
    EngineCapabilities,
    EngineProbeResult,
    InvocationSpec,
    NormalizedSolverEvent,
    SolverAdapter,
)

_CTF_PROGRESS_RE = re.compile(r"@@CTF_PROGRESS@@\s*(\{.*?\})")
_FLAG_RE = re.compile(r"(?:asis|flag|ctf|picoctf|htb|shell|seccon)\{[a-zA-Z0-9_\-\.\:\@\!\?\$\#\%\&\*\+\=]+\}", re.IGNORECASE)
_FILTER_KEYWORDS = ("safety policy", "content policy", "harmful content", "cannot assist with this request", "against our use case policy")
_QUOTA_KEYWORDS = ("quota exceeded", "rate limit exceeded", "too many requests", "insufficient_quota")


class GptSolverAdapter(SolverAdapter):
    engine_id: str = "gpt"
    display_name: str = "ChatGPT Web Conduit (GPT-5.6 Thinking)"

    def __init__(self, binary_override: Optional[str] = None):
        self.binary_override = binary_override

    def probe(self) -> EngineProbeResult:
        binary = (
            self.binary_override
            or shutil.which("gpt")
            or str(Path.home() / ".local" / "bin" / "gpt")
        )
        usable = bool(binary and os.path.isfile(binary) and os.access(binary, os.X_OK))
        return EngineProbeResult(
            engine_id=self.engine_id,
            display_name=self.display_name,
            installed=usable,
            usable=usable,
            binary_path=binary if usable else None,
            version=None,
            status_message="Ready" if usable else "gpt launcher not found in PATH or ~/.local/bin/gpt",
        )

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            supports_streaming=True,
            supports_stdin_prompt=False,
            supports_headless=True,
            supports_session_resume=True,
            supports_session_fork=False,
            supports_tool_execution=True,
            default_log_filename="gpt.log",
        )

    def _binary(self) -> str:
        return (
            self.binary_override
            or shutil.which("gpt")
            or str(Path.home() / ".local" / "bin" / "gpt")
        )

    def create_session(self, job: Any) -> str:
        """Create and return a durable GPT local session before worker launch."""
        category = re.sub(r"[^a-z0-9]+", "-", str(job.category).lower()).strip("-")
        name = re.sub(r"[^a-z0-9]+", "-", str(job.name).lower()).strip("-")
        tag = f"ctf:{job.display_id}:{category}-{name}"[:96]
        result = subprocess.run(
            [self._binary(), "session", "new", "--json", "--tag", tag],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            payload = json.loads(result.stdout)
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
        except ValueError as exc:
            raise RuntimeError("gpt session creation returned invalid JSON") from exc
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("gpt session creation did not return a session_id")
        return session_id

    def build_invocation(
        self,
        job: Any,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        timeout: int = 900,
        extra_args: Optional[Sequence[str]] = None,
    ) -> InvocationSpec:
        binary = self._binary()
        if extra_args:
            argv = list(extra_args)
        elif session_id:
            argv = [
                binary, "run", "--resume-session", session_id,
                "-b", "br", "--json", "-p", prompt,
            ]
        else:
            argv = [binary, "run", "-b", "br", "--json", "-p", prompt]

        env = os.environ.copy()
        env["TERM"] = "dumb"

        return InvocationSpec(
            argv=argv,
            cwd=job.path,
            env=env,
            stdin_payload=None,
            log_filename="gpt.log",
            legacy_log_filename="agy.log",
        )

    def decode_stream_line(self, line: str) -> List[NormalizedSolverEvent]:
        events: List[NormalizedSolverEvent] = []
        raw_text = line.strip()
        if not raw_text:
            return events

        # `gpt run --json` emits one terminal document even when the process
        # itself exits successfully.  Treat lifecycle failures as failures,
        # not as ordinary text: a zero exit code only means the CLI rendered
        # the response, not that the upstream turn settled.
        try:
            terminal = json.loads(raw_text)
        except ValueError:
            terminal = None
        if isinstance(terminal, dict) and "lifecycle_status" in terminal:
            lifecycle = str(terminal.get("lifecycle_status") or "")
            message = str(terminal.get("error") or terminal.get("text") or lifecycle)
            if lifecycle == "completed_turn":
                events.append(NormalizedSolverEvent(
                    event_type="done", phase="completed", message=message, raw=terminal,
                ))
            elif lifecycle in {"upstream_error", "transport_lost", "server_active"}:
                events.append(NormalizedSolverEvent(
                    event_type="upstream", phase="upstream", message=message, raw=terminal,
                ))
            else:
                events.append(NormalizedSolverEvent(
                    event_type="terminal", phase="terminal", message=message, raw=terminal,
                ))
            return events

        # 1. Check for @@CTF_PROGRESS@@ JSON markers
        match = _CTF_PROGRESS_RE.search(raw_text)
        if match:
            try:
                data = json.loads(match.group(1))
                cand = str(data.get("candidate_flag", "")).strip()
                if cand in ("...", "<FLAG>", "<ACTUAL_FLAG>", "FLAG", "") or "..." in cand or (cand.startswith("<") and cand.endswith(">")):
                    cand = None
                events.append(
                    NormalizedSolverEvent(
                        event_type="progress",
                        phase=data.get("phase"),
                        message=data.get("message"),
                        candidate_flag=cand,
                        raw=data,
                    )
                )
            except Exception:
                pass

        # 2. Check for candidate flags directly in text
        flag_match = _FLAG_RE.search(raw_text)
        if flag_match:
            candidate = flag_match.group(0)
            if not any(e.candidate_flag == candidate for e in events):
                events.append(
                    NormalizedSolverEvent(
                        event_type="progress",
                        phase="exploit",
                        message=f"Discovered flag: {candidate}",
                        candidate_flag=candidate,
                        raw=raw_text,
                    )
                )

        # 3. Check for Thinking telemetry (💭 [Thinking] ...)
        if "💭 [Thinking]" in raw_text or "💭" in raw_text:
            cleaned = raw_text.replace("💭 [Thinking]", "").replace("💭", "").strip()
            events.append(
                NormalizedSolverEvent(
                    event_type="progress",
                    phase="thinking",
                    message=cleaned,
                    raw=raw_text,
                )
            )

        # 4. Check for Tool telemetry (🔧 [Tool] Calling ... / 🔧 [Tool Finished] ...)
        if "🔧 [Tool" in raw_text or "🔧" in raw_text:
            cleaned = (
                raw_text.replace("🔧 [Tool Call:", "")
                .replace("🔧 [Tool Finished]", "")
                .replace("🔧 [Tool]", "")
                .replace("🔧", "")
                .strip()
            )
            if cleaned.startswith("Calling "):
                cleaned = cleaned[len("Calling "):].strip()
            if cleaned.endswith("..."):
                cleaned = cleaned[:-3].strip()
            events.append(
                NormalizedSolverEvent(
                    event_type="tool_call",
                    phase="tool",
                    message=cleaned,
                    raw=raw_text,
                )
            )

        # 5. Check for Safety / Filter Refusal
        lower_line = raw_text.lower()
        if any(kw in lower_line for kw in _FILTER_KEYWORDS):
            events.append(
                NormalizedSolverEvent(
                    event_type="refusal",
                    message=raw_text,
                    raw=raw_text,
                )
            )

        # 6. Check for Quota Exceeded
        if any(kw in lower_line for kw in _QUOTA_KEYWORDS):
            events.append(
                NormalizedSolverEvent(
                    event_type="quota",
                    message=raw_text,
                    raw=raw_text,
                )
            )

        # Default text delta event
        if not events:
            events.append(
                NormalizedSolverEvent(
                    event_type="text_delta",
                    message=raw_text,
                    raw=raw_text,
                )
            )

        return events

    def classify_exit(self, exit_code: int, events: List[NormalizedSolverEvent]) -> str:
        for ev in events:
            if ev.candidate_flag:
                return "completed"
            if ev.event_type == "upstream":
                return "upstream_error"
            if ev.event_type == "terminal":
                return "model_terminal"
            if ev.event_type == "refusal":
                return "refused"
            if ev.event_type == "quota":
                return "quota_exceeded"
            if ev.event_type == "timeout":
                return "timeout"

        if exit_code == 0:
            return "completed"
        return "failed"
