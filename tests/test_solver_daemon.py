import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ctf_downloader.services.solver_service import (
    SolverService,
    get_pid_start_ticks,
    get_system_boot_id,
)
from ctf_downloader.services.solver_daemon import run_daemon


def make_challenge(workspace: Path, category: str, name: str, display_id: int, source: bool = True) -> Path:
    folder = workspace / category / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "challenge").mkdir(parents=True, exist_ok=True)
    if source:
        (folder / "challenge" / "app.py").write_text("print('flag')", encoding="utf-8")
    meta = {
        "id": display_id,
        "name": name,
        "category": category,
        "connection_info": "",
    }
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def test_pid_start_ticks_and_boot_id():
    pid = os.getpid()
    ticks = get_pid_start_ticks(pid)
    assert ticks is not None
    assert ticks > 0
    # Non-existent PID
    assert get_pid_start_ticks(99999999) is None

    boot_id = get_system_boot_id()
    # On Linux systems, boot_id is a valid UUID or empty string
    assert isinstance(boot_id, str)


def test_pid_is_alive_with_ticks_and_boot_id():
    pid = os.getpid()
    ticks = get_pid_start_ticks(pid)
    boot_id = get_system_boot_id()

    # Self should be alive with correct ticks and boot_id
    assert SolverService._pid_is_alive(pid, start_ticks=ticks, boot_id=boot_id) is True

    # If ticks mismatch (simulating PID recycling), it should report False
    assert SolverService._pid_is_alive(pid, start_ticks=ticks + 1000, boot_id=boot_id) is False

    # If boot_id mismatches (simulating reboot), it should report False
    assert SolverService._pid_is_alive(pid, start_ticks=ticks, boot_id="wrong-boot-id") is False


def test_get_daemon_status_idle_and_running():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        service = SolverService(workspace)

        # When no manager-state.json exists
        status = service.get_daemon_status()
        assert status["is_running"] is False
        assert status["status"] == "idle"

        # When state exists with current process as daemon
        state_path = workspace / ".ctf-solver" / "manager-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "daemon_pid": os.getpid(),
            "pid_start_ticks": get_pid_start_ticks(os.getpid()),
            "boot_id": get_system_boot_id(),
            "status": "running",
            "target_ids": "1",
            "active_ids": ["1"],
        }), encoding="utf-8")

        status = service.get_daemon_status()
        assert status["is_running"] is True
        assert status["status"] == "running"
        assert status["daemon_pid"] == os.getpid()


def test_spawn_background_and_already_running(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "test-bg", 1, source=True)
        service = SolverService(workspace)

        class _MockDaemonProc:
            pid = 55555
            def poll(self):
                return None

        spawned_cmds = []

        def mock_popen(cmd, **kwargs):
            spawned_cmds.append(cmd)
            # Create the manager state so daemon appears running
            state_path = workspace / ".ctf-solver" / "manager-state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "daemon_pid": os.getpid(),
                "pid_start_ticks": get_pid_start_ticks(os.getpid()),
                "boot_id": get_system_boot_id(),
                "status": "running",
                "target_ids": "1",
                "active_ids": ["1"],
            }), encoding="utf-8")
            return _MockDaemonProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        res = service.spawn_background("1")
        assert res["success"] is True
        assert "SuperBQA daemon" in res["message"]
        assert len(spawned_cmds) == 1
        assert "--ids" in spawned_cmds[0]
        assert "1" in spawned_cmds[0]

        # Second spawn attempt while daemon is running should be rejected
        res2 = service.spawn_background("1")
        assert res2["success"] is False
        assert res2["error"] == "ALREADY_RUNNING"


def test_stop_background_cancels_workers_and_daemon(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        chal = make_challenge(workspace, "Crypto", "bg-chal", 2, source=True)
        service = SolverService(workspace)

        # Write running state for challenge
        script_dir = chal / "script"
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "worker-state.json").write_text(json.dumps({
            "state": "running",
            "phase": "recon",
            "pid": 66666,
            "pid_start_ticks": 100,
            "boot_id": get_system_boot_id(),
        }), encoding="utf-8")

        # Fake _pid_is_alive for 66666
        monkeypatch.setattr(SolverService, "_pid_is_alive", lambda *args, **kwargs: True)
        killed_signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_signals.append((pid, sig)))
        monkeypatch.setattr(os, "killpg", lambda pid, sig: killed_signals.append((pid, sig)))

        res = service.stop_background("2")
        assert res["success"] is True
        assert res["stopped_workers"] == 1

        state = json.loads((script_dir / "worker-state.json").read_text(encoding="utf-8"))
        assert state["state"] == "cancelled"


def test_run_daemon_end_to_end_no_double_lock(monkeypatch):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "daemon-chal", 1, source=True)

        class _MockProc:
            pid = 77777
            returncode = 0
            def poll(self):
                return 0

        monkeypatch.setattr(
            SolverService,
            "_start_worker",
            lambda self, job, cmd: _MockProc()
        )

        exit_code = run_daemon(workspace, "1", workers=1, timeout=10, stale_timeout=10)
        assert exit_code == 0

        state_path = workspace / ".ctf-solver" / "manager-state.json"
        assert state_path.is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["status"] == "idle"
        assert state["error"] is None

        log_path = workspace / ".ctf-solver" / "supervisor.log"
        assert log_path.is_file()
        log_content = log_path.read_text(encoding="utf-8")
        assert "Daemon completed work" in log_content
        assert "Daemon error/signal" not in log_content


def test_daemon_blocks_concurrent_solver(monkeypatch):
    from ctf_downloader.services.solver_service import SolverAlreadyRunning

    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        make_challenge(workspace, "Web", "daemon-chal-2", 1, source=True)

        service = SolverService(workspace)
        with service._manager_lock():
            # While manager.lock is held, run_daemon should exit with code 2
            exit_code = run_daemon(workspace, "1", workers=1, timeout=10, stale_timeout=10)
            assert exit_code == 2

            # Also foreground service.run should raise SolverAlreadyRunning
            with pytest.raises(SolverAlreadyRunning):
                service.run("1")


