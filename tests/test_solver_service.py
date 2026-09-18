import json
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest
from unittest.mock import patch

from ctf_downloader.services.solver_service import SolverService, SolverSelectionError


def make_challenge(workspace: Path, category: str, name: str, challenge_id: int,
                   *, source: bool = False, instance: bool = False) -> Path:
    root = workspace / category / name
    (root / "challenge").mkdir(parents=True)
    (root / "script").mkdir()
    (root / "solver").mkdir()
    (root / "challenge" / "README.md").write_text("# challenge", encoding="utf-8")
    (root / "challenge" / "NOTE.md").write_text("# note", encoding="utf-8")
    if source:
        (root / "challenge" / "app.py").write_text("print('service')", encoding="utf-8")
    metadata = {
        "id": challenge_id,
        "name": name,
        "category": category,
        "connection_info": "nc example.org 31337" if instance else "",
        "instance_info": {"is_container": instance},
    }
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


def test_scan_classifies_source_and_instance_and_assigns_stable_display_ids():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "portal", 20, source=True, instance=True)
        make_challenge(workspace, "Crypto", "rsa", 10, source=False, instance=False)

        jobs = SolverService(workspace).scan()

        assert [(job.display_id, job.category, job.name, job.has_source, job.has_instance)
                for job in jobs] == [
                    (1, "Crypto", "rsa", False, False),
                (2, "Web", "portal", True, True),
            ]


def test_scan_treats_all_platform_solved_states_as_solved():
    """Solve all must agree with the Tree View's platform-solved markers."""
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        for challenge_id, solve_state in enumerate(
            ("solved_by_me", "solved_by_team", "solved_other"), start=1
        ):
            root = make_challenge(workspace, "Web", solve_state, challenge_id, source=True)
            metadata_path = root / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["status"] = {"solve": solve_state}
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        assert [job.is_solved for job in SolverService(workspace).scan()] == [True, True, True]


def test_queue_eligibility_ignores_malformed_cached_flag():
    """A partial worker value is not a hoarded flag and must not block BQA."""
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Web", "partial", 1, source=True)
        (root / "script" / "worker-state.json").write_text(
            json.dumps({"candidate_flag": "CTF{partial"}), encoding="utf-8"
        )

        service = SolverService(workspace)
        eligibility = service.queue_eligibility(service.scan()[0])

        assert eligibility.ready is True
        assert eligibility.local_flag is None


