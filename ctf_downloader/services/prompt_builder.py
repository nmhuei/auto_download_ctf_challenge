"""Category-aware prompt builder for CTF solver & triage workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .solver_service import SolverJob


class CategoryPromptBuilder:
    """Builds category-specific, filter-safe prompts scoped to current workspace."""

    @classmethod
    def normalize_category(cls, category: str | None) -> str:
        cat = (category or "").strip().lower()
        if any(k in cat for k in ("pwn", "binary", "kernel", "heap", "buffer", "overflow")):
            return "pwn"
        if any(k in cat for k in ("crypto", "cipher", "rsa", "ecc", "hash")):
            return "crypto"
        if any(k in cat for k in ("rev", "reverse", "crack", "decompile", "vm")):
            return "reverse"
        if any(k in cat for k in ("hardware", "rf", "radio", "dsp", "signal", "iot", "firmware")):
            return "hardware"
        if any(k in cat for k in ("blockchain", "web3", "contract", "solidity", "evm", "ether")):
            return "blockchain"
        if any(k in cat for k in ("web", "http", "api", "injection", "xss")):
            return "web"
        if any(k in cat for k in ("forensic", "pcap", "dfir", "stego", "disk", "memory", "image")):
            return "forensics"
        if any(k in cat for k in ("ai", "llm", "prompt", "jailbreak", "model", "neural", "ml")):
            return "ai"
        return "general"

    @classmethod
    def _extract_context(cls, job: SolverJob) -> list[str]:
        """Extract lightweight metadata context (attachments, remote info) without inflating prompt size."""
        lines: list[str] = []
        chall_dir = job.path / "challenge"
        if chall_dir.is_dir():
            try:
                attachments = [
                    p.name for p in sorted(chall_dir.iterdir())
                    if p.is_file() and not p.name.startswith(".") and p.name not in ("README.md", "NOTE.md", "metadata.json")
                ]
                if attachments:
                    files_str = ", ".join(attachments[:8])
                    if len(attachments) > 8:
                        files_str += f" (+{len(attachments) - 8} more)"
                    lines.append(f"Target Attachments in challenge/: {files_str}")
            except OSError:
                pass

        meta_path = job.path / "metadata.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                conn = meta.get("connection_info")
                if conn and isinstance(conn, str) and conn.strip():
                    lines.append(f"Target Connection: {conn.strip()}")
                elif isinstance(meta.get("instance_info"), dict):
                    inst = meta["instance_info"]
                    host = inst.get("host")
                    port = inst.get("port")
                    url = inst.get("url") or inst.get("entry")
                    if url:
                        lines.append(f"Target Instance: {url}")
                    elif host and port:
                        lines.append(f"Target Connection: nc {host} {port}")
            except Exception:
                pass
        return lines

    @classmethod
    def build(cls, job: SolverJob, *, fallback_mode: bool = False, is_continuation: bool = False, is_resume: bool = False) -> str:
        """Construct a scoped prompt tailored to the challenge category."""
        if fallback_mode:
            return cls._fallback_triage_prompt(job)

        cat_key = cls.normalize_category(job.category)
        builder_map = {
            "pwn": cls._pwn_prompt,
            "crypto": cls._crypto_prompt,
            "reverse": cls._reverse_prompt,
            "hardware": cls._hardware_prompt,
            "web": cls._web_prompt,
            "forensics": cls._forensics_prompt,
            "blockchain": cls._blockchain_prompt,
            "ai": cls._ai_prompt,
        }
        builder = builder_map.get(cat_key, cls._general_prompt)
        base_prompt = builder(job)

        ctx_lines = cls._extract_context(job)
        if ctx_lines:
            # Inject context block right after Workspace line
            parts = base_prompt.split("\n\n", 1)
            if len(parts) == 2:
                base_prompt = f"{parts[0]}\n" + "\n".join(ctx_lines) + f"\n\n{parts[1]}"
            else:
                base_prompt = base_prompt + "\n\n" + "\n".join(ctx_lines)

        if is_resume:
            prefix = (
                f"[RESUMING CHALLENGE ANALYSIS · {job.name}]\n"
                "Your previous turn was interrupted or stopped by a safety guardrail. "
                "Do NOT attempt any offensive exploitation, raw payload execution, or cyber attacks.\n"
                "Automatic state recovery:\n"
                "1. Read ./script/analysis.md and inspect files in ./script/ to identify what has already been analyzed and where you were stopped.\n"
                "2. Formulate your next hypothesis defensively.\n"
                "3. If blocked on a difficult mathematical, cryptographic, reverse-engineering, SAT/SMT, or state reachability subproblem:\n"
                "   Do NOT force an attack script. Abstract the subproblem into math_workspace/ with TASK.md and instance.json, and run:\n"
                "   ctf ask --workspace math_workspace\n"
                "4. Proceed to finish the solution from your existing progress.\n\n"
            )
            return prefix + base_prompt

        if is_continuation:
            prefix = (
                f"[CONTINUING {job.category.upper()} SESSION · NEXT CHALLENGE: {job.name}]\n"
                f"You are continuing your persistent {job.category} solver session. "
                f"Previous workspace knowledge, scripts, and environment context are preserved in this conversation.\n"
                f"Proceed to solve this next challenge:\n\n"
            )
            return prefix + base_prompt
        return base_prompt

    @classmethod
    def _scope_preamble(cls, job: SolverJob) -> list[str]:
        chall_path = job.path.resolve()
        return [
            "Workspace: Current working directory.",
            f"Target Directory: {chall_path}",
            "Boundary: Strictly confined to this challenge directory. DO NOT search, list, or navigate outside (never scan /home, ~, Desktop, Downloads, or parent directories).",
            "Immediate Action: Read ./challenge/README.md and ./challenge/NOTE.md first to understand the challenge. Inspect files in ./challenge/.",
            "Hard Roadblocks: If blocked on a difficult mathematical, cryptographic, reverse-engineering, SAT/SMT, or state reachability subproblem, abstract it into a minimal formal instance and use the `ctf-ask` skill with deterministic local verification.",
        ]

    @classmethod
    def _pwn_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Binary Triage & Architecture Analysis)",
            *cls._scope_preamble(job),
            "",
            "Scope: Focus strictly on defensive software analysis and binary inspection:",
            "1. Inspect binary metadata and mitigations (e.g. readelf, checksec).",
            "2. Map input/output protocol, serialization format, and state machine.",
            "3. Document memory structures, buffers, and control flow in script/analysis.md.",
            "4. Build diagnostic test harnesses in script/ without generating weaponized attack payloads.",
            "5. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _crypto_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Cryptographic Modeling & Reduction)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Extract public parameters (modulus, curves, generators, ciphertexts).",
            "2. Identify the mathematical primitive or algebraic structure.",
            "3. Model the system algebraically; test for structural reductions or parameter flaws.",
            "4. Write verification and decryption scripts in script/ and log math models in script/analysis.md.",
            "5. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _reverse_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Reverse Engineering & Algorithm Recovery)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Identify the verification routine, dispatch table, or transformation layers.",
            "2. Deconstruct custom VM opcodes, S-boxes, or cyclic modular operations.",
            "3. Formulate the algebraic or logic inverse of the check routine.",
            "4. Write standalone decoders/emulators in script/ and document findings in script/analysis.md.",
            "5. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _hardware_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Hardware & Signal Analysis)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Determine signal properties: sample rate, modulation scheme, baud rate, or bus protocol.",
            "2. Build a phased demodulation pipeline in script/ and save visual/data checkpoints.",
            "3. Synthesize intermediate findings into script/analysis.md every stage to avoid token exhaustion.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _web_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Web Source Code & Configuration Audit)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Audit backend source, framework configuration, Dockerfile, and route definitions.",
            "2. Trace request processing and state handling to identify architectural or logic flaws.",
            "3. Verify behavior with localized mock probes in script/ and document in script/analysis.md.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _forensics_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Forensic Artifact & Data Analysis)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Examine file headers, metadata, file systems, or network capture streams.",
            "2. Extract embedded artifacts, hidden payloads, or reconstructed timeline events into script/.",
            "3. Summarize findings and anomalies in script/analysis.md.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _blockchain_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Smart Contract & Blockchain Security Analysis)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Audit smart contract ABI, Solidity/Vyper source code, state variables, and access controls.",
            "2. Identify arithmetic invariants, reentrancy vectors, or flash loan interaction flows.",
            "3. Formulate local reproduction test harnesses in script/ and document state in script/analysis.md.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _ai_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Machine Learning & Model Safety Analysis)",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Inspect model architecture, weights, tokenizer configuration, and prompt templates.",
            "2. Analyze decision boundaries, adversarial sensitivity, or data leakage vectors.",
            "3. Build offline inference probes in script/ and record findings in script/analysis.md.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @classmethod
    def _general_prompt(cls, job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name}",
            *cls._scope_preamble(job),
            "",
            "Instructions:",
            "1. Inspect metadata.json, challenge/NOTE.md, and all challenge attachments.",
            "2. Build diagnostic test harnesses or analysis scripts in script/.",
            "3. Document your hypotheses, findings, and technical notes in script/analysis.md.",
            "4. If a candidate flag is found, save it to flag.txt.",
            "",
            "Protocol: Emit '@@CTF_PROGRESS@@ {\"phase\": \"<phase>\", \"message\": \"<brief>\"}' (phases: recon, modeling, local_verify).",
        ))

    @staticmethod
    def _fallback_triage_prompt(job: SolverJob) -> str:
        return "\n".join((
            f"Challenge: {job.name} (Defensive Architecture & Format Triage)",
            "Workspace: Current working directory.",
            "",
            "Note: Perform defensive software triage only. Do not attempt any exploitation or attack scripts:",
            "1. Document file structures, metadata, architecture, and protocol boundaries.",
            "2. Map function tables, state machines, and configuration parameters.",
            "3. Output a comprehensive technical assessment directly to script/analysis.md.",
        ))
