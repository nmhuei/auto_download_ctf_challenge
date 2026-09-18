import json
import tempfile
from pathlib import Path

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.cli_commands import handle_status


def make_workspace() -> Path:
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    # Keep the TemporaryDirectory alive through the test by attaching it.
    root._temporary_directory = temp  # type: ignore[attr-defined]
    challenge = root / "Web" / "portal"
    (challenge / "challenge").mkdir(parents=True)
    (challenge / "script").mkdir()
    (challenge / "solver").mkdir()
    (challenge / "challenge" / "app.py").write_text("print(1)", encoding="utf-8")
    (challenge / "metadata.json").write_text(json.dumps({
        "id": 9, "name": "portal", "category": "Web", "connection_info": "",
    }), encoding="utf-8")
    return root


def test_solve_parser_defaults_to_three_workers_and_accepts_ids():
    args = build_unified_parser().parse_args(["solve", "--ids", "1,2", "--workers", "3"])

    assert args.subcommand == "solve"
    assert args.ids == "1,2"
    assert args.workers == 3


def test_status_solver_parser_accepts_detail_target_and_watch():
    args = build_unified_parser().parse_args(["status", "2", "--solver", "--watch"])

    assert args.target == "2"
    assert args.solver is True
    assert args.watch is True


def test_status_solver_renders_worker_state(capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        challenge = root / "Web" / "portal"
        (challenge / "challenge").mkdir(parents=True)
        (challenge / "script").mkdir()
        (challenge / "solver").mkdir()
        (challenge / "metadata.json").write_text(json.dumps({
            "id": 9, "name": "portal", "category": "Web", "connection_info": "",
        }), encoding="utf-8")
        (challenge / "script" / "worker-state.json").write_text(json.dumps({
            "state": "running", "phase": "analysis", "last_output": "reading source",
        }), encoding="utf-8")

        handle_status(type("Args", (), {
            "workspace": str(root), "category": None, "unsolved": False, "solved": False,
            "container": False, "labels": None, "search": None, "solver": True,
            "watch": False, "target": None,
        })())

        assert "portal" in capsys.readouterr().out


def test_solve_parser_exposes_stale_timeout():
    args = build_unified_parser().parse_args(["solve", "--stale-timeout", "45"])

    assert args.stale_timeout == 45


def test_running_label_animates_only_when_requested(monkeypatch):
    import ctf_downloader.cli_commands as commands

    assert commands._solver_running_label(animate=False) == "◌ running"
    monkeypatch.setattr(commands.time, "monotonic", lambda: 0.0)
    first = commands._solver_running_label(animate=True)
    monkeypatch.setattr(commands.time, "monotonic", lambda: 0.2)
    second = commands._solver_running_label(animate=True)

    assert first.endswith(" running")
    assert second.endswith(" running")
    assert first != second


def test_status_solver_recovers_dead_worker_before_rendering(capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        challenge = root / "Web" / "crashed"
        (challenge / "challenge").mkdir(parents=True)
        (challenge / "script").mkdir()
        (challenge / "solver").mkdir()
        (challenge / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "crashed", "category": "Web", "connection_info": "",
        }), encoding="utf-8")
        (challenge / "script" / "worker-state.json").write_text(json.dumps({
            "state": "running", "phase": "analysis", "pid": 99999999,
        }), encoding="utf-8")

        handle_status(type("Args", (), {
            "workspace": str(root), "category": None, "unsolved": False, "solved": False,
            "container": False, "labels": None, "search": None, "solver": True,
            "watch": False, "target": None,
        })())

        out = capsys.readouterr().out
        assert "E_WORKER_CRASH" in out
        state = json.loads((challenge / "script" / "worker-state.json").read_text(encoding="utf-8"))
        assert state["state"] == "failed"
        assert state["error_code"] == "E_WORKER_CRASH"


def test_handle_solve_exits_nonzero_on_selection_error(monkeypatch):
    import pytest
    from ctf_downloader.cli_commands import handle_solve

    with tempfile.TemporaryDirectory() as temp:
        args = type("Args", (), {
            "workspace": temp, "ids": "999", "workers": 1, "timeout": 10, "stale_timeout": 10,
        })()
        with pytest.raises(SystemExit) as exc:
            handle_solve(args)
        assert exc.value.code == 1


def test_handle_solve_exits_nonzero_on_non_interactive_without_ids(monkeypatch):
    import sys
    import pytest
    from ctf_downloader.cli_commands import handle_solve

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with tempfile.TemporaryDirectory() as temp:
        args = type("Args", (), {
            "workspace": temp, "ids": None, "workers": 1, "timeout": 10, "stale_timeout": 10,
        })()
        with pytest.raises(SystemExit) as exc:
            handle_solve(args)
        assert exc.value.code == 1