def test_per_category_run_rechecks_platform_solve_before_launching_worker():
    """The daemon must enforce Solve all eligibility after the menu confirmation."""
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Web", "already_solved", 1, source=True)
        metadata_path = root / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["status"] = {"solve": "solved_by_team"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        worker = workspace / "fake_agy.py"
        worker.write_text(
            "from pathlib import Path; Path('script/started').write_text('yes')\n",
            encoding="utf-8",
        )

        SolverService(workspace).run(
            "1", workers=1, per_category=True,
            agy_command=[sys.executable, str(worker)],
        )

        assert not (root / "script" / "started").exists()


def test_select_ids_rejects_unknown_and_duplicate_ids():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Misc", "one", 1)
        service = SolverService(workspace)

        with pytest.raises(SolverSelectionError, match="không hợp lệ"):
            service.select_ids("1,1")
        with pytest.raises(SolverSelectionError, match="không tồn tại"):
            service.select_ids("2")
        with pytest.raises(SolverSelectionError, match="không hợp lệ"):
            service.select_ids("²")
        with pytest.raises(SolverSelectionError, match="không hợp lệ"):
            service.select_ids("1,²")
        with pytest.raises(SolverSelectionError, match="không hợp lệ"):
            service.select_ids("①")


def test_three_worker_pool_persists_progress_logs_and_final_solver():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        for index in range(4):
            make_challenge(workspace, "Web", f"job-{index}", index, source=True)

        worker = workspace / "fake_agy.py"
        worker.write_text(textwrap.dedent("""
            import pathlib
            import sys
            import time
            print('@@CTF_PROGRESS@@ {"phase":"local_verify","message":"testing locally"}', flush=True)
            time.sleep(0.05)
            root = pathlib.Path.cwd()
            (root / "solver" / "solve.py").write_text("print('ok')\\n", encoding="utf-8")
            (root / "script" / "worker-report.json").write_text(
                '{"local_verification":"passed","summary":"fake local check"}', encoding="utf-8")
            print('solver complete', flush=True)
        """), encoding="utf-8")

        service = SolverService(workspace)
        results = service.run("1,2,3,4", workers=3,
                              agy_command=[sys.executable, str(worker)])

        assert [result["state"] for result in results] == ["completed"] * 4
        assert service.max_active_workers <= 3
        for job in service.scan():
            state = service.read_job(job)
            assert state["state"] == "completed"
            assert state["phase"] == "completed"
            assert "solver complete" in (job.path / "script" / "agy.log").read_text(encoding="utf-8")
            assert json.loads((job.path / "script" / "progress.json").read_text(encoding="utf-8"))["phase"] == "local_verify"
            assert (job.path / "solver" / "solve.py").is_file()


def test_recover_marks_dead_running_worker_as_crashed():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Pwn", "dead", 1)
        state_path = root / "script" / "worker-state.json"
        state_path.write_text(json.dumps({"state": "running", "pid": 99999999}), encoding="utf-8")

        service = SolverService(workspace)
        assert service.recover_stale_jobs() == 1
        assert service.read_job(service.scan()[0])["error_code"] == "E_WORKER_CRASH"


def test_prompt_is_category_aware_and_scoped_to_current_directory():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "rsa", 1, source=True, instance=True)
        make_challenge(workspace, "Pwnable", "arena", 2, source=True, instance=True)
        service = SolverService(workspace)
        jobs = service.scan()

        crypto_prompt = service.build_prompt(jobs[0])
        assert "Challenge: rsa (Cryptographic Modeling & Reduction)" in crypto_prompt
        assert "Workspace: Current working directory." in crypto_prompt
        assert "Extract public parameters" in crypto_prompt

        pwn_prompt = service.build_prompt(jobs[1])
        assert "Challenge: arena (Binary Triage & Architecture Analysis)" in pwn_prompt
        assert "Workspace: Current working directory." in pwn_prompt
        assert "Focus strictly on defensive software analysis" in pwn_prompt

        fallback_prompt = service.build_prompt(jobs[1], fallback_mode=True)
        assert "Defensive Architecture & Format Triage" in fallback_prompt


def test_existing_downloader_template_is_not_a_completed_solver_without_worker_report():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        challenge = make_challenge(workspace, "Crypto", "template", 1, source=True)
        (challenge / "solver" / "solve.py").write_text("# generated template\n", encoding="utf-8")
        worker = workspace / "fake_agy.py"
        worker.write_text("print('finished')\n", encoding="utf-8")

        result = SolverService(workspace).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "failed"
        assert result["error_code"] == "E_VERIFY_LOCAL"


def test_filter_response_is_marked_filtered_before_local_verification():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Pwn", "blocked", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text("print(\"This request was blocked by Gemini's filters.\")\n", encoding="utf-8")

        result = SolverService(workspace).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "filtered"
        assert result["error_code"] == "E_FILTER"


def test_filter_is_sticky_when_refusal_is_not_last_output_line():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Pwn", "blocked-middle", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text(
            "print(\"This request was blocked by Gemini's filters.\", flush=True)\n"
            "print('ordinary trailing cleanup line', flush=True)\n",
            encoding="utf-8",
        )

        result = SolverService(workspace).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "filtered"
        assert result["error_code"] == "E_FILTER"
        assert result["filter_detected"] is True


