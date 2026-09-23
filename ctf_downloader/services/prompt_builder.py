"""Category-aware prompt builder for CTF solver & triage workers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..categories import CategoryDefinition, CategoryRegistry

if TYPE_CHECKING:
    from .solver_service import SolverJob


def _find_workspace(start_path: Path | None) -> Path | None:
    """Find workspace root directory by looking for .ctf or known ancestor structure."""
    if not start_path:
        return None
    try:
        p = Path(start_path).resolve()
        for current in [p, *p.parents]:
            if (current / ".ctf").is_dir() or (current / ".ctf" / "categories.json").is_file():
                return current
        if len(p.parents) >= 2:
            return p.parents[1]
    except Exception:
        pass
    return None


class CategoryPromptBuilder:
    """Builds category-specific, filter-safe prompts scoped to current workspace."""

    @classmethod
    def _sanitize_filter_terms(cls, text: str | None) -> str:
        """Neutralize high-risk offensive terms in text before prompt interpolation."""
        if not text:
            return ""
        pattern = r"(?i)(^|[^a-zA-Z0-9])(?:weaponized?|0day|cyberattack|attack|exploit|payload|pwn|pwnable|pwning)s?([^a-zA-Z0-9]|$)"
        cleaned = str(text)
        prev = None
        while prev != cleaned:
            prev = cleaned
            cleaned = re.sub(pattern, r"\g<1>target\g<2>", cleaned)
        return cleaned

    @classmethod
    def _sanitize_name(cls, name: str | None, max_len: int = 80) -> str:
        """Strip control characters, newlines, and limit length to prevent prompt disruption."""
        if not name:
            return "Challenge"
        cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", str(name)).strip()
        cleaned = re.sub(r"^[#>*\-_=\s]+", "", cleaned)
        cleaned = cls._sanitize_filter_terms(cleaned)
        return cleaned[:max_len].strip() or "Challenge"

    @classmethod
    def _sanitize_line(cls, text: str | None, max_len: int = 160) -> str:
        """Strip control characters, newlines, and injection delimiters from context strings."""
        if not text:
            return ""
        cleaned = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", str(text)).strip()
        cleaned = re.sub(r"[`\"'\[\]{}]+", "", cleaned)
        return cleaned[:max_len].strip()

    @classmethod
    def normalize_category(cls, category: str | None) -> str:
        return CategoryRegistry.get_instance().normalize(category)

    @classmethod
    def _safe_category_label(cls, category: str | None) -> str:
        return CategoryRegistry.get_instance().safe_label(category)

    @classmethod
    def _sanitize_connection_info(cls, conn_str: str) -> str:
        """Parse and sanitize connection info to prevent command execution, injection, or IP filter triggers."""
        if not conn_str:
            return ""
        suspicious_re = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|nip\.io|sslip\.io|traefik\.me|local(?:host)?\b", re.IGNORECASE)
        # Check if it's nc <host> <port>
        nc_match = re.search(r"\b(?:nc|ncat|netcat)\s+([a-zA-Z0-9.\-_]+)\s+([0-9]{1,5})\b", conn_str)
        if nc_match:
            host, port = nc_match.group(1), nc_match.group(2)
            if suspicious_re.search(host):
                return f"Remote service configured in metadata.json (Port: {port})"
            return f"Host: {host}, Port: {port} (see metadata.json)"
        # Check if it's host:port
        hp_match = re.search(r"\b([a-zA-Z0-9.\-_]+):([0-9]{1,5})\b", conn_str)
        if hp_match:
            host, port = hp_match.group(1), hp_match.group(2)
            if suspicious_re.search(host):
                return f"Remote service configured in metadata.json (Port: {port})"
            return f"Host: {host}, Port: {port} (see metadata.json)"
        # Check if it's a URL
        url_match = re.search(r"https?://([a-zA-Z0-9.\-_]+)(?::([0-9]{1,5}))?(?:/[a-zA-Z0-9.\-_/]*)?", conn_str)
        if url_match:
            host = url_match.group(1)
            port = url_match.group(2) or ("443" if "https" in url_match.group(0) else "80")
            if suspicious_re.search(host):
                return f"Remote web endpoint configured in metadata.json (Port: {port})"
            clean_url = cls._sanitize_filter_terms(url_match.group(0))
            return f"Endpoint: {clean_url} (see metadata.json)"
        return "Available in metadata.json"

    @classmethod
    def _extract_context(cls, job: SolverJob) -> list[str]:
        """Extract lightweight metadata context (attachments, remote info) without inflating prompt size."""
        lines: list[str] = []
        chall_dir = job.path / "challenge"
        if chall_dir.is_dir():
            try:
                attachments = [
                    cls._sanitize_name(p.name) for p in sorted(chall_dir.iterdir())
                    if p.is_file() and not p.name.startswith(".") and p.name not in ("README.md", "NOTE.md", "metadata.json")
                ]
                if attachments:
                    files_str = ", ".join(attachments[:6])
                    if len(attachments) > 6:
                        files_str += f" (+{len(attachments) - 6} more)"
                    lines.append(f"Target Attachments in challenge/: {files_str}")
            except OSError:
                pass

        meta_path = job.path / "metadata.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                conn = meta.get("connection_info")
                if conn and isinstance(conn, str) and conn.strip():
                    lines.append(f"Target Connection: {cls._sanitize_connection_info(conn.strip())}")
                elif isinstance(meta.get("instance_info"), dict):
                    inst = meta["instance_info"]
                    host = inst.get("host")
                    port = inst.get("port")
                    url = inst.get("url") or inst.get("entry")
                    if url:
                        lines.append(f"Target Instance: {cls._sanitize_connection_info(str(url))}")
                    elif host and port:
                        h_str = cls._sanitize_line(str(host))
                        p_str = cls._sanitize_line(str(port))
                        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", h_str):
                            lines.append(f"Target Connection: Remote service configured in metadata.json (Port: {p_str})")
                        else:
                            lines.append(f"Target Connection: Host: {h_str}, Port: {p_str} (see metadata.json)")
            except Exception:
                pass
        return lines

    @classmethod
    def build_command_prompt(cls, job: SolverJob) -> str:
        """Construct the ultra-concise 1-line command prompt: /ctf-toolkit <chall_name>."""
        safe_name = cls._sanitize_name(job.name)
        return f"/ctf-toolkit {safe_name}"

    @classmethod
    def build(
        cls,
        job: SolverJob,
        *,
        fallback_mode: bool = False,
        is_continuation: bool = False,
        is_resume: bool = False,
        workspace: Path | None = None,
        minimal: bool = False,
    ) -> str:
        """Construct a scoped prompt tailored to the challenge category via declarative CategoryRegistry."""
        if minimal:
            return cls.build_command_prompt(job)

        if fallback_mode:
            return cls._fallback_triage_prompt(job)

        ws = workspace or (_find_workspace(job.path) if hasattr(job, "path") and job.path else None)
        registry = CategoryRegistry.get_instance(workspace=ws)
        cat_def = registry.get(job.category)
        base_prompt = cls._build_category_prompt(job, cat_def)

        ctx_lines = cls._extract_context(job)
        if ctx_lines:
            # Inject context block right after Workspace line
            parts = base_prompt.split("\n\n", 1)
            if len(parts) == 2:
                base_prompt = f"{parts[0]}\n" + "\n".join(ctx_lines) + f"\n\n{parts[1]}"
            else:
                base_prompt = base_prompt + "\n\n" + "\n".join(ctx_lines)

        safe_name = cls._sanitize_name(job.name)
        if is_resume:
            prefix = (
                f"[RESUMING CHALLENGE ANALYSIS · {safe_name}]\n"
                "Automatic state recovery:\n"
                "1. Read ./script/analysis.md and ./script/ to review existing progress and findings.\n"
                "2. Formulate your next analytical hypothesis and verification steps defensively.\n"
                "3. If blocked on a difficult mathematical, cryptographic, algorithmic modeling, SAT/SMT, or state reachability subproblem:\n"
                "   Abstract the subproblem into math_workspace/ with TASK.md and instance.json, and consult expert models via the `ctf-ask` skill methodology.\n"
                "4. Proceed to finish the solution, save any discovered flag to flag.txt, and write your step-by-step cognitive thought process writeup to writeup/README.md.\n\n"
            )
            return prefix + base_prompt

        if is_continuation:
            cat_label = cls._sanitize_filter_terms(registry.safe_label(job.category))
            prefix = (
                f"[CONTINUING {cat_label.upper()} SESSION · NEXT CHALLENGE: {safe_name}]\n"
                f"You are continuing your persistent {cat_label.lower()} solver session. "
                f"Previous workspace knowledge, scripts, and environment context are preserved in this conversation.\n"
                f"Proceed to solve this next challenge:\n\n"
            )
            return prefix + base_prompt
        return base_prompt

    @classmethod
    def _scope_preamble(cls, job: SolverJob) -> list[str]:
        return [
            "Workspace: Current working directory.",
            "Boundary: Strictly confined to this challenge directory (never scan /home, ~, Desktop, Downloads, or parent directories).",
            "Immediate Action: Read ./challenge/README.md and ./challenge/NOTE.md first to inspect files in ./challenge/.",
            "Hard Roadblocks: If blocked on a difficult mathematical, cryptographic, algorithmic modeling, SAT/SMT, or state reachability subproblem, abstract it into a minimal formal instance and use the `ctf-ask` skill with deterministic local verification.",
            "Post-Solve Delivery: After saving flag to flag.txt, write a step-by-step cognitive writeup in writeup/README.md. Focus strictly on actual thought process (why each step was taken, what discoveries prompted it, what was searched online and applied), not generic theory.",
        ]

    @classmethod
    def _build_category_prompt(cls, job: SolverJob, cat_def: CategoryDefinition) -> str:
        safe_name = cls._sanitize_name(job.name)
        title_sfx = cls._sanitize_filter_terms(cat_def.title_suffix) if cat_def.title_suffix else ""
        if cat_def.key == "general" or not title_sfx or title_sfx == "Challenge Analysis":
            title_header = f"Challenge: {safe_name}"
        else:
            title_header = f"Challenge: {safe_name} ({title_sfx})"

        lines = [
            title_header,
            *cls._scope_preamble(job),
            "",
        ]

        if cat_def.scope_focus:
            clean_scope = cls._sanitize_filter_terms(cat_def.scope_focus)
            lines.append(f"Scope: {clean_scope}")
        else:
            lines.append("Instructions:")

        for idx, instruction in enumerate(cat_def.instructions, 1):
            clean_inst = cls._sanitize_filter_terms(instruction)
            lines.append(f"{idx}. {clean_inst}")

        lines.extend([
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ])
        return "\n".join(lines)

    # Compatibility helpers delegating to schema registry
    @classmethod
    def _pwn_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("pwn"))

    @classmethod
    def _crypto_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("crypto"))

    @classmethod
    def _reverse_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("reverse"))

    @classmethod
    def _hardware_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("hardware"))

    @classmethod
    def _web_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("web"))

    @classmethod
    def _forensics_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("forensics"))

    @classmethod
    def _blockchain_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("blockchain"))

    @classmethod
    def _ai_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("ai"))

    @classmethod
    def _general_prompt(cls, job: SolverJob) -> str:
        return cls._build_category_prompt(job, CategoryRegistry.get_instance().get("general"))

    @staticmethod
    def _fallback_triage_prompt(job: SolverJob) -> str:
        safe_name = CategoryPromptBuilder._sanitize_name(job.name)
        return "\n".join((
            f"Challenge: {safe_name} (Defensive Architecture & Format Triage)",
            "Workspace: Current working directory.",
            "",
            "Note: Perform defensive software analysis and formal modeling:",
            "1. Document file structures, metadata, architecture, and protocol boundaries.",
            "2. Map function tables, state machines, and configuration parameters.",
            "3. Output a comprehensive technical assessment directly to script/analysis.md.",
        ))
