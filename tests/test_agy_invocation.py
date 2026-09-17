import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from ctf_downloader.bqa_recovery import BqaRecovery, BqaSessionStore, RecoveryIncident
from ctf_downloader.services.health_service import HealthService
from ctf_downloader.services.solver_service import SolverService
from ctf_downloader.utils.agy_resolver import (
    extract_json_payload,
    is_agy_available,
    resolve_agy_binary,
)


def _make_challenge(workspace: Path, category: str, name: str, chal_id: int, *, source: bool = True) -> Path:
    root = workspace / category / name
    (root / "challenge").mkdir(parents=True, exist_ok=True)
    (root / "solver").mkdir(parents=True, exist_ok=True)
    (root / "script").mkdir(parents=True, exist_ok=True)
    if source:
        (root / "challenge" / "app.py").write_text("print('test')", encoding="utf-8")
    meta = {
        "id": chal_id,
        "name": name,
        "category": category,
        "points": 100,
    }
    (root / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "challenge" / "NOTE.md").write_text("# Note", encoding="utf-8")
    return root


def test_resolve_agy_binary_explicit_path():
    assert resolve_agy_binary("/bin/sh") == "/bin/sh"
    assert resolve_agy_binary("./relative/bin") == "./relative/bin"


def test_resolve_agy_binary_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom_agy"
    custom.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    custom.chmod(0o755)

    monkeypatch.setenv("AGY_BIN", str(custom))
    assert resolve_agy_binary("agy") == str(custom)
    assert is_agy_available("agy") is True


def test_resolve_agy_binary_local_bin_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.delenv("AGY_PATH", raising=False)
    # Clear PATH
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))

    fake_home = tmp_path / "home"
    fake_local = fake_home / ".local" / "bin" / "agy"
    fake_local.parent.mkdir(parents=True, exist_ok=True)
    fake_local.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake_local.chmod(0o755)

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert resolve_agy_binary("agy") == str(fake_local)
    assert is_agy_available("agy") is True


def test_extract_json_payload_variations():
    # Pure JSON
    assert extract_json_payload('{"status": "SUCCESS", "conversation_id": "c1"}') == {
        "status": "SUCCESS",
        "conversation_id": "c1",
    }

    # JSON preceded by stderr log / warnings
    prefixed = textwrap.dedent("""
        error: invalid model selection (--model "flash")
        Available models:
          Gemini 3.8 Flash
        {"status": "ERROR", "conversation_id": "", "error": "bad model"}
    """).strip()
    extracted = extract_json_payload(prefixed)
    assert extracted is not None
    assert extracted["status"] == "ERROR"
    assert extracted["error"] == "bad model"

    # NDJSON output (take valid JSON line)
    ndjson = '{"event": "init"}\n{"event": "result", "result": {"status": "SUCCESS"}}'
    assert extract_json_payload(ndjson) is not None

    # Completely invalid output
    assert extract_json_payload("Just some random text\nAnother line") is None
    assert extract_json_payload("") is None


def test_solver_service_missing_executable_fails_without_crash(tmp_path):
    _make_challenge(tmp_path, "Web", "web1", 1, source=True)
    service = SolverService(tmp_path)

    # agy_command points to non-existent executable
    results = service.run("1", workers=1, agy_command=["/definitely/nonexistent/agy_bin_xyz"])
    assert len(results) == 1
    assert results[0]["state"] == "failed"
    assert results[0]["phase"] == "crashed"
    assert results[0]["error_code"] == "E_WORKER_CRASH"
    assert "Agy worker failed to start" in results[0]["message"]


def test_solver_service_stream_json_progress_extraction(tmp_path):
    root = _make_challenge(tmp_path, "Web", "web2", 1, source=True)

    fake_worker = tmp_path / "fake_stream_worker.py"
    fake_worker.write_text(textwrap.dedent("""
        import json
        import pathlib
        import sys
        import time

        # Emit stream-json line containing progress
        event = {
            "event": "step_update",
            "step_update": {
                "text_delta": '@@CTF_PROGRESS@@ {"phase":"testing_exploit","message":"sending payload"}\\n'
            }
        }
        print(json.dumps(event), flush=True)
        time.sleep(0.05)

        cwd = pathlib.Path.cwd()
        (cwd / "solver" / "solve.py").write_text("print('win')", encoding="utf-8")
        (cwd / "script" / "worker-report.json").write_text(
            json.dumps({"local_verification": "passed", "summary": "done"}), encoding="utf-8"
        )
    """), encoding="utf-8")

    service = SolverService(tmp_path)
    results = service.run("1", workers=1, agy_command=[sys.executable, str(fake_worker)])

    assert len(results) == 1
    assert results[0]["state"] == "completed"
    progress = json.loads((root / "script" / "progress.json").read_text(encoding="utf-8"))
    assert progress["phase"] == "testing_exploit"
    assert progress["message"] == "sending payload"


def test_bqa_repair_handles_stderr_and_error_message(tmp_path):
    class StderrProcess:
        def __init__(self):
            out = textwrap.dedent("""
                [agy] WARNING: Some initial warning
                Available models:
                  Gemini 3.8 Flash
                {"status": "ERROR", "conversation_id": "", "error": "model quota exhausted"}
            """).strip()
            self.stdout = textwrap.dedent(out)
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        p = StderrProcess()
        # Mocking readline on stdout
        from io import StringIO
        p.stdout = StringIO(p.stdout)
        return p

    store = BqaSessionStore(tmp_path / "sessions.json")
    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=store,
        popen_factory=fake_popen,
        status_writer=lambda s: None,
        revision_provider=lambda _r: "rev1",
    )
    incident = RecoveryIncident.from_failure(["pull", "-u", "https://ctf.test"], 1)
    result = recovery.repair(incident)

    assert result.returncode == 1
    assert "model quota exhausted" in result.reason


