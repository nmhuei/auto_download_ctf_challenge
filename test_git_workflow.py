import json
import subprocess
from pathlib import Path

import pytest

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.services.git_workflow import (
    GitWorkflowError,
    GitWorkflowService,
)


def git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
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


def test_missing_git_binary_is_actionable(monkeypatch, tmp_path):
    import ctf_downloader.services.git_workflow as git_mod

    monkeypatch.setattr(git_mod.shutil, "which", lambda name: None)
    with pytest.raises(
        GitWorkflowError,
        match=r"Không tìm thấy git.*PATH.*cài Git",
    ):
        GitWorkflowService.find_repo_root(tmp_path)


def test_branch_name_is_stable_and_git_safe():
    assert GitWorkflowService.branch_name("ASIS CTF Quals 2026") == (
        "ctf/asis-ctf-quals-2026"
    )
    assert GitWorkflowService.branch_name("🎟 The Lottery Race!") == (
        "ctf/the-lottery-race"
    )


def test_prepare_pull_creates_event_branch_and_metadata(git_env):
    repo, _remote, _info = git_env
    ws = repo / "ASIS_CTF_2026"

    meta = GitWorkflowService.prepare_pull(ws, "ASIS CTF 2026")

    assert meta["branch"] == "ctf/asis-ctf-2026"
    assert meta["base_branch"] == "main"
    assert meta["status"] == "active"
    assert git(repo, "branch", "--show-current").stdout.strip() == meta["branch"]

    stored = json.loads((ws / ".ctf" / "git.json").read_text())
    assert stored["workspace"] == "ASIS_CTF_2026"
    # Absolute repo paths must NOT be committed: the workspace may be cloned
    # or moved to another machine. Runtime resolves the enclosing repo.
    assert "repo_root" not in stored
    assert GitWorkflowService._runtime_repo(ws) == repo.resolve()


def test_checkpoint_push_and_finish_merges_main_and_deletes_branches(git_env):
    repo, remote, _info = git_env
    ws = repo / "Demo_CTF_2026"
    meta = GitWorkflowService.prepare_pull(ws, "Demo CTF 2026")
    branch = meta["branch"]

    (ws / "challenge.txt").write_text("flag work\n", encoding="utf-8")
    pushed = GitWorkflowService.checkpoint_and_push(ws)
    assert pushed["committed"] is True
    assert pushed["pushed"] is True

    remote_heads = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"refs/heads/{branch}" in remote_heads

    result = GitWorkflowService.finish(ws)
    assert result["base_branch"] == "main"
    assert result["base_pushed"] is True
    assert result["remote_deleted"] is True
    assert result["local_deleted"] is True

    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
        check=False,
    ).returncode != 0
    assert (ws / "challenge.txt").read_text(encoding="utf-8") == "flag work\n"

    stored = json.loads((ws / ".ctf" / "git.json").read_text())
    assert stored["status"] == "merged"
    assert stored["merged_into"] == "main"
    assert stored.get("merged_at")
    status = GitWorkflowService.status(ws)
    assert status["merged_into_base"] is True
    assert status["current_branch"] == "main"

    remote_heads = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "refs/heads/main" in remote_heads
    assert f"refs/heads/{branch}" not in remote_heads