def test_status_solver_target_watch_handles_keyboard_interrupt(monkeypatch):
    import ctf_downloader.cli_commands as commands
    from ctf_downloader.services.solver_service import SolverService

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        challenge = root / "Web" / "portal"
        (challenge / "challenge").mkdir(parents=True)
        (challenge / "script").mkdir()
        (challenge / "solver").mkdir()
        (challenge / "metadata.json").write_text(json.dumps({
            "id": 9, "name": "portal", "category": "Web", "connection_info": "",
        }), encoding="utf-8")

        service = SolverService(root)
        slept = []

        def fake_sleep(duration):
            slept.append(duration)
            raise KeyboardInterrupt()

        monkeypatch.setattr(commands.time, "sleep", fake_sleep)

        # Calling with target="1" and watch=True should enter live loop, sleep, catch KeyboardInterrupt and return cleanly
        commands._render_solver_status(service, target="1", watch=True)
        assert len(slept) == 1


def test_handle_solve_detach_spawns_background(monkeypatch, capsys):
    from ctf_downloader.cli_commands import handle_solve
    from ctf_downloader.services.solver_service import SolverService

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        chal = root / "Web" / "target"
        (chal / "challenge").mkdir(parents=True)
        (chal / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "target", "category": "Web", "connection_info": "",
        }), encoding="utf-8")

        monkeypatch.setattr(
            SolverService,
            "spawn_background",
            lambda self, ids, **kwargs: {"success": True, "message": "Worker started (PID 1234)"}
        )

        args = type("Args", (), {
            "workspace": str(root), "ids": "1", "detach": True, "status": None,
            "stop": None, "logs": None, "attach": False, "workers": 3,
            "timeout": 3600, "stale_timeout": 300,
        })()

        handle_solve(args)
        out = capsys.readouterr().out
        assert "Worker started (PID 1234)" in out
        assert "ctf solve --status" in out


def test_handle_solve_stop_and_logs(monkeypatch, capsys):
    from ctf_downloader.cli_commands import handle_solve
    from ctf_downloader.services.solver_service import SolverService

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        chal = root / "Web" / "target"
        (chal / "challenge").mkdir(parents=True)
        (chal / "script").mkdir(parents=True)
        (chal / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "target", "category": "Web", "connection_info": "",
        }), encoding="utf-8")
        log_file = chal / "script" / "agy.log"
        log_file.write_text("Line 1\nLine 2\nLine 3 [tool] view_file\n", encoding="utf-8")

        # Test --stop
        monkeypatch.setattr(
            SolverService,
            "stop_background",
            lambda self, tid: {"success": True, "message": "Stopped worker."}
        )
        args_stop = type("Args", (), {
            "workspace": str(root), "ids": None, "detach": False, "status": None,
            "stop": "1", "logs": None, "attach": False, "workers": 3,
            "timeout": 3600, "stale_timeout": 300,
        })()
        handle_solve(args_stop)
        out = capsys.readouterr().out
        assert "Stopped worker." in out

        # Test --logs
        args_logs = type("Args", (), {
            "workspace": str(root), "ids": None, "detach": False, "status": None,
            "stop": None, "logs": "1", "attach": False, "workers": 3,
            "timeout": 3600, "stale_timeout": 300,
        })()
        handle_solve(args_logs)
        out_logs = capsys.readouterr().out
        assert "Line 3 [tool] view_file" in out_logs