def test_state_updates_from_reader_and_scheduler_do_not_lose_fields(monkeypatch):
    import threading

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "race", 1, source=True)
        service = SolverService(workspace)
        job = service.scan()[0]
        barrier = threading.Barrier(2)
        original_read = service.read_job

        def synchronized_stale_read(target):
            state = original_read(target)
            barrier.wait(timeout=2)
            return state

        monkeypatch.setattr(service, "read_job", synchronized_stale_read)
        first = threading.Thread(target=lambda: service._write_job(job, pid=1234))
        second = threading.Thread(target=lambda: service._write_job(job, heartbeat_at="heartbeat"))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()

        state = json.loads(job.state_path.read_text(encoding="utf-8"))
        assert state["pid"] == 1234
        assert state["heartbeat_at"] == "heartbeat"


def test_silent_worker_is_marked_stalled_before_hard_timeout():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Misc", "silent", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

        started = time.monotonic()
        result = SolverService(workspace, timeout_seconds=5, stale_seconds=0.05).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert time.monotonic() - started < 2
        assert result["state"] == "failed"
        assert result["phase"] == "stalled"
        assert result["error_code"] == "E_STALLED"


def test_no_source_job_is_skipped_without_launching_worker():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        challenge = make_challenge(workspace, "Forensics", "artifact-only", 1, source=False)
        worker = workspace / "fake_agy.py"
        worker.write_text(
            "from pathlib import Path\nPath('script/launched').write_text('yes')\n",
            encoding="utf-8",
        )

        result = SolverService(workspace).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "skipped_no_input"
        assert result["phase"] == "skipped"
        assert result["error_code"] is None
        assert not (challenge / "script" / "launched").exists()
        assert not (challenge / "script" / "agy.log").exists()


def test_worker_requests_stream_json_so_internal_agy_activity_refreshes_heartbeat():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "streaming", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text(textwrap.dedent("""
            import json
            import pathlib
            import sys
            import time

            root = pathlib.Path.cwd()
            try:
                output_index = sys.argv.index('--output-format')
                streaming = sys.argv[output_index + 1] == 'stream-json'
            except (ValueError, IndexError):
                streaming = False

            if not streaming:
                time.sleep(1)
                raise SystemExit(0)

            for index in range(6):
                print(json.dumps({"event": "step_update", "index": index}), flush=True)
                time.sleep(0.03)

            (root / "solver" / "solve.py").write_text("print('ok')\\n", encoding="utf-8")
            (root / "script" / "worker-report.json").write_text(
                '{"local_verification":"passed","summary":"stream activity kept worker alive"}',
                encoding="utf-8",
            )
        """), encoding="utf-8")

        result = SolverService(workspace, timeout_seconds=2, stale_seconds=0.08).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "completed"
        assert result["error_code"] is None


def test_agy_print_timeout_is_set_beyond_scheduler_hard_timeout():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "timeout-contract", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text(textwrap.dedent("""
            import pathlib
            import sys

            root = pathlib.Path.cwd()
            try:
                index = sys.argv.index('--print-timeout')
                value = sys.argv[index + 1]
                seconds = int(value.removesuffix('s'))
            except (ValueError, IndexError):
                seconds = 0

            if seconds <= 2:
                print('[agy] print timeout after 5m0s with turn in progress; returning partial output', flush=True)
                raise SystemExit(0)

            (root / "solver" / "solve.py").write_text("print('ok')\\n", encoding="utf-8")
            (root / "script" / "worker-report.json").write_text(
                '{"local_verification":"passed","summary":"print timeout exceeds manager timeout"}',
                encoding="utf-8",
            )
        """), encoding="utf-8")

        result = SolverService(workspace, timeout_seconds=2, stale_seconds=1).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "completed"


def test_agy_internal_print_timeout_is_classified_as_timeout_not_verify_failure():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Forensics", "agy-timeout", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text(
            "print('[agy] print timeout after 5m0s with turn in progress; returning partial output', flush=True)\n",
            encoding="utf-8",
        )

        result = SolverService(workspace, timeout_seconds=2, stale_seconds=1).run(
            "1", agy_command=[sys.executable, str(worker)]
        )[0]

        assert result["state"] == "failed"
        assert result["error_code"] == "E_TIMEOUT"


