"""Incident taxonomy, recovery hints, and safety policies for Antigravity (agy / BQA).

This module defines structured incident models for non-pull operations (instance lifecycle,
platform sync, submission, and general runtime anomalies) that allow the CLI and TUI
to coordinate with Antigravity / BQA safely without leaking credentials.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple


class IncidentKind(str, Enum):
    """Categorized incident types that determine how recovery should be routed."""
    UNSUPPORTED_PLATFORM_FEATURE = "unsupported_platform_feature"
    PLATFORM_SCHEMA_OR_API_DRIFT = "platform_schema_or_api_drift"
    AUTH_OR_SESSION_EXPIRED = "auth_or_session_expired"
    UNCAUGHT_RUNTIME_EXCEPTION = "uncaught_runtime_exception"


@dataclass(frozen=True)
class RetryPlan:
    """Safe command invocation plan to retry after a successful repair."""
    argv: Tuple[str, ...]
    safe_to_retry: bool = False


@dataclass(frozen=True)
class RecoveryHint:
    """Non-secret recovery instructions attached to a Diagnostic."""
    kind: IncidentKind
    operation: str
    platform: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    retry: Optional[RetryPlan] = None
    repair_eligible: bool = True
    error_code: str = "CTF-INSTANCE-U01"


def may_spawn_agy(
    env: Optional[Mapping[str, str]] = None,
    stdin: Optional[Any] = None,
) -> bool:
    """Check whether it is safe and policy-compliant to spawn Antigravity (agy / BQA).

    Prevents automated execution during automated tests, CI, recursive retries,
    or non-interactive pipelines.
    """
    env = env if env is not None else os.environ
    stdin = stdin if stdin is not None else sys.stdin

    if env.get("PYTEST_CURRENT_TEST"):
        return False
    if env.get("CI"):
        return False
    if env.get("CTF_DISABLE_BQA") == "1":
        return False
    if env.get("CTF_BQA_RETRY") == "1":
        return False

    isatty_fn = getattr(stdin, "isatty", None)
    if callable(isatty_fn):
        return bool(isatty_fn())
    return False


__all__ = [
    "IncidentKind",
    "RetryPlan",
    "RecoveryHint",
    "may_spawn_agy",
]
