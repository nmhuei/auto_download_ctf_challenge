"""Playbook Distiller: Synthesizes operational methodology and evolves category SOPs."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..utils.agy_resolver import is_agy_available, resolve_agy_binary


PLAYBOOK_PROMPT_TEMPLATE = """You are the Category Master Architect for CTF {category}.
Your objective is to update and refine the Operational Playbook (Standard Operating Procedure) based on the latest execution trajectory.

CRITICAL INSTRUCTIONS:
1. Focus STRICTLY on PROCESS, METHODOLOGY, and WORKFLOW (quy trình làm bài).
2. DO NOT record challenge-specific trivia, specific passwords, specific numbers/primes, or flag strings.
3. Extract actionable heuristics:
   - Optimal reconnaissance order and discriminating checks.
   - Hypothesis triage: how to identify and prune dead ends early before wasting tokens/time.
   - Local verification standard before firing against remote.
   - Escalation trigger: when a mathematical or formal abstraction model (ctf-ask / Astra expert) should be used instead of manual guessing.
   - Anti-patterns: procedural traps, unnecessary brute-forcing, or missing checks observed during solving.

Existing Playbook for {category}:
\"\"\"
{existing_playbook}
\"\"\"

Execution Telemetry & Evidence from Recent Challenge(s):
{telemetry_summary}

Produce an updated, cohesive, markdown-formatted playbook with the following sections:
# {category} Operational Playbook (SOP)
## 1. Reconnaissance & Discriminating Checks
## 2. Hypothesis Triage & Dead-End Pruning
## 3. Local Verification & Sandbox Testing
## 4. Remote Triggering & Flag Capture
## 5. Formal & Mathematical Escalation (ctf-ask Triggers)
## 6. Procedural Traps to Avoid