def test_finish_worker_detects_filter_from_log_even_if_not_in_state():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Pwn", "log-refusal", 1, source=True)
        (root / "solver" / "solve.py").write_text("print('ok')\n", encoding="utf-8")
        (root / "script" / "agy.log").write_text(
            "early log line\nThis request was blocked by Gemini's filters.\nsome trailing stuff\n",
            encoding="utf-8",
        )
        service = SolverService(workspace)
        job = service.scan()[0]

        class _DummyProcess:
            pid = 1234
            def poll(self):
                return 0

        res = service._finish_worker(job, _DummyProcess(), timed_out=False, stalled=False)
        assert res["state"] == "filtered"
        assert res["error_code"] == "E_FILTER"


def test_run_cancels_remaining_queued_jobs_on_keyboard_interrupt(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "job-1", 1, source=True)
        make_challenge(workspace, "Web", "job-2", 2, source=True)
        service = SolverService(workspace)

        def interrupt_start(job, cmd):
            if job.display_id == 1:
                raise KeyboardInterrupt()

        monkeypatch.setattr(service, "_start_worker", interrupt_start)
        with pytest.raises(KeyboardInterrupt):
            service.run("1,2", workers=1)

        jobs = service.scan()
        state2 = service.read_job(jobs[1])
        assert state2["state"] == "cancelled"
        assert state2["phase"] == "cancelled"


def test_terminate_group_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    import signal
    from ctf_downloader.services.solver_service import SolverService

    killed_signals = []

    class _HangingProcess:
        pid = 4321
        def poll(self):
            return None
        def wait(self, timeout=None):
            if signal.SIGKILL not in killed_signals:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            return -9

    monkeypatch.setattr("os.killpg", lambda pid, sig: killed_signals.append(sig))
    SolverService._terminate_group(_HangingProcess(), timeout=0.01)

    assert signal.SIGTERM in killed_signals
    assert signal.SIGKILL in killed_signals


