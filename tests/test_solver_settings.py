from pathlib import Path

import pytest

from ctf_downloader.solver.settings import solver_max_workers
from ctf_downloader.services.solver_service import SolverService


def test_workspace_dotenv_configures_solver_worker_limit(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CTF_SOLVER_MAX_WORKERS", raising=False)
    (tmp_path / ".env").write_text("CTF_SOLVER_MAX_WORKERS=5\n", encoding="utf-8")

    assert solver_max_workers(tmp_path) == 5
    assert SolverService(tmp_path).worker_limit == 5


def test_environment_overrides_workspace_dotenv(monkeypatch, tmp_path: Path):
    (tmp_path / ".env").write_text("CTF_SOLVER_MAX_WORKERS=5\n", encoding="utf-8")
    monkeypatch.setenv("CTF_SOLVER_MAX_WORKERS", "2")

    assert solver_max_workers(tmp_path) == 2


def test_solver_rejects_worker_count_above_configured_limit(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CTF_SOLVER_MAX_WORKERS", "2")
    service = SolverService(tmp_path)

    with pytest.raises(ValueError, match="between 1 and 2"):
        service.run("1", workers=3)
