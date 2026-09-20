"""Tests for modern CTF Cockpit sub-hub controllers."""
from pathlib import Path
import tempfile
import json
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from ctf_downloader.ui.menu_hubs import (
    render_hub_menu,
    hub_workspace_targets,
    hub_flag_submission,
    hub_system_arsenal,
    challenge_action_card,
)


class DummyConsoleApp:
    def __init__(self, workspace_path: str, inputs: list[str] | None = None):
        self.workspace_path = workspace_path
        self.cookie = "dummy_cookie"
        self.token = "dummy_token"
        self.config = {}
        self._inputs = list(inputs or [])
        self.calls = []

    def _menu_select_workspace(self):
        self.calls.append("select_workspace")

    def _menu_download_new(self):
        self.calls.append("download_new")

    def _menu_config_credentials(self):
        self.calls.append("config_credentials")

    def _menu_submit_flag(self):
        self.calls.append("submit_flag")

    def _menu_auto_submit(self):
        self.calls.append("auto_submit")

    def _menu_git(self):
        self.calls.append("git")

    def _menu_switch_theme(self):
        self.calls.append("switch_theme")

    def _view_challenge_detail_for(self, target):
        self.calls.append(("view_detail", target.get("id")))

    def _run_container_action_for_id(self, cid, name=""):
        self.calls.append(("container_action", cid))

    def _submit_flag_for_target(self, target):
        self.calls.append(("submit_target_flag", target.get("id")))

    def _launch_solver_for_target(self, target):
        self.calls.append(("solver_target", target.get("id")))


def test_render_hub_menu(monkeypatch):
    inputs = iter(["1"])
    monkeypatch.setattr("ctf_downloader.ui.menu_hubs._prompt", lambda p: next(inputs))

    actions = [
        ("1", "Action One"),
        ("0", "Back"),
    ]
    choice = render_hub_menu(title="Test Hub", actions=actions)
    assert choice == "1"


def test_hub_workspace_targets_dispatches_and_exits(monkeypatch):
    inputs = iter(["1", "2", "3", "0"])
    monkeypatch.setattr("ctf_downloader.ui.menu_hubs._prompt", lambda p: next(inputs))

    app = DummyConsoleApp(workspace_path="/tmp/dummy")
    hub_workspace_targets(app)

    assert "select_workspace" in app.calls
    assert "download_new" in app.calls
    assert "config_credentials" in app.calls


def test_hub_flag_submission_dispatches_and_exits(monkeypatch):
    inputs = iter(["1", "2", "0"])
    monkeypatch.setattr("ctf_downloader.ui.menu_hubs._prompt", lambda p: next(inputs))

    app = DummyConsoleApp(workspace_path="/tmp/dummy")
    hub_flag_submission(app)

    assert "submit_flag" in app.calls
    assert "auto_submit" in app.calls


def test_hub_system_arsenal_dispatches_and_exits(monkeypatch):
    inputs = iter(["1", "2", "0"])
    monkeypatch.setattr("ctf_downloader.ui.menu_hubs._prompt", lambda p: next(inputs))

    app = DummyConsoleApp(workspace_path="/tmp/dummy")
    hub_system_arsenal(app)

    assert "git" in app.calls
    assert "switch_theme" in app.calls


def test_challenge_action_card_dispatches_all_actions(monkeypatch):
    inputs = iter(["1", "2", "3", "4", "0"])
    monkeypatch.setattr("ctf_downloader.ui.menu_hubs._prompt", lambda p: next(inputs))

    app = DummyConsoleApp(workspace_path="/tmp/dummy")
    target = {"id": 42, "name": "Web Portal", "category": "web"}

    # Test running actions 1, 2, 3, 4 then 0 to exit
    challenge_action_card(app, target)

    assert ("view_detail", 42) in app.calls
    assert ("container_action", 42) in app.calls
    assert ("submit_target_flag", 42) in app.calls
    assert ("solver_target", 42) in app.calls