def test_run_cancels_active_jobs_on_keyboard_interrupt(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "job-active", 1, source=True)
        service = SolverService(workspace)

        class _MockActiveProcess:
            pid = 9876
            def poll(self):
                return None
            def wait(self, timeout=None):
                return -15

        monkeypatch.setattr(service, "_start_worker", lambda job, cmd: _MockActiveProcess())
        # Simulate KeyboardInterrupt on refresh while active
        monkeypatch.setattr(time, "sleep", lambda sec: (_ for _ in ()).throw(KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            service.run("1", workers=1)

        job = service.scan()[0]
        state = service.read_job(job)
        assert state["state"] == "cancelled"
        assert state["phase"] == "cancelled"
        assert "interrupt" in state.get("message", "")


def test_recover_stale_jobs_does_not_crash_starting_job_within_grace_period():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "starting-job", 1, source=True)
        service = SolverService(workspace)
        job = service.scan()[0]

        # Job is starting right now without PID
        service._write_job(job, state="starting", phase="starting", started_at=service._now(), pid=None)
        recovered = service.recover_stale_jobs()
        assert recovered == 0
        assert service.read_job(job)["state"] == "starting"

        # Stale starting job from 2 minutes ago
        service._write_job(job, state="starting", phase="starting", started_at="2020-01-01T00:00:00Z", pid=None)
        recovered = service.recover_stale_jobs()
        assert recovered == 1
        assert service.read_job(job)["state"] == "failed"
        assert service.read_job(job)["error_code"] == "E_WORKER_CRASH"


def test_filter_refusal_on_decompilation_and_exploit():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Pwn", "refusal-decompile", 1, source=True)
        worker = workspace / "fake_agy.py"
        worker.write_text(
            "print('I cannot decompile or disassemble compiled binaries to search for exploitable vulnerabilities.')\n",
            encoding="utf-8",
        )

        result = SolverService(workspace).run("1", agy_command=[sys.executable, str(worker)])[0]
        assert result["state"] == "filtered"
        assert result["error_code"] == "E_FILTER"


def test_flag_extraction_prefers_flag_txt_and_ignores_dummies():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Reverse", "custom-vm", 1, source=True)
        (root / "solver" / "solve.py").write_text("print('ok')\n", encoding="utf-8")
        # Real flag in flag.txt
        (root / "flag.txt").write_text("ASIS{M1ddL3_3nd14n_N1bbL35_M4k3_Q3MU_D122y!}\n", encoding="utf-8")
        # Log has a dummy probe flag from early testing
        (root / "script" / "agy.log").write_text(
            "early test with ASIS{test}\nand also ASIS{dummy}\n",
            encoding="utf-8",
        )
        service = SolverService(workspace)
        job = service.scan()[0]

        class _DummyProcess:
            pid = 1234
            def poll(self):
                return 0

        res = service._finish_worker(job, _DummyProcess(), timed_out=False, stalled=False)
        assert res["state"] == "completed"
        assert res["candidate_flag"] == "ASIS{M1ddL3_3nd14n_N1bbL35_M4k3_Q3MU_D122y!}"

        # Metadata should have been updated and hoarded
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        assert meta["status"]["flag"]["value"] == "ASIS{M1ddL3_3nd14n_N1bbL35_M4k3_Q3MU_D122y!}"
        assert meta["status"]["flag"]["state"] == "hoarded"


def test_quota_error_is_classified_as_e_quota_and_preserves_analysis():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        root = make_challenge(workspace, "Hardware", "sstv", 1, source=True)
        (root / "script" / "analysis.md").write_text("# SSTV Demodulation Notes\nAll 4 channels synced.\n", encoding="utf-8")
        (root / "script" / "agy.log").write_text(
            "demodulating... error: Individual quota reached. Please upgrade your subscription.\n",
            encoding="utf-8",
        )
        service = SolverService(workspace)
        job = service.scan()[0]

        class _CrashedProcess:
            pid = 1234
            def poll(self):
                return 1

        res = service._finish_worker(job, _CrashedProcess(), timed_out=False, stalled=False)
        assert res["state"] == "failed"
        assert res["error_code"] == "E_QUOTA"
        assert res["outcome"] == "analyzed"
        assert "quota" in res["message"].lower()


def test_category_prompt_builder_all_categories():
    from ctf_downloader.services.prompt_builder import CategoryPromptBuilder

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Reverse", "rev-vm", 1, source=True)
        make_challenge(workspace, "Hardware", "radio-sig", 2, source=True)
        make_challenge(workspace, "Web", "sqli-web", 3, source=True)
        make_challenge(workspace, "Forensics", "pcap-dump", 4, source=True)
        make_challenge(workspace, "Misc", "trivia", 5, source=True)

        service = SolverService(workspace)
        jobs = {job.name: job for job in service.scan()}

        rev = service.build_prompt(jobs["rev-vm"])
        assert "Reverse Engineering & Algorithm Recovery" in rev
        assert "Deconstruct custom VM opcodes" in rev

        hw = service.build_prompt(jobs["radio-sig"])
        assert "Hardware & Signal Analysis" in hw
        assert "demodulation pipeline" in hw

        web = service.build_prompt(jobs["sqli-web"])
        assert "Web Source Code & Configuration Audit" in web
        assert "Audit backend source" in web

        forensics = service.build_prompt(jobs["pcap-dump"])
        assert "Forensic Artifact & Data Analysis" in forensics
        assert "file headers, metadata" in forensics

        misc = service.build_prompt(jobs["trivia"])
        assert "Challenge: trivia" in misc
        assert "Inspect metadata.json" in misc


def test_category_sessions_lifecycle():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        service = SolverService(workspace)

        assert service.get_category_session("Crypto") is None
        assert service.get_category_sessions() == {}

        service.save_category_session("Crypto", "conv-crypto-001")
        assert service.get_category_session("Crypto") == "conv-crypto-001"
        # Case-insensitive lookup
        assert service.get_category_session("crypto") == "conv-crypto-001"
        assert service.get_category_session("CRYPTO") == "conv-crypto-001"

        service.save_category_session("Web", "conv-web-002")
        assert service.get_category_session("Web") == "conv-web-002"

        # Clear single category
        service.clear_category_sessions("crypto")
        assert service.get_category_session("Crypto") is None
        assert service.get_category_session("Web") == "conv-web-002"

        # Clear all
        service.clear_category_sessions()
        assert service.get_category_session("Web") is None
        assert service.get_category_sessions() == {}


def test_solver_worker_reuses_category_session_by_default(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        chal1 = make_challenge(workspace, "Crypto", "c1", 1, source=True)
        chal2 = make_challenge(workspace, "Crypto", "c2", 2, source=True)
        service = SolverService(workspace)

        launched_commands = []

        class _MockProc:
            pid = 88888
            returncode = 0
            def poll(self):
                return 0

        def mock_popen(cmd, **kwargs):
            launched_commands.append(list(cmd))
            return _MockProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        # 1. Pre-seed a session for Crypto
        service.save_category_session("Crypto", "conv-crypto-prior-999")

        # 2. Run challenge 1 with default (reuse_session=True)
        service.run("1")
        assert len(launched_commands) == 1
        cmd1 = launched_commands[0]
        assert "--conversation" in cmd1
        conv_idx = cmd1.index("--conversation")
        assert cmd1[conv_idx + 1] == "conv-crypto-prior-999"

        # 3. Run challenge 2 with reuse_session=False (--new-session)
        service.run("2", reuse_session=False)
        assert len(launched_commands) == 2
        cmd2 = launched_commands[1]
        assert "--conversation" not in cmd2


def test_solver_worker_resumes_filtered_job_with_recovery_prompt(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        chal = make_challenge(workspace, "Pwn", "heap-overflow", 1, source=True)
        service = SolverService(workspace)

        launched_commands = []

        class _MockProc:
            pid = 77777
            returncode = 0
            def poll(self):
                return 0

        def mock_popen(cmd, **kwargs):
            launched_commands.append(list(cmd))
            return _MockProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        job = service.scan()[0]
        # Simulate that this job was previously run and stopped by a filter
        service._write_job(job, state="filtered", filter_detected=True, conversation_id="conv-filtered-child-888")

        # Run again
        service.run("1")
        assert len(launched_commands) == 1
        cmd = launched_commands[0]
        assert "--conversation" in cmd
        conv_idx = cmd.index("--conversation")
        assert cmd[conv_idx + 1] == "conv-filtered-child-888"

        # Check prompt injected recovery instructions
        print_idx = cmd.index("--print")
        prompt_text = cmd[print_idx + 1]
        assert "[RESUMING CHALLENGE ANALYSIS · heap-overflow]" in prompt_text
        assert "Automatic state recovery:" in prompt_text
        assert "ctf ask --workspace math_workspace" in prompt_text


def test_multi_worker_category_slots_are_independent(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "crypto-1", 1, source=True)
        make_challenge(workspace, "Crypto", "crypto-2", 2, source=True)
        make_challenge(workspace, "Web", "web-1", 3, source=True)

        service = SolverService(workspace)
        active_sim = {}
        execution_order = []

        class _ControlledProc:
            def __init__(self, job_id):
                self.pid = 90000 + job_id
                self.job_id = job_id
                self._poll_count = 0
                self.returncode = 0

            def poll(self):
                self._poll_count += 1
                # crypto-1 and web-1 finish on poll #2
                # crypto-2 starts after crypto-1 finishes
                if self._poll_count >= 2:
                    return 0
                return None

        def mock_start(job, cmd, reuse_session=True):
            execution_order.append(job.name)
            return _ControlledProc(job.display_id)

        monkeypatch.setattr(service, "_start_worker", mock_start)

        # Run with 2 workers: selected challenges occupy slots independently.
        service.run("1,2,3", workers=2)

        # crypto-1 and crypto-2 start together; web-1 enters after a slot frees.
        assert execution_order[0] == "crypto-1"
        assert execution_order[1] == "crypto-2"
        assert execution_order[2] == "web-1"


def test_multi_worker_starts_same_category_jobs_concurrently(monkeypatch):
    """Multiple selected challenges must occupy worker slots independently."""
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "crypto-1", 1, source=True)
        make_challenge(workspace, "Crypto", "crypto-2", 2, source=True)
        service = SolverService(workspace)
        started = []

        class _ControlledProc:
            def __init__(self, job_id):
                self.pid = 91000 + job_id
                self.returncode = 0
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                return None if self.poll_count == 1 else 0

        def mock_start(job, cmd, **kwargs):
            started.append((job.name, kwargs.get("fork_session", False)))
            return _ControlledProc(job.display_id)

        monkeypatch.setattr(service, "_start_worker", mock_start)

        service.run("1,2", workers=2)

        assert started == [("crypto-1", False), ("crypto-2", True)]
        assert service.max_active_workers == 2


def test_per_category_pool_starts_one_worker_for_each_category(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Crypto", "crypto-1", 1, source=True)
        make_challenge(workspace, "Crypto", "crypto-2", 2, source=True)
        make_challenge(workspace, "Forensics", "forensics-1", 3, source=True)
        make_challenge(workspace, "Web", "web-1", 4, source=True)
        service = SolverService(workspace)
        started = []

        class _ControlledProc:
            def __init__(self, job_id):
                self.pid = 92000 + job_id
                self.returncode = 0
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                return None if self.poll_count == 1 else 0

        def mock_start(job, cmd, **kwargs):
            started.append(job.name)
            return _ControlledProc(job.display_id)

        monkeypatch.setattr(service, "_start_worker", mock_start)

        service.run("1,2,3,4", workers=3, per_category=True)

        assert started[:3] == ["crypto-1", "forensics-1", "web-1"]
        assert started[3:] == ["crypto-2"]
        assert service.max_active_workers == 3


def test_solver_worker_forks_session_when_requested(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chal = make_challenge(workspace, "Crypto", "c1", 1, source=True)
    service = SolverService(workspace)
    service.save_category_session("Crypto", "conv-master-crypto-123")

    job = service.scan()[0]
    launched_cmd = []

    class _MockProc:
        pid = 11111
        returncode = 0
        def poll(self):
            return 0

    def mock_popen(cmd, **kwargs):
        launched_cmd.extend(list(cmd))
        return _MockProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    # 1. Fork session succeeds
    with patch("ctf_downloader.services.session_forker.fork_agy_session", return_value="conv-forked-crypto-456"):
        service._start_worker(job, ["agy"], reuse_session=True, fork_session=True)
        assert "--conversation" in launched_cmd
        conv_idx = launched_cmd.index("--conversation")
        assert launched_cmd[conv_idx + 1] == "conv-forked-crypto-456"
        state = service.read_job(job)
        assert state["forked_session"] is True
        assert state["reused_session"] is False

    # 2. Fork session fails -> fail-closed to fresh session (do not clobber master)
    launched_cmd.clear()
    with patch("ctf_downloader.services.session_forker.fork_agy_session", return_value=None):
        service._start_worker(job, ["agy"], reuse_session=True, fork_session=True)
        assert "--conversation" not in launched_cmd
        state = service.read_job(job)
        assert state["conversation_id"] is None
        assert state["forked_session"] is False
        assert state["reused_session"] is False
        # Verify master session was NOT corrupted or changed
        assert service.get_category_session("Crypto") == "conv-master-crypto-123"
