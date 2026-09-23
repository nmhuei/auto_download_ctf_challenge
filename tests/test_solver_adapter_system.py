"""Unit tests for Pluggable Solver Adapter architecture."""
from pathlib import Path
from unittest import mock
import pytest

from ctf_downloader.solver import (
    DEFAULT_SOLVER_ENGINE,
    get_solver_adapter,
    list_registered_adapters,
    probe_available_adapters,
)
from ctf_downloader.solver.adapters.agy import AgySolverAdapter
from ctf_downloader.solver.adapters.codex import CodexSolverAdapter
from ctf_downloader.solver.adapters.claude import ClaudeSolverAdapter
from ctf_downloader.solver.adapters.generic import GenericCliAdapter


def test_registered_adapters_include_agy_codex_claude():
    registered = list_registered_adapters()
    assert DEFAULT_SOLVER_ENGINE == "agy"
    assert "agy" in registered
    assert "codex" in registered
    assert "claude" in registered
    assert "generic" in registered


def test_get_solver_adapter_defaults_to_agy():
    adp = get_solver_adapter()
    assert isinstance(adp, AgySolverAdapter)
    assert adp.engine_id == "agy"


def test_get_solver_adapter_unknown_raises_keyerror():
    with pytest.raises(KeyError, match="Unknown solver engine 'unknown_future_agent'"):
        get_solver_adapter("unknown_future_agent")


def test_agy_adapter_capabilities_and_invocation(tmp_path):
    adp = AgySolverAdapter(binary_override="/usr/bin/agy")
    caps = adp.capabilities()
    assert caps.supports_streaming is True
    assert caps.supports_session_resume is True
    assert caps.default_log_filename == "agy.log"

    job = mock.MagicMock()
    job.path = tmp_path
    spec = adp.build_invocation(
        job=job,
        prompt="Solve this challenge",
        session_id="test-conv-123",
        timeout=300,
    )

    assert spec.argv[0] == "/usr/bin/agy"
    assert "--conversation" in spec.argv
    assert "test-conv-123" in spec.argv
    assert "--print" in spec.argv
    assert "Solve this challenge" in spec.argv
    assert spec.log_filename == "agy.log"


def test_agy_stream_line_decoding():
    adp = AgySolverAdapter()

    # 1. Progress event
    events = adp.decode_stream_line('{"content": "Working on it... @@CTF_PROGRESS@@ {\\"phase\\": \\"recon\\", \\"message\\": \\"Binary inspected\\", \\"candidate_flag\\": \\"flag{123}\\"}"}')
    assert len(events) >= 1
    prog_ev = [e for e in events if e.event_type == "progress"][0]
    assert prog_ev.phase == "recon"
    assert prog_ev.message == "Binary inspected"
    assert prog_ev.candidate_flag == "flag{123}"

    # 2. Timeout event
    timeout_events = adp.decode_stream_line("[agy] print timeout after 360s")
    assert any(e.event_type == "timeout" for e in timeout_events)

    # 3. Refusal event
    refusal_events = adp.decode_stream_line("Refusal triggered by safety policy against cybersecurity exploitation")
    assert any(e.event_type == "refusal" for e in refusal_events)


def test_codex_adapter_capabilities_and_invocation(tmp_path):
    adp = CodexSolverAdapter(binary_override="/usr/bin/codex")
    caps = adp.capabilities()
    assert caps.supports_streaming is True
    assert adp.engine_id == "codex"

    job = mock.MagicMock()
    job.path = tmp_path
    spec = adp.build_invocation(
        job=job,
        prompt="Analyze binary",
        session_id=None,
    )
    assert spec.argv[0] == "/usr/bin/codex"
    assert "exec" in spec.argv
    assert "--json" in spec.argv
    assert "--cd" in spec.argv
    assert str(tmp_path) in spec.argv


def test_claude_adapter_capabilities_and_invocation(tmp_path):
    adp = ClaudeSolverAdapter(binary_override="/usr/bin/claude")
    caps = adp.capabilities()
    assert caps.supports_streaming is True
    assert adp.engine_id == "claude"

    job = mock.MagicMock()
    job.path = tmp_path
    spec = adp.build_invocation(
        job=job,
        prompt="Analyze code",
        session_id=None,
    )
    assert spec.argv[0] == "/usr/bin/claude"
    assert "-p" in spec.argv
    assert "Analyze code" in spec.argv