def test_bqa_repair_handles_unstartable_binary(tmp_path):
    def failing_popen(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "agy")

    store = BqaSessionStore(tmp_path / "sessions.json")
    recovery = BqaRecovery(
        source_root=tmp_path,
        session_store=store,
        popen_factory=failing_popen,
        status_writer=lambda s: None,
    )
    incident = RecoveryIncident.from_failure(["pull"], 1)
    result = recovery.repair(incident)

    assert result.returncode == 127
    assert "No such file or directory" in result.reason or "FileNotFoundError" in result.reason


def test_doctor_runtime_reports_agy_cli():
    report = HealthService.check_runtime()
    agy_check = next((c for c in report.checks if c.name == "Agy CLI"), None)
    assert agy_check is not None
    assert agy_check.required is False


def test_solver_recognizes_non_code_ctf_artifacts_as_source(tmp_path):
    root = tmp_path / "Crypto" / "cipher"
    (root / "challenge").mkdir(parents=True)
    (root / "challenge" / "ciphertext.txt").write_text("c = 12345", encoding="utf-8")
    (root / "metadata.json").write_text(json.dumps({"id": 1, "name": "cipher", "category": "Crypto"}), encoding="utf-8")

    service = SolverService(tmp_path)
    job = service.scan()[0]
    assert job.has_source is True

    # Test extensionless binary
    root_pwn = tmp_path / "Pwn" / "binary_chal"
    (root_pwn / "challenge").mkdir(parents=True)
    vuln_bin = root_pwn / "challenge" / "vuln"
    vuln_bin.write_bytes(b"\x7fELFfake")
    vuln_bin.chmod(0o755)
    (root_pwn / "metadata.json").write_text(json.dumps({"id": 2, "name": "binary_chal", "category": "Pwn"}), encoding="utf-8")

    job_pwn = service.scan()[1]
    assert job_pwn.has_source is True


def test_solver_runs_instance_challenge_even_without_local_source(tmp_path):
    root = tmp_path / "Web" / "blackbox"
    (root / "challenge").mkdir(parents=True)
    (root / "solver").mkdir(parents=True)
    (root / "script").mkdir(parents=True)
    meta = {
        "id": 1,
        "name": "blackbox",
        "category": "Web",
        "connection_info": "http://example.com:8080",
        "instance_info": {"is_container": True},
    }
    (root / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    worker = tmp_path / "fake_web_worker.py"
    worker.write_text(textwrap.dedent("""
        import pathlib, json
        root = pathlib.Path.cwd()
        (root / "solver" / "solve.py").write_text("print('pwned')\\n", encoding="utf-8")
        (root / "script" / "worker-report.json").write_text(
            '{"local_verification":"passed"}', encoding="utf-8"
        )
    """), encoding="utf-8")

    service = SolverService(tmp_path)
    results = service.run("1", workers=1, agy_command=[sys.executable, str(worker)])
    assert results[0]["state"] == "completed"


def test_solver_truncates_log_preventing_old_filter_bleed(tmp_path):
    root = _make_challenge(tmp_path, "Web", "sticky_filter", 1, source=True)
    # Pre-populate agy.log with old filter error from a previous run
    (root / "script" / "agy.log").write_text("Blocked by cyber safety filters!\n", encoding="utf-8")

    worker = tmp_path / "fake_clean_worker.py"
    worker.write_text(textwrap.dedent("""
        import pathlib, json
        root = pathlib.Path.cwd()
        (root / "solver" / "solve.py").write_text("print('fixed')\\n", encoding="utf-8")
        (root / "script" / "worker-report.json").write_text(
            '{"local_verification":"passed"}', encoding="utf-8"
        )
    """), encoding="utf-8")

    service = SolverService(tmp_path)
    results = service.run("1", workers=1, agy_command=[sys.executable, str(worker)])

    # The new attempt should not be flagged as filtered because the old log was truncated
    assert results[0]["state"] == "completed"
    assert results[0].get("filter_detected") is not True


def test_solver_auto_hoards_candidate_flag(tmp_path):
    root = _make_challenge(tmp_path, "Crypto", "flag_chal", 1, source=True)

    worker = tmp_path / "fake_flag_worker.py"
    worker.write_text(textwrap.dedent("""
        import pathlib, json
        root = pathlib.Path.cwd()
        (root / "solver" / "solve.py").write_text("print('FLAG{found_in_solver_123}')\\n", encoding="utf-8")
        (root / "script" / "worker-report.json").write_text(
            '{"local_verification":"passed","flag":"FLAG{found_in_solver_123}"}', encoding="utf-8"
        )
    """), encoding="utf-8")

    service = SolverService(tmp_path)
    results = service.run("1", workers=1, agy_command=[sys.executable, str(worker)])

    assert results[0]["state"] == "completed"
    assert results[0]["candidate_flag"] == "FLAG{found_in_solver_123}"

    # Verify flag is hoarded in status.json
    status = service.repo.read_status(root / "metadata.json")
    assert status["flag"]["value"] == "FLAG{found_in_solver_123}"
    assert status["flag"]["state"] == "hoarded"
