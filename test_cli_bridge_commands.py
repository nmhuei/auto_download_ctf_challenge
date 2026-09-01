import pytest
from ctf_downloader.services.health_service import HealthService
from ctf_downloader.bridge.daemon import BridgeDaemon
from ctf_downloader.cli_commands import handle_bridge
import argparse

def test_health_service_check_bridge():
    bridge_info = HealthService.check_bridge_health()
    assert "bridge_running" in bridge_info
    assert "port" in bridge_info
    assert "token_exists" in bridge_info
    assert "extension_connected" in bridge_info
    assert "state" in bridge_info
    assert "error" in bridge_info

def test_health_service_reports_missing_bridge_runtime(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "ctf_downloader.bridge.transport", None)
    info = HealthService.check_bridge_health()
    assert info["state"] == "runtime-unavailable"
    assert "import failed" in (info["error"] or "")


def test_handle_bridge_status(capsys):
    args = argparse.Namespace(bridge_action="status")
    handle_bridge(args)
    captured = capsys.readouterr()
    assert "BRIDGE" in captured.out or "Bridge" in captured.out or "bridge" in captured.out

def test_handle_bridge_status_distinguishes_daemon_only(capsys, monkeypatch):
    monkeypatch.setattr(
        HealthService,
        "check_bridge_health",
        classmethod(lambda cls: {
            "bridge_running": True,
            "extension_connected": False,
            "state": "daemon-only",
            "error": None,
            "token_exists": True,
            "pid": 123,
            "host": "127.0.0.1",
            "port": 18888,
        }),
    )
    args = argparse.Namespace(bridge_action="status")
    handle_bridge(args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "DAEMON ONLY" in combined
    assert "Extension" in combined
    assert "not connected" in combined
    assert "mở browser" in combined


def test_health_service_reports_port_conflict(monkeypatch):
    monkeypatch.setattr(
        BridgeDaemon,
        "inspect_status",
        lambda self: {
            "pid_running": False,
            "port_open": True,
            "owned": False,
            "port_conflict": True,
            "pid": None,
            "host": self.host,
            "port": self.port,
        },
    )
    info = HealthService.check_bridge_health()
    assert info["state"] == "port-conflict"
    assert info["port_conflict"] is True
    assert info["port_open"] is True
    assert "không thuộc PID Bridge" in (info["error"] or "")


def test_handle_bridge_status_port_conflict_has_specific_action(capsys, monkeypatch):
    monkeypatch.setattr(
        HealthService,
        "check_bridge_health",
        classmethod(lambda cls: {
            "bridge_running": False,
            "pid_running": False,
            "port_open": True,
            "port_conflict": True,
            "extension_connected": False,
            "state": "port-conflict",
            "error": "foreign listener",
            "token_exists": True,
            "pid": None,
            "host": "127.0.0.1",
            "port": 18888,
        }),
    )
    args = argparse.Namespace(bridge_action="status")
    handle_bridge(args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "PORT CONFLICT" in combined
    assert "foreign listener" in combined
    assert "dừng process chiếm port" in combined


def test_handle_bridge_start_rejects_port_conflict_before_token(capsys, monkeypatch):
    monkeypatch.setattr(
        BridgeDaemon,
        "inspect_status",
        lambda self: {
            "pid_running": False,
            "port_open": True,
            "owned": False,
            "port_conflict": True,
            "pid": None,
            "host": self.host,
            "port": self.port,
        },
    )
    token_call = []
    monkeypatch.setattr(
        BridgeDaemon,
        "get_or_create_token",
        lambda self: token_call.append(True) or "should-not-run",
    )
    args = argparse.Namespace(bridge_action="start")
    with pytest.raises(SystemExit) as exc:
        handle_bridge(args)
    assert exc.value.code == 1
    assert token_call == []
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "port" in combined.lower()


def test_handle_bridge_status_runtime_error_has_install_action(capsys, monkeypatch):
    monkeypatch.setattr(
        HealthService,
        "check_bridge_health",
        classmethod(lambda cls: {
            "bridge_running": False,
            "extension_connected": False,
            "state": "runtime-unavailable",
            "error": "ModuleNotFoundError: websockets",
            "token_exists": False,
            "pid": None,
            "host": "127.0.0.1",
            "port": 18888,
        }),
    )
    args = argparse.Namespace(bridge_action="status")
    handle_bridge(args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "RUNTIME ERROR" in combined
    assert "pip install -r requirements.txt" in combined


def test_handle_bridge_token(capsys):
    args = argparse.Namespace(bridge_action="token")
    handle_bridge(args)
    captured = capsys.readouterr()
    daemon = BridgeDaemon()
    token = daemon.get_or_create_token()
    assert token in captured.out
