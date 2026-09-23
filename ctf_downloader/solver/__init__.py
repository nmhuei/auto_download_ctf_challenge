"""Pluggable AI Solver Subsystem for CTF Toolkit."""
from __future__ import annotations

from .base import (
    EngineCapabilities,
    EngineProbeResult,
    InvocationSpec,
    NormalizedSolverEvent,
    SolverAdapter,
)
from .registry import (
    DEFAULT_SOLVER_ENGINE,
    get_solver_adapter,
    list_registered_adapters,
    probe_available_adapters,
    register_solver_adapter,
)

__all__ = [
    "EngineCapabilities",
    "EngineProbeResult",
    "InvocationSpec",
    "NormalizedSolverEvent",
    "SolverAdapter",
    "DEFAULT_SOLVER_ENGINE",
    "get_solver_adapter",
    "list_registered_adapters",
    "probe_available_adapters",
    "register_solver_adapter",
]
