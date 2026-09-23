"""Deterministic prompt routing engine for CTF toolkit.

Implements the 'Prompt-as-Command' paradigm for ultra-terse user interaction:
- Precedence: REJECT_CANDIDATE -> STATUS -> CONTINUE -> SELECT -> HYPOTHESIS -> CONSTRAINT
- Normalizes Vietnamese Telex typing errors (e.g., 'tieens ddooj', 'cos theer', 'cai nafy')
- Routes challenge selectors (e.g., 'pwn5', 'rev12', '/ctf-toolkit Lottery')
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RouteAction(str, Enum):
    SELECT = "SELECT"
    CONTINUE = "CONTINUE"
    STATUS = "STATUS"
    REJECT_CANDIDATE = "REJECT_CANDIDATE"
    HYPOTHESIS = "HYPOTHESIS"
    CONSTRAINT = "CONSTRAINT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RouteDecision:
    action: RouteAction
    target: Optional[str] = None
    raw_prompt: str = ""
    normalized_prompt: str = ""
    reason: str = ""


# Simple telex normalization table for common prompt terms
_TELEX_REPLACEMENTS = [
    (r"\btieens\s+ddooj\b", "tiến độ"),
    (r"\bcos\s+theer\b", "có thể"),
    (r"\bddos\b", "đó"),
    (r"\blaf\b", "là"),
    (r"\bcai\s+nafy\b", "cái này"),
    (r"\btimf\b", "tìm"),
    (r"\bthaajt\b", "thật"),
    (r"\bbaif\b", "bài"),
    (r"\bveiets\b", "viết"),
    (r"\bchauw\b", "chưa"),
    (r"\btuwf\b", "từ"),
    (r"\bdafu\b", "đầu"),
    (r"\bkahcs\b", "khác"),
]

_DECOY_PATTERNS = [
    re.compile(r"\bdecoy\b", re.IGNORECASE),
    re.compile(r"\bflag\s+decoy\b", re.IGNORECASE),
    re.compile(r"\bcái\s+này\s+là\s+flag\s+decoy\b", re.IGNORECASE),
    re.compile(r"\bdecoy\s+flag\b", re.IGNORECASE),
    re.compile(r"\bfake\s*flag\b", re.IGNORECASE),
]

_STATUS_PATTERNS = [
    re.compile(r"^(?:status|tiến\s*độ|tiendo|tieens\s+ddooj|progress|báo\s*cáo)$", re.IGNORECASE),
    re.compile(r"^(?:/status|/statú)$", re.IGNORECASE),
]

_CONTINUE_PATTERNS = [
    re.compile(r"^(?:next|nexxt|continue|tiếp|tiếp\s*tục|go\s*on|proceed)$", re.IGNORECASE),
    re.compile(r"^/ctf(?:-toolkit)?\s+next$", re.IGNORECASE),
]

_SELECT_SHORTHAND = re.compile(
    r"^(?:/ctf(?:-toolkit)?\s+)?([a-zA-Z]+)[-_ ]?(\d+)$",
    re.IGNORECASE,
)

_SELECT_COMMAND = re.compile(
    r"^/ctf(?:-toolkit)?\s+(.+)$",
    re.IGNORECASE,
)

_HYPOTHESIS_PATTERNS = [
    re.compile(r"\b(?:có thể đó là|co the do la|hint hay j|maybe|hypothesis)\b", re.IGNORECASE),
]

_CONSTRAINT_PATTERNS = [
    re.compile(r"\b(?:connect\s+qua\s+lan|lan\s+only|chỉ\s+có\s+thể\s+connect\s+qua\s+lan|server\s+is\s+only\s+be\s+connected\s+by\s+lan)\b", re.IGNORECASE),
    re.compile(r"\b(?:tự\s+bật\s+instance|run\s+mcp|dùng\s+lan)\b", re.IGNORECASE),
]


def normalize_prompt(text: str) -> str:
    """Normalize raw prompt by resolving common telex keystrokes and trimming whitespace."""
    normalized = text.strip()
    for pattern, repl in _TELEX_REPLACEMENTS:
        normalized = re.sub(pattern, repl, normalized, flags=re.IGNORECASE)
    return normalized


def route_prompt(raw_text: str) -> RouteDecision:
    """Route a concise prompt into a deterministic command packet.
    
    Precedence:
    1. REJECT_CANDIDATE (decoy, fake flag)
    2. STATUS (tiến độ, status)
    3. CONTINUE (next, continue)
    4. SELECT (/ctf-toolkit <target>, pwn5, rev12, or challenge slug)
    5. HYPOTHESIS (có thể đó là hint)
    6. CONSTRAINT (LAN only, dynamic instances)
    """
    cleaned = raw_text.strip()
    if not cleaned:
        return RouteDecision(RouteAction.UNKNOWN, raw_prompt=raw_text, reason="empty prompt")

    normalized = normalize_prompt(cleaned)

    # 1. REJECT_CANDIDATE
    for pat in _DECOY_PATTERNS:
        if pat.search(normalized) or pat.search(cleaned):
            return RouteDecision(
                action=RouteAction.REJECT_CANDIDATE,
                raw_prompt=raw_text,
                normalized_prompt=normalized,
                reason="matched decoy/fake flag pattern",
            )

    # 2. STATUS
    for pat in _STATUS_PATTERNS:
        if pat.match(normalized) or pat.match(cleaned):
            return RouteDecision(
                action=RouteAction.STATUS,
                raw_prompt=raw_text,
                normalized_prompt=normalized,
                reason="matched progress/status query",
            )

    # 3. CONTINUE
    for pat in _CONTINUE_PATTERNS:
        if pat.match(normalized) or pat.match(cleaned):
            return RouteDecision(
                action=RouteAction.CONTINUE,
                raw_prompt=raw_text,
                normalized_prompt=normalized,
                reason="matched continuation trigger",
            )

    # 4. SELECT
    # 4a. Shorthand like 'pwn5', 'rev12', 'crypto_3'
    m_short = _SELECT_SHORTHAND.match(cleaned)
    if m_short:
        pfx, num = m_short.group(1), m_short.group(2)
        target = f"{pfx}{num}"
        return RouteDecision(
            action=RouteAction.SELECT,
            target=target,
            raw_prompt=raw_text,
            normalized_prompt=normalized,
            reason=f"matched challenge shorthand category '{pfx}' index '{num}'",
        )

    # 4b. Explicit command '/ctf-toolkit <target>' or '/ctf <target>'
    m_cmd = _SELECT_COMMAND.match(cleaned)
    if m_cmd:
        target = m_cmd.group(1).strip()
        return RouteDecision(
            action=RouteAction.SELECT,
            target=target,
            raw_prompt=raw_text,
            normalized_prompt=normalized,
            reason="matched explicit /ctf-toolkit command",
        )

    # 5. HYPOTHESIS
    for pat in _HYPOTHESIS_PATTERNS:
        if pat.search(normalized) or pat.search(cleaned):
            return RouteDecision(
                action=RouteAction.HYPOTHESIS,
                raw_prompt=raw_text,
                normalized_prompt=normalized,
                reason="matched hypothesis/hint prompt",
            )

    # 6. CONSTRAINT
    for pat in _CONSTRAINT_PATTERNS:
        if pat.search(normalized) or pat.search(cleaned):
            return RouteDecision(
                action=RouteAction.CONSTRAINT,
                raw_prompt=raw_text,
                normalized_prompt=normalized,
                reason="matched environment/runtime constraint",
            )

    # If it is a single word or short identifier without spaces, test as candidate challenge name
    if len(cleaned.split()) == 1 and re.match(r"^[a-zA-Z0-9_\-]+$", cleaned):
        return RouteDecision(
            action=RouteAction.SELECT,
            target=cleaned,
            raw_prompt=raw_text,
            normalized_prompt=normalized,
            reason="single token assumed to be challenge identifier",
        )

    return RouteDecision(
        action=RouteAction.UNKNOWN,
        raw_prompt=raw_text,
        normalized_prompt=normalized,
        reason="no deterministic pattern matched",
    )
