"""Solver Engine Registry and Discovery for AI Agent Adapters.

Centralizes discovery, registration, and factory instantiation of AI solver adapters.
Defaults strictly to Antigravity CLI ('agy') to preserve existing behavior.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from .base import EngineProbeResult, SolverAdapter
from .adapters.agy import AgySolverAdapter
from .adapters.claude import ClaudeSolverAdapter
from .adapters.codex import CodexSolverAdapter
from .adapters.generic import GenericCliAdapter

DEFAULT_SOLVER_ENGINE = "agy"

_REGISTRY: Dict[str, Type[SolverAdapter]] = {
    "agy": AgySolverAdapter,
    "codex": CodexSolverAdapter,
    "claude": ClaudeSolverAdapter,
    "generic": GenericCliAdapter,
}


def register_solver_adapter(engine_id: str, adapter_cls: Type[SolverAdapter]) -> None:
    """Register or override a solver adapter class."""
    clean_id = str(engine_id).strip().lower()
    _REGISTRY[clean_id] = adapter_cls


def get_solver_adapter(engine_id: Optional[str] = None, **kwargs) -> SolverAdapter:
    """Instantiate a solver adapter by engine ID. Defaults to 'agy'."""
    if engine_id is None:
        key = DEFAULT_SOLVER_ENGINE
    else:
        key = str(engine_id).strip().lower()
        if key not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY.keys()))
            raise KeyError(f"Unknown solver engine '{engine_id}'. Available engines: {available}")
    cls = _REGISTRY[key]
    return cls(**kwargs)


def list_registered_adapters() -> List[str]:
    """Return list of all registered solver engine IDs."""
    return sorted(_REGISTRY.keys())


def probe_available_adapters() -> List[EngineProbeResult]:
    """Probe host system to inspect installation and readiness of all registered adapters."""
    results: List[EngineProbeResult] = []
    for key in sorted(_REGISTRY.keys()):
        adapter = _REGISTRY[key]()
        results.append(adapter.probe())
    return results
