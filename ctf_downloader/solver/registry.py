from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Dict, List, Optional, Type

from .base import EngineProbeResult, SolverAdapter
import ctf_downloader.solver.adapters as adapters_pkg

# Explicit imports for direct backwards-compatible symbol access
from .adapters.agy import AgySolverAdapter
from .adapters.claude import ClaudeSolverAdapter
from .adapters.codex import CodexSolverAdapter
from .adapters.generic import GenericCliAdapter
from .adapters.gpt import GptSolverAdapter

DEFAULT_SOLVER_ENGINE = "agy"

_REGISTRY: Dict[str, Type[SolverAdapter]] = {}
_DISCOVERED: bool = False


def _auto_discover_adapters() -> None:
    """Dynamically scan ctf_downloader.solver.adapters directory and register all SolverAdapter subclasses."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    for finder, name, ispkg in pkgutil.iter_modules(adapters_pkg.__path__):
        try:
            mod = importlib.import_module(f"ctf_downloader.solver.adapters.{name}")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, SolverAdapter)
                    and attr is not SolverAdapter
                ):
                    engine_id = getattr(attr, "engine_id", None)
                    if engine_id:
                        _REGISTRY[str(engine_id).strip().lower()] = attr
        except Exception:
            pass
    _DISCOVERED = True


def register_solver_adapter(engine_id: str, adapter_cls: Type[SolverAdapter]) -> None:
    """Register or override a solver adapter class."""
    _auto_discover_adapters()
    clean_id = str(engine_id).strip().lower()
    _REGISTRY[clean_id] = adapter_cls


def get_solver_adapter(engine_id: Optional[str] = None, **kwargs) -> SolverAdapter:
    """Instantiate a solver adapter by engine ID. Defaults to 'agy'."""
    _auto_discover_adapters()
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
    _auto_discover_adapters()
    return sorted(_REGISTRY.keys())


def probe_available_adapters() -> List[EngineProbeResult]:
    """Probe host system to inspect installation and readiness of all registered adapters."""
    _auto_discover_adapters()
    results: List[EngineProbeResult] = []
    for key in sorted(_REGISTRY.keys()):
        adapter = _REGISTRY[key]()
        results.append(adapter.probe())
    return results
