from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from ctf_downloader.interactive_menu import CTFInteractiveConsole
from ctf_downloader.services.git_workflow import GitWorkflowService


@pytest.fixture
def test_workspace(tmp_path):
    repo = tmp_path / "ctf-repo"
    GitWorkflowService.initialize_repository(repo, base_branch="main", push=False)
    ws = repo / "ASIS_CTF_2026"
    ws.mkdir(parents=True)
    return ws


def test_submit_flag_auto_checkpoint_on_success(test_workspace):
    app = CTFInteractiveConsole(workspace_path=str(test_workspace))
    target = {"id": 10, "name": "FakeChall", "category": "Web"}

    # Mock submit_single_flag returning (True, "Correct")
    with patch("ctf_downloader.interactive_menu._prompt", return_value="flag{fake}"):
        with patch("ctf_downloader.interactive_menu.FlagSubmitter") as mock_sub_cls:
            mock_sub = MagicMock()
            mock_sub.submit_single_flag.return_value = (True, "Correct")
            mock_sub_cls.return_value = mock_sub

            # Put a dummy file in workspace to be committed
            (test_workspace / "flag.txt").write_text("flag{fake}")

            with patch("ctf_downloader.interactive_menu._pause"):
                app._submit_flag_for_target(target)

    # Verify Git committed the solved flag
    st = GitWorkflowService.status(test_workspace)
    assert st["dirty_files"] == 0
