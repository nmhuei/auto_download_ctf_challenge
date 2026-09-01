"""Git lifecycle for CTF workspaces.

A contest lives on its own branch while the event is active::

    main -> ctf/<contest-slug> -> (pull/solve/checkpoint/push)
                              -> finish -> merge main -> delete event branch

Only files below the selected workspace are ever staged by checkpoint/push.
Branch switching and finish refuse to run while unrelated working-tree changes
exist, so the tool never silently commits or carries changes from another
workspace/source tree across branches.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..storage.fileio import locked_write_text


class GitWorkflowError(RuntimeError):
    """Git workflow cannot safely continue."""


class GitWorkflowService:
    SCHEMA_VERSION = 1
    DEFAULT_BASE_BRANCH = "main"
    DEFAULT_REMOTE = "origin"
    BRANCH_PREFIX = "ctf/"
    META_REL = Path(".ctf") / "git.json"

    @staticmethod
    def _now_iso() -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _git_executable() -> str:
        """Resolve Git once per operation and fail with an actionable message."""
        exe = shutil.which("git")
        if exe:
            return exe
        raise GitWorkflowError(
            "Không tìm thấy git trong PATH — chức năng Git workflow không "
            "thể chạy. Hãy cài Git và bảo đảm lệnh 'git' có trong PATH."
        )

    @classmethod
    def _run(
        cls,
        repo: Path,
        args: Sequence[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        git_exe = cls._git_executable()
        try:
            proc = subprocess.run(
                [git_exe, *list(args)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except PermissionError as exc:
            raise GitWorkflowError(
                f"Không có quyền thực thi git tại {git_exe}: {exc}"
            ) from exc
        except OSError as exc:
            raise GitWorkflowError(f"Không chạy được git ({git_exe}): {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GitWorkflowError(
                f"git {' '.join(args)} thất bại (exit {proc.returncode}): {detail}"
            )
        return proc

    @classmethod
    def _commit(
        cls,
        repo: Path,
        message: str,
        *,
        pathspec: str | None = None,
    ) -> bool:
        """Commit staged changes, optionally restricted to one workspace path."""
        diff_args = ["diff", "--cached", "--quiet"]
        if pathspec:
            diff_args += ["--", pathspec]
        quiet = cls._run(repo, diff_args, check=False)
        if quiet.returncode == 0:
            return False
        if quiet.returncode not in (0, 1):
            raise GitWorkflowError(
                f"Không kiểm tra được staged diff: {quiet.stderr.strip()}"
            )

        name = cls._run(repo, ["config", "--get", "user.name"], check=False)
        email = cls._run(repo, ["config", "--get", "user.email"], check=False)
        prefix: list[str] = []
        if not name.stdout.strip():
            prefix += ["-c", "user.name=CTF Toolkit"]
        if not email.stdout.strip():
            prefix += ["-c", "user.email=ctf-toolkit@local"]
        commit_args = [*prefix, "commit", "-m", message]
        if pathspec:
            # --only guarantees pre-staged files outside this workspace never
            # hitchhike into an automatic CTF checkpoint.
            commit_args += ["--only", "--", pathspec]
        cls._run(repo, commit_args)
        return True

    @staticmethod
    def _existing_probe(path: Path) -> Path:
        probe = path.expanduser().absolute()
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe

    @classmethod
    def find_repo_root(cls, path: str | os.PathLike) -> Path | None:
        probe = cls._existing_probe(Path(path))
        proc = cls._run(
            probe, ["rev-parse", "--show-toplevel"], check=False
        )
        if proc.returncode != 0:
            return None
        root = proc.stdout.strip()
        return Path(root).resolve() if root else None

    @classmethod
    def initialize_repository(
        cls,
        repo_dir: str | os.PathLike,
        *,
        remote_url: str | None = None,
        base_branch: str = DEFAULT_BASE_BRANCH,
        remote: str = DEFAULT_REMOTE,
        push: bool = True,
        import_existing: bool = False,
    ) -> dict[str, Any]:
        """Initialize the shared CTF repository if needed.

        A non-empty directory is imported only with import_existing=True;
        this prevents accidentally committing old secrets/artifacts merely by
        enabling Git workflow.
        """
        repo = Path(repo_dir).expanduser().resolve()
        repo.mkdir(parents=True, exist_ok=True)

        root = cls.find_repo_root(repo)
        preexisting = [
            p for p in repo.iterdir()
            if p.name != ".git"
        ] if root is None else []
        if root is None and preexisting and not import_existing:
            raise GitWorkflowError(
                f"{repo} đã có {len(preexisting)} mục dữ liệu. "
                "Dùng --import-existing nếu thực sự muốn đưa toàn bộ dữ liệu "
                "hiện có vào commit baseline của main."
            )
        if root is not None and root != repo:
            raise GitWorkflowError(
                f"{repo} đang nằm trong Git repo khác: {root}. "
                "Hãy dùng chính repo root đó thay vì tạo nested repository."
            )

        if root is None:
            proc = cls._run(
                repo, ["init", "-b", base_branch, "."], check=False
            )
            if proc.returncode != 0:
                raise GitWorkflowError(
                    f"git init thất bại: {(proc.stderr or proc.stdout).strip()}"
                )
            root = repo

        head = cls._run(root, ["rev-parse", "--verify", "HEAD"], check=False)
        imported = False
        if head.returncode != 0:
            current = cls._run(
                root, ["symbolic-ref", "--short", "HEAD"], check=False
            ).stdout.strip()
            if current != base_branch:
                cls._run(root, ["symbolic-ref", "HEAD", f"refs/heads/{base_branch}"])
            if preexisting and import_existing:
                cls._run(root, ["add", "-A"])
                imported = cls._commit(
                    root, "chore: import existing CTF workspaces"
                )
            else:
                cls._run(
                    root,
                    [
                        "-c",
                        "user.name=CTF Toolkit",
                        "-c",
                        "user.email=ctf-toolkit@local",
                        "commit",
                        "--allow-empty",
                        "-m",
                        "chore: initialize CTF workspace repository",
                    ],
                )
        elif not cls._branch_exists(root, base_branch):
            raise GitWorkflowError(
                f"Repo chưa có base branch '{base_branch}'. "
                "Hãy tạo/rename branch này trước."
            )

        if remote_url:
            existing = cls._run(root, ["remote", "get-url", remote], check=False)
            if existing.returncode == 0:
                if existing.stdout.strip() != remote_url:
                    raise GitWorkflowError(
                        f"Remote '{remote}' đã tồn tại với URL khác: "
                        f"{existing.stdout.strip()}"
                    )
            else:
                cls._run(root, ["remote", "add", remote, remote_url])

        pushed = False
        if push and cls._remote_exists(root, remote):
            cls._run(root, ["push", "-u", remote, base_branch])
            pushed = True
        return {
            "repo_root": str(root),
            "base_branch": base_branch,
            "remote": remote,
            "pushed": pushed,
            "imported_existing": imported,
        }

    @classmethod
    def branch_name(cls, title: str) -> str:
        raw = unicodedata.normalize("NFKD", str(title or "ctf"))
        ascii_title = raw.encode("ascii", "ignore").decode("ascii").lower()
        slug = re.sub(r"[^a-z0-9._-]+", "-", ascii_title)
        slug = re.sub(r"-{2,}", "-", slug).strip("-./")
        slug = slug or "ctf"
        return cls.BRANCH_PREFIX + slug[:96].rstrip("-.")

    @classmethod
    def _branch_exists(cls, repo: Path, branch: str) -> bool:
        return (
            cls._run(
                repo,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        )

    @classmethod
    def _remote_tracking_exists(cls, repo: Path, remote: str, branch: str) -> bool:
        return (
            cls._run(
                repo,
                [
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/{remote}/{branch}",
                ],
                check=False,
            ).returncode
            == 0
        )

    @classmethod
    def _remote_exists(cls, repo: Path, remote: str) -> bool:
        return (
            cls._run(repo, ["remote", "get-url", remote], check=False).returncode
            == 0
        )

    @classmethod
    def _remote_branch_exists(cls, repo: Path, remote: str, branch: str) -> bool:
        """Query the remote directly instead of trusting stale tracking refs."""
        if not cls._remote_exists(repo, remote):
            return False
        result = cls._run(
            repo,
            ["ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}"],
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    @classmethod
    def _current_branch(cls, repo: Path) -> str:
        return cls._run(repo, ["branch", "--show-current"]).stdout.strip()

    @classmethod
    def _assert_clean(cls, repo: Path, action: str) -> None:
        status = cls._run(
            repo, ["status", "--porcelain", "--untracked-files=all"]
        ).stdout.strip()
        if status:
            preview = "\n".join(status.splitlines()[:12])
            raise GitWorkflowError(
                f"Không thể {action}: Git working tree còn thay đổi chưa commit.\n"
                f"{preview}\n"
                "Hãy commit/stash phần ngoài workspace trước."
            )

    @classmethod
    def _workspace_rel(cls, repo: Path, workspace: Path) -> Path:
        try:
            return workspace.resolve().relative_to(repo.resolve())
        except ValueError as exc:
            raise GitWorkflowError(
                f"Workspace {workspace} không nằm trong Git repo {repo}."
            ) from exc

    @classmethod
    def _meta_path(cls, workspace: Path) -> Path:
        return workspace / cls.META_REL

    @classmethod
    def _load_meta(cls, workspace: Path) -> dict[str, Any]:
        path = cls._meta_path(workspace)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GitWorkflowError(f"Không đọc được {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise GitWorkflowError(f"Git metadata không hợp lệ: {path}")
        return data

    @classmethod
    def _write_meta(cls, workspace: Path, data: dict[str, Any]) -> None:
        path = cls._meta_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_write_text(
            path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @classmethod
    def _repo_for_workspace(
        cls, workspace: Path, *, auto_init: bool, base_branch: str
    ) -> Path:
        repo = cls.find_repo_root(workspace)
        if repo is not None:
            return repo
        if not auto_init:
            raise GitWorkflowError(
                f"Không tìm thấy Git repo chứa {workspace}. "
                "Chạy 'ctf git init -d <thư-mục-cha> --remote-url <url>'."
            )

        parent = workspace.parent
        # Auto-init is safe only for a genuinely empty parent.  Initializing a
        # non-empty ~/Workspace/CTF would turn every pre-existing contest into
        # unrelated untracked state, then branch switching would be blocked.
        # Require an explicit migration/init command instead of mutating it
        # half-way through pull.
        if parent.exists():
            entries = [p for p in parent.iterdir() if p.name != ".git"]
            if entries:
                raise GitWorkflowError(
                    f"{parent} đã có dữ liệu nhưng chưa là Git repo. "
                    f"Chạy 'ctf git init -d {parent} --import-existing' "
                    "nếu muốn đưa dữ liệu cũ vào main, hoặc chọn base dir rỗng."
                )
        result = cls.initialize_repository(
            parent, base_branch=base_branch, push=False
        )
        return Path(result["repo_root"])

    @classmethod
    def _runtime_repo(cls, workspace: Path) -> Path:
        """Resolve the repository from the current filesystem, not metadata.

        This keeps committed .ctf/git.json portable across clone/move/machine.
        """
        repo = cls.find_repo_root(workspace)
        if repo is None:
            raise GitWorkflowError(
                f"Không tìm thấy Git repo chứa workspace {workspace}."
            )
        return repo

    @classmethod
    def prepare_pull(
        cls,
        workspace: str | os.PathLike,
        title: str,
        *,
        base_branch: str = DEFAULT_BASE_BRANCH,
        remote: str = DEFAULT_REMOTE,
        auto_init: bool = True,
    ) -> dict[str, Any]:
        """Checkout/create the event branch before pull writes workspace data."""
        ws = Path(workspace).expanduser().resolve()
        existing_meta = cls._load_meta(ws) if ws.exists() else {}
        base_branch = str(existing_meta.get("base_branch") or base_branch)
        remote = str(existing_meta.get("remote") or remote)
        branch = str(existing_meta.get("branch") or cls.branch_name(title))
        if (existing_meta.get("status") == "merged"
                and existing_meta.get("merged_into") == base_branch):
            raise GitWorkflowError(
                f"Workspace {ws.name} đã merge vào '{base_branch}' và event branch "
                "đã kết thúc. Không tự tạo lại branch lịch sử; nếu thật sự cần "
                "reopen hãy tạo branch mới thủ công hoặc đổi metadata có chủ đích."
            )

        repo = cls._repo_for_workspace(
            ws, auto_init=auto_init, base_branch=base_branch
        )
        rel = cls._workspace_rel(repo, ws)

        current = cls._current_branch(repo)
        if current != branch:
            cls._assert_clean(repo, f"checkout branch '{branch}'")
            if cls._branch_exists(repo, branch):
                cls._run(repo, ["switch", branch])
            elif cls._remote_tracking_exists(repo, remote, branch):
                cls._run(repo, ["switch", "--track", "-c", branch, f"{remote}/{branch}"])
            else:
                if not cls._branch_exists(repo, base_branch):
                    raise GitWorkflowError(
                        f"Không tìm thấy base branch '{base_branch}' trong {repo}."
                    )
                if current != base_branch:
                    cls._run(repo, ["switch", base_branch])
                cls._run(repo, ["switch", "-c", branch])

        ws.mkdir(parents=True, exist_ok=True)
        meta = {
            **existing_meta,
            "schema_version": cls.SCHEMA_VERSION,
            "status": "active",
            "contest_title": str(title or ws.name),
            # repo_root intentionally omitted: absolute paths make metadata
            # unusable after clone/move. Runtime always resolves Git root.
            "workspace": rel.as_posix(),
            "branch": branch,
            "base_branch": base_branch,
            "remote": remote,
            "created_at": existing_meta.get("created_at") or cls._now_iso(),
        }
        # Drop legacy absolute-path field when an older workspace is touched.
        meta.pop("repo_root", None)
        cls._write_meta(ws, meta)
        return meta

    @classmethod
    def checkpoint_and_push(
        cls,
        workspace: str | os.PathLike,
        *,
        message: str | None = None,
        push: bool = True,
        remote: str | None = None,
    ) -> dict[str, Any]:
        """Commit only this workspace and push its event branch."""
        ws = Path(workspace).expanduser().resolve()
        meta = cls._load_meta(ws)
        if not meta:
            raise GitWorkflowError(
                f"Workspace chưa có {cls.META_REL}; hãy pull với Git workflow trước."
            )
        repo = cls._runtime_repo(ws)
        rel = cls._workspace_rel(repo, ws)
        branch = str(meta["branch"])
        current = cls._current_branch(repo)

        if current != branch:
            cls._assert_clean(repo, f"checkout branch '{branch}'")
            if not cls._branch_exists(repo, branch):
                raise GitWorkflowError(f"Không tìm thấy event branch '{branch}'.")
            cls._run(repo, ["switch", branch])

        meta["last_checkpoint_at"] = cls._now_iso()
        cls._write_meta(ws, meta)
        cls._run(repo, ["add", "--", rel.as_posix()])
        committed = cls._commit(
            repo,
            message or f"ctf({ws.name}): checkpoint",
            pathspec=rel.as_posix(),
        )

        remote_name = str(remote or meta.get("remote") or cls.DEFAULT_REMOTE)
        pushed = False
        remote_configured = cls._remote_exists(repo, remote_name)
        if push and remote_configured:
            cls._run(repo, ["push", "-u", remote_name, branch])
            pushed = True

        return {
            "repo_root": str(repo),
            "workspace": str(ws),
            "branch": branch,
            "committed": committed,
            "pushed": pushed,
            "remote": remote_name if remote_configured else None,
        }

    @classmethod
    def status(cls, workspace: str | os.PathLike) -> dict[str, Any]:
        ws = Path(workspace).expanduser().resolve()
        meta = cls._load_meta(ws)
        if not meta:
            raise GitWorkflowError(f"Workspace chưa có {cls.META_REL}.")
        repo = cls._runtime_repo(ws)
        rel = cls._workspace_rel(repo, ws)
        dirty = cls._run(
            repo,
            ["status", "--porcelain", "--untracked-files=all", "--", rel.as_posix()],
        ).stdout.splitlines()
        branch = str(meta["branch"])
        base = str(meta.get("base_branch") or cls.DEFAULT_BASE_BRANCH)
        merged = bool(
            meta.get("status") == "merged" and meta.get("merged_into") == base
        )
        if (not merged and cls._branch_exists(repo, branch)
                and cls._branch_exists(repo, base)):
            merged = (
                cls._run(
                    repo,
                    ["merge-base", "--is-ancestor", branch, base],
                    check=False,
                ).returncode
                == 0
            )
        return {
            **meta,
            "repo_root": str(repo),
            "current_branch": cls._current_branch(repo),
            "dirty_files": len(dirty),
            "merged_into_base": merged,
            "remote_configured": cls._remote_exists(
                repo, str(meta.get("remote") or cls.DEFAULT_REMOTE)
            ),
        }

    @classmethod
    def finish(
        cls,
        workspace: str | os.PathLike,
        *,
        base_branch: str | None = None,
        remote: str | None = None,
        push: bool = True,
        delete_remote: bool = True,
    ) -> dict[str, Any]:
        """Merge a completed contest into main and delete its event branch."""
        ws = Path(workspace).expanduser().resolve()
        meta = cls._load_meta(ws)
        if not meta:
            raise GitWorkflowError(f"Workspace chưa có {cls.META_REL}.")

        repo = cls._runtime_repo(ws)
        branch = str(meta["branch"])
        base = str(base_branch or meta.get("base_branch") or cls.DEFAULT_BASE_BRANCH)
        remote_name = str(remote or meta.get("remote") or cls.DEFAULT_REMOTE)

        if branch == base:
            raise GitWorkflowError("Event branch không được trùng base branch.")
        if remote is not None and not cls._remote_exists(repo, remote_name):
            raise GitWorkflowError(
                f"Remote override '{remote_name}' không tồn tại trong {repo}; "
                "dừng trước final checkpoint/merge."
            )

        # Idempotent retry after a partially completed finish. A previous run
        # may have merged/committed metadata/pushed main, then failed while
        # deleting the remote event branch. Re-running MUST NOT switch back to
        # the event branch and create a new checkpoint after the merge, because
        # that would make the branch no longer an ancestor of main and force a
        # second merge. When metadata already says merged, only finish the
        # remaining publish/delete cleanup steps.
        if meta.get("status") == "merged" and meta.get("merged_into") == base:
            cls._assert_clean(repo, f"resume finish cleanup cho '{branch}'")
            if not cls._branch_exists(repo, base):
                raise GitWorkflowError(f"Không tìm thấy base branch '{base}'.")
            if cls._current_branch(repo) != base:
                cls._run(repo, ["switch", base])

            local_exists = cls._branch_exists(repo, branch)
            if local_exists:
                merged_ok = (
                    cls._run(
                        repo,
                        ["merge-base", "--is-ancestor", branch, base],
                        check=False,
                    ).returncode
                    == 0
                )
                if not merged_ok:
                    raise GitWorkflowError(
                        f"Metadata nói '{branch}' đã merge vào '{base}' nhưng branch "
                        "hiện có commit mới chưa nằm trong base; dừng để không xóa dữ liệu."
                    )

            remote_configured = cls._remote_exists(repo, remote_name)
            base_pushed = False
            if push and remote_configured:
                cls._run(repo, ["push", remote_name, base])
                base_pushed = True

            remote_deleted = False
            if push and remote_configured and delete_remote:
                if cls._remote_branch_exists(repo, remote_name, branch):
                    delete = cls._run(
                        repo,
                        ["push", remote_name, "--delete", branch],
                        check=False,
                    )
                    if delete.returncode != 0:
                        raise GitWorkflowError(
                            f"Main đã merge/push nhưng không xóa được remote branch "
                            f"'{branch}'; local branch được giữ để retry: "
                            f"{(delete.stderr or delete.stdout).strip()}"
                        )
                # Desired state is already true even if another actor/run
                # deleted the remote branch before this retry.
                remote_deleted = True

            if local_exists:
                cls._run(repo, ["branch", "-d", branch])
            return {
                "repo_root": str(repo),
                "workspace": str(ws),
                "branch": branch,
                "base_branch": base,
                "already_merged": True,
                "base_pushed": base_pushed,
                "remote_deleted": remote_deleted,
                "local_deleted": True,
                "resumed_cleanup": True,
            }

        cp = cls.checkpoint_and_push(
            ws,
            message=f"ctf({ws.name}): final checkpoint",
            push=push,
            remote=remote_name,
        )
        cls._assert_clean(repo, f"merge '{branch}' vào '{base}'")
        if not cls._branch_exists(repo, base):
            raise GitWorkflowError(f"Không tìm thấy base branch '{base}'.")
        if not cls._branch_exists(repo, branch):
            raise GitWorkflowError(f"Không tìm thấy event branch '{branch}'.")

        if cls._current_branch(repo) != base:
            cls._run(repo, ["switch", base])

        already_merged = (
            cls._run(
                repo, ["merge-base", "--is-ancestor", branch, base], check=False
            ).returncode
            == 0
        )
        if not already_merged:
            merge = cls._run(
                repo,
                ["merge", "--no-ff", branch, "-m", f"merge(ctf): {ws.name}"],
                check=False,
            )
            if merge.returncode != 0:
                cls._run(repo, ["merge", "--abort"], check=False)
                detail = (merge.stderr or merge.stdout).strip()
                raise GitWorkflowError(
                    f"Merge '{branch}' vào '{base}' thất bại; branch được giữ nguyên: "
                    f"{detail}"
                )

        merged_meta = cls._load_meta(ws)
        merged_meta.update(
            {
                "status": "merged",
                "merged_at": cls._now_iso(),
                "merged_into": base,
                "branch": branch,
            }
        )
        cls._write_meta(ws, merged_meta)
        rel = cls._workspace_rel(repo, ws)
        cls._run(repo, ["add", "--", (rel / cls.META_REL).as_posix()])
        cls._commit(
            repo,
            f"chore(ctf): mark {ws.name} merged",
            pathspec=(rel / cls.META_REL).as_posix(),
        )

        remote_configured = cls._remote_exists(repo, remote_name)
        base_pushed = False
        if push and remote_configured:
            cls._run(repo, ["push", remote_name, base])
            base_pushed = True

        remote_deleted = False
        if push and remote_configured and delete_remote and cp.get("pushed"):
            delete = cls._run(
                repo, ["push", remote_name, "--delete", branch], check=False
            )
            if delete.returncode != 0:
                raise GitWorkflowError(
                    f"Main đã merge/push nhưng không xóa được remote branch "
                    f"'{branch}'; local branch được giữ để retry: "
                    f"{(delete.stderr or delete.stdout).strip()}"
                )
            remote_deleted = True

        cls._run(repo, ["branch", "-d", branch])
        return {
            "repo_root": str(repo),
            "workspace": str(ws),
            "branch": branch,
            "base_branch": base,
            "already_merged": already_merged,
            "base_pushed": base_pushed,
            "remote_deleted": remote_deleted,
            "local_deleted": True,
        }
