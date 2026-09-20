from pathlib import Path
from unittest.mock import patch
import pytest

from ctf_downloader.interactive_menu import CTFInteractiveConsole, _MAIN_ACTIONS_FULL
from ctf_downloader.services.git_workflow import GitWorkflowService


@pytest.fixture
def test_workspace(tmp_path):
    repo = tmp_path / "ctf-repo"
    GitWorkflowService.initialize_repository(repo, base_branch="main", push=False)
    ws = repo / "ASIS_CTF_2026"
    ws.mkdir(parents=True)
    return ws


def test_main_actions_includes_git():
    action_keys = [k for k, _ in _MAIN_ACTIONS_FULL]
    assert "5" in action_keys or "G" in action_keys


def test_menu_git_status_and_back(test_workspace):
    app = CTFInteractiveConsole(workspace_path=str(test_workspace))
    # Option '0' exits immediately
    with patch("ctf_downloader.interactive_menu._prompt", return_value="0"):
        app._menu_git()


def test_menu_git_quick_push(test_workspace):
    app = CTFInteractiveConsole(workspace_path=str(test_workspace))
    # Add a file in workspace
    (test_workspace / "solve.py").write_text("print(1)")

    # Sequence: choose 1 (Quick push), message default "", pause ""
    inputs = iter(["1", "", ""])
    with patch("ctf_downloader.interactive_menu._prompt", side_effect=lambda *args: next(inputs)):
        app._menu_git()

    st = GitWorkflowService.status(test_workspace)
    assert st["dirty_files"] == 0


def test_menu_git_large_file_detection(test_workspace):
    app = CTFInteractiveConsole(workspace_path=str(test_workspace))
    large_file = test_workspace / "big_dump.raw"
    with open(large_file, "wb") as f:
        f.seek(55 * 1024 * 1024 - 1)
        f.write(b"\0")

    # Sequence: choose 3 (Detailed Status & Large File Guard), pause ""
    inputs = iter(["3", ""])
    with patch("ctf_downloader.interactive_menu._prompt", side_effect=lambda *args: next(inputs)):
        app._menu_git()