Return ONLY the updated markdown document.
"""


class PlaybookDistiller:
    """Distills solver trajectories into category-wide operational playbooks."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.playbooks_dir = self.workspace / ".ctf-solver" / "playbooks"

    def get_playbook_path(self, category: str) -> Path:
        sanitized = re.sub(r"[^\w\-]", "_", category.strip())
        return self.playbooks_dir / f"{sanitized}.md"

    def read_playbook(self, category: str) -> str:
        path = self.get_playbook_path(category)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""
        return ""

    def save_playbook(self, category: str, content: str) -> Path:
        self.playbooks_dir.mkdir(parents=True, exist_ok=True)
        path = self.get_playbook_path(category)
        path.write_text(content.strip() + "\n", encoding="utf-8")
        return path

    def collect_telemetry(self, jobs: Sequence[Any]) -> str:
        """Gather sanitized execution history, tool calls, and outcomes from jobs."""
        lines: List[str] = []
        for job in jobs:
            st_path = getattr(job, "state_path", None)
            log_path = getattr(job, "log_path", None)
            script_dir = getattr(job, "script_dir", None)
            name = getattr(job, "name", "Challenge")
            cat = getattr(job, "category", "Unknown")

            state: Dict[str, Any] = {}
            if st_path and Path(st_path).is_file():
                try:
                    state = json.loads(Path(st_path).read_text(encoding="utf-8"))
                except Exception:
                    pass

            outcome = state.get("outcome") or state.get("phase") or state.get("state") or "unknown"
            err = state.get("error_code")
            reused = state.get("reused_session", False)

            lines.append(f"### Challenge: {name} (Category: {cat})")
            lines.append(f"- Final Outcome: {outcome}")
            if err:
                lines.append(f"- Error Encountered: {err} ({state.get('message')})")
            lines.append(f"- Reused Persistent Session: {reused}")

            # Collect findings / analysis if present
            if script_dir:
                analysis_path = Path(script_dir) / "analysis.md"
                if analysis_path.is_file():
                    try:
                        content = analysis_path.read_text(encoding="utf-8", errors="replace")
                        # Truncate to first 500 chars to avoid leaking instance trivia
                        preview = content[:500].replace("\n", " ").strip()
                        lines.append(f"- Analysis Overview: {preview}...")
                    except OSError:
                        pass

            # Scan log for tools called
            if log_path and Path(log_path).is_file():
                try:
                    log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                    tool_names = set(re.findall(r'"tool_name"\s*:\s*"([a-zA-Z0-9_\-]+)"', log_text))
                    if tool_names:
                        lines.append(f"- Tools Utilized: {', '.join(sorted(tool_names))}")
                except OSError:
                    pass

            lines.append("")
        return "\n".join(lines).strip()

    def distill(
        self,
        category: str,
        jobs: Sequence[Any],
        *,
        main_conversation_id: Optional[str] = None,
        agy_cmd: Optional[Sequence[str]] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Synthesize telemetry and update category playbook + main session."""
        cat_clean = category.strip()
        cat_jobs = [j for j in jobs if getattr(j, "category", "").strip().casefold() == cat_clean.casefold()]

        existing = self.read_playbook(cat_clean)
        telemetry = self.collect_telemetry(cat_jobs)

        if not telemetry:
            telemetry = f"No completed challenge telemetry recorded yet for {cat_clean}. Initializing baseline SOP."

        prompt = PLAYBOOK_PROMPT_TEMPLATE.format(
            category=cat_clean,
            existing_playbook=existing or "No previous playbook recorded. Create initial version.",
            telemetry_summary=telemetry,
        )

        binary = resolve_agy_binary((agy_cmd[0] if agy_cmd else "agy"))
        updated_content: Optional[str] = None

        # If agy is available and we have a main_conversation_id or can run print mode:
        if is_agy_available(binary):
            cmd = [binary, "--mode", "accept-edits", "--dangerously-skip-permissions"]
            if main_conversation_id:
                cmd.extend(["--conversation", main_conversation_id])
            cmd.extend(["--print-timeout", f"{timeout}s", "--print", prompt])

            try:
                res = subprocess.run(
                    cmd,
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    updated_content = res.stdout.strip()
            except Exception:
                pass

        # Fallback template if agy execution was not available or errored
        if not updated_content:
            if existing and existing.strip():
                return {
                    "success": False,
                    "category": cat_clean,
                    "playbook_path": str(self.get_playbook_path(cat_clean)),
                    "error": "Agy distillation execution failed or was unavailable; preserved existing playbook.",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "main_conversation_id": main_conversation_id,
                    "jobs_distilled": 0,
                    "content_preview": existing[:300],
                }

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            updated_content = f"""# {cat_clean} Operational Playbook (SOP)
*Last updated: {now_str}*

## 1. Reconnaissance & Discriminating Checks
- Perform surface inspection before running heavy exploits.
- Check source code comments, generator scripts, and build artifacts.
- Determine constraints, alphabet, and input spaces.

## 2. Hypothesis Triage & Dead-End Pruning
- Formulate 2-3 falsifiable hypotheses before writing attack scripts.
- Discard brute-force attempts immediately if complexity exceeds 2^20.
- Verify whether the problem reduces to a known algebraic, state, or constraint system.

## 3. Local Verification & Sandbox Testing
- Always construct a minimal reproducible script locally.
- Test against provided sample inputs/outputs before targeting remote sockets.
- Ensure all dependencies and math environments are isolated.

## 4. Remote Triggering & Flag Capture
- Verify network connection stability and rate limits.
- Handle socket timeouts gracefully and buffer output completely.
- Capture and format flag cleanly in `flag.txt` and report.

## 5. Formal & Mathematical Escalation (ctf-ask Triggers)
- Escalate to `ctf-ask` (Codex Astra High) when:
  * Lattice dimension n > 40 requiring reduction (LLL/BKZ).
  * Non-linear filter or algebraic relation inversion.
  * Complex SMT/SAT constraint models or state machines.

## 6. Procedural Traps to Avoid
- Avoid testing random payloads without analyzing the verification logic.
- Do not let background processes hang or accumulate stale state.
- Keep the master playbook clean of temporary challenge scripts.
"""

        saved_path = self.save_playbook(cat_clean, updated_content)
        return {
            "success": True,
            "category": cat_clean,
            "playbook_path": str(saved_path),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "main_conversation_id": main_conversation_id,
            "jobs_distilled": len(cat_jobs),
            "content_preview": updated_content[:300],
        }