def test_finish_remote_override_uses_same_remote_for_checkpoint_base_and_delete(git_env, tmp_path):
    repo, origin, _info = git_env
    backup = tmp_path / "backup.git"
    subprocess.run(
        ["git", "init", "--bare", str(backup)],
        capture_output=True, text=True, check=True,
    )
    git(repo, "remote", "add", "backup", str(backup))

    ws = repo / "Remote_Override_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Remote Override CTF")
    branch = meta["branch"]
    (ws / "solve.txt").write_text("done\n", encoding="utf-8")

    result = GitWorkflowService.finish(ws, remote="backup")
    assert result["base_pushed"] is True
    assert result["remote_deleted"] is True

    backup_heads = subprocess.run(
        ["git", "--git-dir", str(backup), "show-ref", "--heads"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "refs/heads/main" in backup_heads
    assert f"refs/heads/{branch}" not in backup_heads

    # Override transaction must not accidentally publish the event branch to
    # the metadata remote (origin).
    origin_heads = subprocess.run(
        ["git", "--git-dir", str(origin), "show-ref", "--heads"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"refs/heads/{branch}" not in origin_heads


def test_finish_missing_remote_override_fails_before_checkpoint_or_merge(git_env):
    repo, _remote, _info = git_env
    ws = repo / "Missing_Remote_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Missing Remote CTF")
    branch = meta["branch"]
    (ws / "unsaved.txt").write_text("local work\n", encoding="utf-8")
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(GitWorkflowError, match="dừng trước final checkpoint"):
        GitWorkflowService.finish(ws, remote="does-not-exist")

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert git(repo, "branch", "--show-current").stdout.strip() == branch
    assert GitWorkflowService._branch_exists(repo, branch)
    assert (ws / "unsaved.txt").read_text(encoding="utf-8") == "local work\n"


def test_finish_refuses_unrelated_dirty_changes_and_keeps_event_branch(git_env):
    repo, _remote, _info = git_env
    ws = repo / "Dirty_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Dirty CTF")
    branch = meta["branch"]
    (ws / "solve.txt").write_text("work\n", encoding="utf-8")
    GitWorkflowService.checkpoint_and_push(ws)

    # Deliberately dirty a path outside the contest workspace.
    (repo / "unrelated.txt").write_text("do not touch\n", encoding="utf-8")

    with pytest.raises(GitWorkflowError, match="working tree"):
        GitWorkflowService.finish(ws)

    assert GitWorkflowService._branch_exists(repo, branch)
    assert git(repo, "branch", "--show-current").stdout.strip() == branch


def test_prepare_pull_reopens_existing_event_branch_from_main(git_env):
    repo, _remote, _info = git_env
    ws = repo / "Resume_CTF"
    meta = GitWorkflowService.prepare_pull(ws, "Resume CTF")
    branch = meta["branch"]
    (ws / "one.txt").write_text("1\n", encoding="utf-8")
    GitWorkflowService.checkpoint_and_push(ws)

    git(repo, "switch", "main")
    assert not ws.exists()

    reopened = GitWorkflowService.prepare_pull(ws, "Resume CTF")
    assert reopened["branch"] == branch
    assert git(repo, "branch", "--show-current").stdout.strip() == branch
    assert (ws / "one.txt").read_text(encoding="utf-8") == "1\n"


def test_cli_git_subcommands_and_pull_git_flags_parse():
    parser = build_unified_parser()

    pull = parser.parse_args(
        [
            "pull",
            "-u",
            "https://ctf.example",
            "--git-base",
            "main",
            "--git-remote",
            "origin",
            "--no-git-push",
        ]
    )
    assert pull.git_base == "main"
    assert pull.git_remote == "origin"
    assert pull.no_git_push is True
    assert pull.no_git is False

    finish = parser.parse_args(
        ["git", "finish", "-w", "ASIS_CTF_2026", "--keep-remote"]
    )
    assert finish.git_command == "finish"
    assert finish.workspace == "ASIS_CTF_2026"
    assert finish.keep_remote is True

    init = parser.parse_args(
        [
            "git", "init", "-d", "/tmp/ctfs",
            "--remote-url", "git@example:x/y.git", "--import-existing",
        ]
    )
    assert init.git_command == "init"
    assert init.dir == "/tmp/ctfs"
    assert init.import_existing is True


def test_checkpoint_never_commits_pre_staged_unrelated_file(git_env):
    repo, _remote, _info = git_env
    ws = repo / "Scoped_CTF"
    GitWorkflowService.prepare_pull(ws, "Scoped CTF")
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = repo / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    git(repo, "add", "outside.txt")

    GitWorkflowService.checkpoint_and_push(ws, push=False)

    committed_names = git(
        repo, "show", "--pretty=", "--name-only", "HEAD"
    ).stdout.splitlines()
    assert "Scoped_CTF/inside.txt" in committed_names
    assert "outside.txt" not in committed_names

    # The user's staged file remains staged and untouched for their own commit.
    staged = git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
    assert "outside.txt" in staged


def test_handle_pull_enables_git_workflow_by_default(monkeypatch):
    from ctf_downloader.cli_commands import handle_pull
    from ctf_downloader.services.pull_service import PullService

    parser = build_unified_parser()
    args = parser.parse_args(["pull", "-u", "https://ctf.example"])
    captured = {}

    def fake_run(config):
        captured["config"] = config
        return {"ok": True}

    monkeypatch.setattr(PullService, "run", fake_run)
    handle_pull(args)

    cfg = captured["config"]
    assert cfg.git_workflow is True
    assert cfg.git_auto_push is True
    assert cfg.git_base_branch == "main"
    assert cfg.git_remote == "origin"

    args = parser.parse_args(
        ["pull", "-u", "https://ctf.example", "--no-git", "--no-git-push"]
    )
    monkeypatch.setattr(PullService, "run", fake_run)
    handle_pull(args)
    cfg = captured["config"]
    assert cfg.git_workflow is False
    assert cfg.git_auto_push is False


def test_init_nonempty_requires_explicit_import(tmp_path):
    repo = tmp_path / "existing-ctfs"
    repo.mkdir()
    (repo / "OLD_CTF").mkdir()
    (repo / "OLD_CTF" / "README.md").write_text("old\n", encoding="utf-8")

    with pytest.raises(GitWorkflowError, match="--import-existing"):
        GitWorkflowService.initialize_repository(repo, push=False)

    # Refusal happens before git init, so the command leaves no half-created repo.
    assert not (repo / ".git").exists()

    result = GitWorkflowService.initialize_repository(
        repo, push=False, import_existing=True
    )
    assert result["imported_existing"] is True
    assert (repo / ".git").is_dir()
    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    tracked = git(repo, "ls-files").stdout.splitlines()
    assert "OLD_CTF/README.md" in tracked


def test_prepare_pull_nonempty_parent_without_git_refuses_before_init(tmp_path):
    parent = tmp_path / "ctfs"
    parent.mkdir()
    (parent / "OLD_CTF").mkdir()
    ws = parent / "NEW_CTF"

    with pytest.raises(GitWorkflowError, match="git init.*--import-existing"):
        GitWorkflowService.prepare_pull(ws, "New CTF")

    assert not (parent / ".git").exists()
