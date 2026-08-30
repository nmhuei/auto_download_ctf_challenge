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

def test_handle_bridge_status(capsys):
    args = argparse.Namespace(bridge_action="status")
    handle_bridge(args)
    captured = capsys.readouterr()
    assert "BRIDGE" in captured.out or "Bridge" in captured.out or "bridge" in captured.out

def test_handle_bridge_token(capsys):
    args = argparse.Namespace(bridge_action="token")
    handle_bridge(args)
    captured = capsys.readouterr()
    daemon = BridgeDaemon()
    token = daemon.get_or_create_token()
    assert token in captured.out