def test_solver_table_displays_flag_in_phase_column():
    from ctf_downloader.cli_commands import _solver_table, _get_challenge_flag
    from ctf_downloader.services.solver_service import SolverService
    from rich.console import Console

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        # 1. Challenge with flag in worker-state.json
        chal1 = root / "Baby" / "mic_check"
        (chal1 / "challenge").mkdir(parents=True)
        (chal1 / "script").mkdir(parents=True)
        (chal1 / "solver").mkdir(parents=True)
        (chal1 / "solver" / "solve.py").write_text("print('solve')", encoding="utf-8")
        (chal1 / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "Mic Check", "category": "Baby 👶", "connection_info": "",
        }), encoding="utf-8")
        (chal1 / "script" / "worker-state.json").write_text(json.dumps({
            "state": "completed", "phase": "completed", "candidate_flag": "CTF{baby_mic_check_flag}",
        }), encoding="utf-8")

        # 2. Challenge with flag in flag.txt
        chal2 = root / "Reverse" / "asis_arch"
        (chal2 / "challenge").mkdir(parents=True)
        (chal2 / "script").mkdir(parents=True)
        (chal2 / "metadata.json").write_text(json.dumps({
            "id": 2, "name": "ASIS Arch", "category": "Reverse", "connection_info": "",
        }), encoding="utf-8")
        (chal2 / "script" / "worker-state.json").write_text(json.dumps({
            "state": "completed", "phase": "completed",
        }), encoding="utf-8")
        (chal2 / "flag.txt").write_text("ASIS{arch_rev_ok}\n", encoding="utf-8")

        # 3. Challenge completed without flag
        chal3 = root / "Web" / "web2048"
        (chal3 / "challenge").mkdir(parents=True)
        (chal3 / "script").mkdir(parents=True)
        (chal3 / "metadata.json").write_text(json.dumps({
            "id": 3, "name": "2048", "category": "Web", "connection_info": "",
        }), encoding="utf-8")
        (chal3 / "script" / "worker-state.json").write_text(json.dumps({
            "state": "completed", "phase": "completed",
        }), encoding="utf-8")

        service = SolverService(root)
        table = _solver_table(service, animate=False)

        console = Console(record=True, width=150)
        console.print(table)
        rendered = console.export_text()

        # Check that flags appear in PHASE instead of generic "completed"
        assert "★ CTF{baby_mic_check_flag}" in rendered
        assert "★ ASIS{arch_rev_ok}" in rendered
        # Chal 3 should retain generic "completed"
        assert "completed" in rendered
        # The new compact status names the flag's local provenance.
        assert "★ local flag" in rendered


def test_solver_table_can_show_live_worker_count():
    from ctf_downloader.cli_commands import _solver_table
    from ctf_downloader.services.solver_service import SolverService

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for idx, state in ((1, "running"), (2, "starting"), (3, "completed")):
            chal = root / "Web" / f"challenge-{idx}"
            (chal / "challenge").mkdir(parents=True)
            (chal / "script").mkdir()
            (chal / "metadata.json").write_text(json.dumps({
                "id": idx, "name": f"Challenge {idx}", "category": "Web",
            }), encoding="utf-8")
            (chal / "script" / "worker-state.json").write_text(json.dumps({
                "state": state,
            }), encoding="utf-8")

        table = _solver_table(SolverService(root), animate=False, show_worker_count=True)

        assert table.title == "Live Radar · 2 workers running"


def test_solver_table_separates_platform_and_local_solve_states():
    """Platform completion and a hoarded local flag must not look identical."""
    from ctf_downloader.cli_commands import _solver_table
    from ctf_downloader.services.solver_service import SolverService
    from rich.console import Console

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for challenge_id, name, solved_by_me in (
            (1, "Platform Solved", True),
            (2, "Local Flag", False),
            (3, "Ready", False),
        ):
            chal = root / "Crypto" / name.casefold().replace(" ", "_")
            (chal / "challenge").mkdir(parents=True)
            (chal / "challenge" / "task.py").write_text("print(1)", encoding="utf-8")
            (chal / "script").mkdir()
            (chal / "metadata.json").write_text(json.dumps({
                "id": challenge_id,
                "name": name,
                "category": "Crypto",
                "solved_by_me": solved_by_me,
            }), encoding="utf-8")
        (root / "Crypto" / "local_flag" / "flag.txt").write_text(
            "CTF{real_hoarded_flag}", encoding="utf-8"
        )

        table = _solver_table(SolverService(root), animate=False)
        output = Console(record=True, width=160)
        output.print(table)
        rendered = output.export_text()

        assert "CTF / FLAG" in rendered
        assert "✓ platform" in rendered
        assert "★ local flag" in rendered
        assert "READY · eligible for BQA" in rendered


def test_live_worker_count_recovers_dead_pid_before_counting():
    from ctf_downloader.cli_commands import _solver_table
    from ctf_downloader.services.solver_service import SolverService

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        chal = root / "Web" / "stale"
        (chal / "challenge").mkdir(parents=True)
        (chal / "script").mkdir()
        (chal / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "Stale", "category": "Web",
        }), encoding="utf-8")
        (chal / "script" / "worker-state.json").write_text(json.dumps({
            "state": "running", "pid": 99999999,
        }), encoding="utf-8")

        service = SolverService(root)
        table = _solver_table(service, animate=False, show_worker_count=True)

        assert table.title == "Live Radar · 0 workers running"
        assert service.read_job(service.scan()[0])["error_code"] == "E_WORKER_CRASH"
