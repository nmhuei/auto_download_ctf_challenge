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
from ctf_downloader.solver.adapters.gpt import GptSolverAdapter


def test_registered_adapters_include_agy_codex_claude():
    registered = list_registered_adapters()
    assert DEFAULT_SOLVER_ENGINE == "agy"
    assert "agy" in registered
    assert "gpt" in registered
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


def test_category_sessions_isolated_by_engine(tmp_path, monkeypatch):
    import ctf_downloader.storage.global_config as gc
    monkeypatch.setattr(gc, "load_global_config", lambda: {})
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


def test_gpt_adapter_capabilities_and_invocation(tmp_path):
    adp = GptSolverAdapter(binary_override="/home/light/.local/bin/gpt")
    caps = adp.capabilities()
    assert caps.supports_streaming is True
    assert caps.supports_session_resume is True
    assert caps.default_log_filename == "gpt.log"
    assert adp.engine_id == "gpt"

    job = mock.MagicMock()
    job.path = tmp_path
    spec = adp.build_invocation(
        job=job,
        prompt="Analyze reverse engineering binary",
        session_id="sess_1234",
    )
    assert spec.argv[0] == "/home/light/.local/bin/gpt"
    assert spec.argv == [
        "/home/light/.local/bin/gpt", "run", "--resume-session", "sess_1234",
        "-b", "br", "--json", "-p", "Analyze reverse engineering binary",
    ]
    assert spec.log_filename == "gpt.log"


def test_gpt_adapter_stream_line_decoding():
    adp = GptSolverAdapter()

    # 1. CTF Progress JSON
    events = adp.decode_stream_line('@@CTF_PROGRESS@@ {"phase": "exploit", "message": "Buffer overflow reached", "candidate_flag": "flag{pwn_success}"}')
    assert len(events) >= 1
    prog_ev = [e for e in events if e.event_type == "progress"][0]
    assert prog_ev.phase == "exploit"
    assert prog_ev.candidate_flag == "flag{pwn_success}"

    # 2. Flag regex detection in normal output
    events_flag = adp.decode_stream_line("Here is the secret: CTF{web_sqli_bypassed_2026}")
    assert any(e.candidate_flag == "CTF{web_sqli_bypassed_2026}" for e in events_flag)

    # 3. Thinking telemetry
    events_think = adp.decode_stream_line("💭 [Thinking] Analyzing AES S-Box structure")
    assert any(e.event_type == "progress" and "AES S-Box" in (e.message or "") for e in events_think)

    # 4. Tool calling telemetry
    events_tool = adp.decode_stream_line("🔧 [Tool] Calling host_run_command (python3 solve.py)...")
    assert any(e.event_type == "tool_call" for e in events_tool)

    # 5. Classification
    assert adp.classify_exit(0, events_flag) == "completed"


def test_gpt_adapter_decodes_terminal_json_and_does_not_call_it_completed():
    adp = GptSolverAdapter()
    events = adp.decode_stream_line(
        '{"status":"incomplete","lifecycle_status":"upstream_error",'
        '"error":"SSE interrupted"}'
    )

    assert any(event.event_type == "upstream" for event in events)
    assert adp.classify_exit(0, events) == "upstream_error"


def test_gpt_adapter_provisions_and_returns_a_persisted_local_session(monkeypatch, tmp_path):
    import ctf_downloader.solver.adapters.gpt as gpt_module

    adp = GptSolverAdapter(binary_override="/usr/local/bin/gpt")
    job = mock.MagicMock()
    job.display_id = 7
    job.category = "Crypto"
    job.name = "Clockwork"

    completed = mock.MagicMock(stdout='{"status":"created","session_id":"sess_ctf7"}\n')
    run = mock.Mock(return_value=completed)
    monkeypatch.setattr(gpt_module.subprocess, "run", run)

    assert adp.create_session(job) == "sess_ctf7"
    assert run.call_args.args[0] == [
        "/usr/local/bin/gpt", "session", "new", "--json", "--tag", "ctf:7:crypto-clockwork",
    ]


def test_solver_engine_memory_and_persistence(tmp_path, monkeypatch):
    import ctf_downloader.storage.global_config as gc
    fake_global = {}
    monkeypatch.setattr(gc, "load_global_config", lambda: dict(fake_global))
    def _fake_update(mut):
        mut(fake_global)
        return dict(fake_global)
    monkeypatch.setattr(gc, "update_global_config", _fake_update)

    from ctf_downloader.solver.settings import solver_default_engine, set_last_solver_engine
    from ctf_downloader.services.solver_service import SolverService

    # 1. Save and verify workspace-level persistence
    set_last_solver_engine("gpt", tmp_path)
    assert solver_default_engine(tmp_path) == "gpt"

    # 2. SolverService loads remembered engine automatically
    service = SolverService(tmp_path)
    assert service.engine == "gpt"

    # 3. Switching engine updates memory
    set_last_solver_engine("codex", tmp_path)
    assert solver_default_engine(tmp_path) == "codex"
    service2 = SolverService(tmp_path)
    assert service2.engine == "codex"

    # 4. Global memory fallback when persisted globally or without workspace
    set_last_solver_engine("codex", persist_global=True)
    assert solver_default_engine(None) == "codex"
