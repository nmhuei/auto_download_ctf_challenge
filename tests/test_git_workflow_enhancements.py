import subprocess
from pathlib import Path
import pytest

from ctf_downloader.services.git_workflow import (
    GitWorkflowError,
    GitWorkflowService,
)


@pytest.fixture
def git_env(tmp_path):
    repo = tmp_path / "ctf-repo"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = GitWorkflowService.initialize_repository(
        repo,
        remote_url=str(remote),
        base_branch="main",
        remote="origin",
        push=True,
    )
    return repo, remote, info


def test_scan_large_files(tmp_path):
    ws = tmp_path / "test_ws"
    ws.mkdir()
    small_file = ws / "solve.py"
    small_file.write_text("print('flag')")

    # Create a 60MB file
    large_file = ws / "dump.raw"
    with open(large_file, "wb") as f:
        f.seek(60 * 1024 * 1024 - 1)
        f.write(b"\0")

    large_files = GitWorkflowService.scan_large_files(ws, threshold_mb=50)
    assert len(large_files) == 1
    path, size_bytes = large_files[0]
    assert path.name == "dump.raw"
    assert size_bytes >= 50 * 1024 * 1024


def test_scoped_clean_does_not_block_on_unrelated_workspace(git_env):
    repo, _remote, _info = git_env
    ws1 = repo / "CTF_A"
    ws2 = repo / "CTF_B"
    ws1.mkdir()
    ws2.mkdir()

    # Commit initial state on main
    (ws1 / "a.txt").write_text("a")
    (ws2 / "b.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True)

    # Now dirty CTF_B
    (ws2 / "dirty.txt").write_text("dirty content")

    # Modifying CTF_A
    (ws1 / "a.txt").write_text("a modified")

    # checkpoint_and_push on ws1 with scoped_only=True should succeed without failing on ws2
    res = GitWorkflowService.checkpoint_and_push(
        ws1,
        message="update CTF_A",
        push=False,
        scoped_only=True,
    )
    assert res["committed"] is True
    # Verify dirty.txt in ws2 is STILL untracked and was NOT committed
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "dirty.txt" in status


def test_safe_sync_pull_rebase(git_env):
    repo, remote, _info = git_env
    ws = repo / "CTF_A"
    ws.mkdir()
    (ws / "solve.py").write_text("print('v1')")

    # Checkpoint and push to origin
    GitWorkflowService.checkpoint_and_push(ws, message="v1", push=True)

    # Simulate another clone pushing a commit to remote
    other_clone = repo.parent / "other-clone"
    subprocess.run(["git", "clone", str(remote), str(other_clone)], check=True)
    other_ws = other_clone / "CTF_A"
    (other_ws / "remote_notes.md").write_text("from team")
    subprocess.run(["git", "add", "-A"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "commit", "-m", "team commit"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(other_clone), check=True)

    # Now local repo has a new commit
    (ws / "local_notes.md").write_text("from local")
    GitWorkflowService.checkpoint_and_push(ws, message="local commit", push=False)

    # safe_sync should rebase local on top of remote and push
    sync_res = GitWorkflowService.safe_sync(ws, remote="origin")
    assert sync_res["success"] is True
    assert (ws / "remote_notes.md").exists()
    assert (ws / "local_notes.md").exists()