def test_generic_adapter_capabilities_and_invocation(tmp_path):
    adp = GenericCliAdapter(command=["python3", "harness.py"])
    assert adp.engine_id == "generic"

    job = mock.MagicMock()
    job.path = tmp_path
    spec = adp.build_invocation(
        job=job,
        prompt="Do work",
    )
    assert spec.argv[:2] == ["python3", "harness.py"]
    assert "Do work" in spec.argv


def test_probe_available_adapters_runs_safely():
    results = probe_available_adapters()
    assert len(results) >= 4
    engine_ids = [r.engine_id for r in results]
    assert "agy" in engine_ids
    assert "codex" in engine_ids
    assert "claude" in engine_ids
    assert "generic" in engine_ids


def test_solver_service_engine_integration_and_dual_logging(tmp_path, monkeypatch):
    import subprocess
    from ctf_downloader.services.solver_service import SolverService

    # Setup dummy challenge workspace
    ch_dir = tmp_path / "Crypto" / "ch1"
    ch_dir.mkdir(parents=True)
    (ch_dir / "challenge").mkdir()
    (ch_dir / "challenge" / "chall.py").write_text("print('test')", encoding="utf-8")
    (ch_dir / "metadata.json").write_text('{"id": 1, "name": "ch1", "category": "Crypto"}', encoding="utf-8")

    service = SolverService(tmp_path, engine="codex")
    jobs = service.scan()
    assert len(jobs) == 1
    job = jobs[0]

    launched = []
    class _MockProc:
        pid = 99999
        returncode = 0
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (launched.extend(cmd), _MockProc())[1])

    # 1. Start worker with codex engine
    service._start_worker(job, ["codex"])
    state = service.read_job(job)
    assert state.get("engine") == "codex"
    assert "codex" in launched[0]

    # 2. Verify dual logging to agy.log and worker.log
    service._append_output(job, "hello world\n", log_filename="worker.log")
    assert (job.script_dir / "agy.log").is_file()
    assert (job.script_dir / "worker.log").is_file()
    assert "hello world" in (job.script_dir / "agy.log").read_text(encoding="utf-8")
    assert "hello world" in (job.script_dir / "worker.log").read_text(encoding="utf-8")


def test_solver_service_run_with_codex_engine_invokes_codex_binary(tmp_path, monkeypatch):
    import subprocess
    from ctf_downloader.services.solver_service import SolverService

    ch_dir = tmp_path / "Web" / "web_task"
    ch_dir.mkdir(parents=True)
    (ch_dir / "challenge").mkdir()
    (ch_dir / "challenge" / "app.py").write_text("print('web')", encoding="utf-8")
    (ch_dir / "metadata.json").write_text('{"id": 101, "name": "web_task", "category": "Web"}', encoding="utf-8")

    service = SolverService(tmp_path)
    launched = []

    class _MockProc:
        pid = 88888
        returncode = 0
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (launched.extend(cmd), _MockProc())[1])

    # Run without agy_command override, specifying engine="codex"
    results = service.run("1", workers=1, engine="codex", acquire_lock=False)
    assert len(results) == 1
    # Verify the executed command was indeed codex, not agy
    assert len(launched) > 0
    assert "codex" in launched[0]
    assert "exec" in launched
    assert "--json" in launched


def test_category_sessions_isolated_by_engine(tmp_path):
    from ctf_downloader.services.solver_service import SolverService

    service = SolverService(tmp_path)
    service.save_category_session("Crypto", "conv-agy-master", engine="agy")
    service.save_category_session("Crypto", "conv-codex-master", engine="codex")

    # Engine agy retrieves agy session
    assert service.get_category_session("Crypto", engine="agy") == "conv-agy-master"
    # Engine codex retrieves codex session, avoiding cross-contamination
    assert service.get_category_session("Crypto", engine="codex") == "conv-codex-master"
    # Default engine query (agy) retrieves agy session
    assert service.get_category_session("Crypto") == "conv-agy-master"


