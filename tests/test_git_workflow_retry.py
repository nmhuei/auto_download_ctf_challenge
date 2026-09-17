"""Failure/retry invariants for destructive Git workflow transitions."""

from __future__ import annotations

import subprocess

import pytest

from ctf_downloader.services.git_workflow import (
    GitWorkflowError,
    GitWorkflowService,
)
from test_git_workflow import git


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


def test_finish_remote_delete_failure_retry_is_idempotent(git_env, monkeypatch):
    repo, remote, _info = git_env
    ws = repo / "Retry_Finish_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Retry Finish CTF")
    branch = meta["branch"]
    (ws / "solve.txt").write_text("done\n", encoding="utf-8")

    original_run = GitWorkflowService._run
    failed_once = {"done": False}

    def flaky_run(repo_path, args, *, check=True, env=None):
        if (
            list(args) == ["push", "origin", "--delete", branch]
            and not failed_once["done"]
        ):
            failed_once["done"] = True
            return subprocess.CompletedProcess(
                ["git", *list(args)],
                1,
                stdout="",
                stderr="injected remote delete failure",
            )
        return original_run(repo_path, args, check=check, env=env)

    monkeypatch.setattr(GitWorkflowService, "_run", staticmethod(flaky_run))

    with pytest.raises(GitWorkflowError, match="không xóa được remote branch"):
        GitWorkflowService.finish(ws)

    # Merge + merged metadata commit already happened and main was pushed.
    stored = GitWorkflowService._load_meta(ws)
    assert stored["status"] == "merged"
    assert stored["merged_into"] == "main"
    assert GitWorkflowService._branch_exists(repo, branch)

    merge_count_before = int(
        git(repo, "rev-list", "--count", "--merges", "main").stdout.strip()
    )
    head_before = git(repo, "rev-parse", "main").stdout.strip()

    # Restore real git and retry. The retry must ONLY finish cleanup; it must
    # not switch back to the event branch, checkpoint it, or merge again.
    monkeypatch.setattr(GitWorkflowService, "_run", staticmethod(original_run))
    result = GitWorkflowService.finish(ws)

    assert result["resumed_cleanup"] is True
    assert result["already_merged"] is True
    assert result["remote_deleted"] is True
    assert result["local_deleted"] is True
    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert not GitWorkflowService._branch_exists(repo, branch)

    merge_count_after = int(
        git(repo, "rev-list", "--count", "--merges", "main").stdout.strip()
    )
    assert merge_count_after == merge_count_before
    assert git(repo, "rev-parse", "main").stdout.strip() == head_before

    remote_heads = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"refs/heads/{branch}" not in remote_heads


def test_finish_retry_refuses_to_delete_branch_with_new_unmerged_commit(
    git_env, monkeypatch
):
    repo, _remote, _info = git_env
    ws = repo / "Diverged_Finish_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Diverged Finish CTF")
    branch = meta["branch"]
    (ws / "solve.txt").write_text("done\n", encoding="utf-8")

    original_run = GitWorkflowService._run
    failed_once = {"done": False}

    def flaky_run(repo_path, args, *, check=True, env=None):
        if (
            list(args) == ["push", "origin", "--delete", branch]
            and not failed_once["done"]
        ):
            failed_once["done"] = True
            return subprocess.CompletedProcess(
                ["git", *list(args)], 1, stdout="", stderr="injected"
            )
        return original_run(repo_path, args, check=check, env=env)

    monkeypatch.setattr(GitWorkflowService, "_run", staticmethod(flaky_run))
    with pytest.raises(GitWorkflowError):
        GitWorkflowService.finish(ws)
    monkeypatch.setattr(GitWorkflowService, "_run", staticmethod(original_run))

    # Simulate an external actor adding work to the still-present event branch.
    git(repo, "switch", branch)
    (ws / "late.txt").write_text("late work\n", encoding="utf-8")
    git(repo, "add", "--", f"{ws.name}/late.txt")
    git(repo, "commit", "-m", "late event work")
    git(repo, "switch", "main")

    with pytest.raises(GitWorkflowError, match="commit mới chưa nằm trong base"):
        GitWorkflowService.finish(ws)

    assert GitWorkflowService._branch_exists(repo, branch)
    assert git(repo, "show", f"{branch}:{ws.name}/late.txt").stdout == "late work\n"


def test_prepare_pull_refuses_to_resurrect_merged_event_branch(git_env):
    repo, _remote, _info = git_env
    ws = repo / "Completed_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Completed CTF")
    branch = meta["branch"]
    (ws / "done.txt").write_text("done\n", encoding="utf-8")
    GitWorkflowService.finish(ws, push=False, delete_remote=False)

    assert not GitWorkflowService._branch_exists(repo, branch)
    assert GitWorkflowService._load_meta(ws)["status"] == "merged"

    with pytest.raises(GitWorkflowError, match="đã merge.*Không tự tạo lại"):
        GitWorkflowService.prepare_pull(ws, "Completed CTF")

    assert not GitWorkflowService._branch_exists(repo, branch)
    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
