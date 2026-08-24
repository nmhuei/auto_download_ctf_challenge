"""StorageManager — kiểm soát dung lượng workspace CTF + archive lên git.

Trách nhiệm (spec storage-manager):

- **scan_usage**: duyệt mọi workspace con của thư mục gốc (mặc định
  ``~/Workspace/CTF``), trả danh sách :class:`WorkspaceUsage` với tổng dung
  lượng, breakdown theo loại (attachments/writeups/solvers/misc), top file
  lớn nhất, số challenge và thời điểm giải ended (từ mirror
  ``challenges.json → ctf_info.event_window.end`` của feature Event Window).
- **format_report**: bảng rich-ready sắp theo size giảm dần, size
  human-readable (B/KiB/MiB/GiB), đánh dấu ⚠️ vượt ngưỡng và 🏁 đã ended.
- **archive_workspace**: đóng gói tar.gz (exclude rác runtime) và tuỳ chọn
  push lên git remote user cấu hình. KHÔNG tự tạo remote trên dịch vụ nào —
  chỉ thao tác git subprocess với remote được truyền vào.
- **delete_workspace**: xoá an toàn vào "thùng rác" = rename sang
  ``_archives/<name>_DELETED_<ts>`` (KHÔNG rm -rf, không tự gọi nội bộ —
  chỉ dành cho CLI gọi sau khi user confirm).
- **suggest_actions**: gợi ý tiếng Việt (archive ws ended >7 ngày, warn vượt
  ngưỡng, cảnh báo đĩa gần đầy).

Method thuần, không dính I/O mạng, dễ test trong tmpdir.
"""
import datetime as _dt
import fnmatch
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..storage.workspace_repo import WorkspaceRepo

# Thư mục con chuẩn của một challenge (workspace layout:
# <ws>/<Category>/<Chall>/{challenge,script,solver,writeup}/)
_CHALLENGE_MARKERS = {"challenge", "script", "solver", "writeup"}

# Thư mục bỏ qua khi scan dung lượng (rác sinh ra lúc chạy)
_SCAN_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}

# Workspace hệ thống không phải dữ liệu CTF của user
_SYSTEM_WORKSPACES = {"_archives"}

# Exclude mặc định khi archive (state runtime không đáng lưu)
DEFAULT_EXCLUDE_FILES = ("*.pyc", "*.part", "*.tmp")
DEFAULT_EXCLUDE_DIRS = ("__pycache__", ".pytest_cache", ".git")
DEFAULT_EXCLUDE_PATHS = (".ctf/watch_state.json",)

_ARCHIVE_DIR_NAME = "_archives"
_DELETED_PREFIX = "_DELETED_"

_TIMEZONE = _dt.timezone.utc


class StorageError(Exception):
    """Lỗi thao tác storage (tar/git/...) kèm ngữ cảnh gốc (stderr git...)."""


@dataclass
class WorkspaceUsage:
    """Dữ liệu dung lượng của một workspace."""

    name: str
    path: str
    total_bytes: int
    breakdown: Dict[str, int]  # attachments / writeups / solvers / misc
    largest_files: List[Tuple[str, int]]  # top-10 (path tương đối, size)
    challenge_count: int
    ended: Optional[_dt.datetime] = None


def human_size(num_bytes: float) -> str:
    """Human-readable theo đơn vị nhị phân: B/KiB/MiB/GiB/TiB."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def parse_event_end(value: Any) -> Optional[_dt.datetime]:
    """Parse ``event_window.end`` (epoch giây hoặc ISO 8601) → aware datetime.

    Trả ``None`` khi thiếu/không parse được (giống hành vi tolerant của
    StatusService._render_window).
    """
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.isdigit()
        ):
            return _dt.datetime.fromtimestamp(float(value), tz=_TIMEZONE)
        iso = str(value).replace("Z", "+00:00")
        parsed = _dt.datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_TIMEZONE)
        return parsed
    except (ValueError, TypeError, OSError, OverflowError):
        return None


class StorageManager:
    """Facade thuần cho các thao tác dung lượng/archive (xem module docstring)."""

    # ------------------------------------------------------------------
    # 1. scan_usage
    # ------------------------------------------------------------------

    @staticmethod
    def scan_usage(base_dir: str | os.PathLike) -> List[WorkspaceUsage]:
        """Duyệt mọi workspace con của ``base_dir``, trả list sắp theo tên.

        - Breakdown phân loại theo thành phần đường dẫn: có segment
          ``challenge`` → attachments; ``writeup`` → writeups;
          ``solver`` → solvers; còn lại → misc.
        - Bỏ qua ``__pycache__/.git/.pytest_cache`` và workspace hệ thống
          (``_archives``).
        - ``ended`` đọc từ mirror Event Window; thiếu/không hợp lệ → None.
        """
        base = Path(base_dir).expanduser()
        usages: List[WorkspaceUsage] = []
        if not base.is_dir():
            return usages

        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name in _SYSTEM_WORKSPACES:
                continue
            if entry.name.startswith("."):
                continue
            usages.append(StorageManager._scan_one(entry))
        return usages

    @staticmethod
    def _scan_one(ws_path: Path) -> WorkspaceUsage:
        total = 0
        breakdown = {
            "attachments": 0,
            "writeups": 0,
            "solvers": 0,
            "misc": 0,
        }
        files: List[Tuple[str, int]] = []
        challenge_dirs: set = set()

        for root, dirnames, filenames in os.walk(ws_path):
            dirnames[:] = sorted(d for d in dirnames if d not in _SCAN_SKIP_DIRS)
            for fname in sorted(filenames):
                fpath = Path(root) / fname
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                rel = fpath.relative_to(ws_path).as_posix()
                parts = fpath.relative_to(ws_path).parts
                total += size
                files.append((rel, size))
                breakdown[StorageManager._classify(parts)] += size

        # challenge_count: đếm thư mục depth>=2 (Category/Chall) chứa ít nhất
        # một marker subdir.
        for root, dirnames, _filenames in os.walk(ws_path):
            dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
            rel_parts = Path(root).relative_to(ws_path).parts
            if len(rel_parts) >= 2 and _CHALLENGE_MARKERS.intersection(dirnames):
                challenge_dirs.add(str(Path(root).relative_to(ws_path)))

        largest = sorted(files, key=lambda item: (-item[1], item[0]))[:10]

        ended = None
        try:
            win = ((WorkspaceRepo(ws_path).read_challenges().get("ctf_info") or {})
                   .get("event_window") or {})
            ended = parse_event_end(win.get("end"))
        except Exception:
            ended = None

        return WorkspaceUsage(
            name=ws_path.name,
            path=str(ws_path),
            total_bytes=total,
            breakdown=breakdown,
            largest_files=largest,
            challenge_count=len(challenge_dirs),
            ended=ended,
        )

    @staticmethod
    def _classify(rel_parts: Tuple[str, ...]) -> str:
        """Phân loại file theo segment đầu tiên khớp trong relative parts."""
        for part in rel_parts:
            if part == "challenge":
                return "attachments"
            if part == "writeup":
                return "writeups"
            if part == "solver":
                return "solvers"
        return "misc"

    # ------------------------------------------------------------------
    # 2. format_report
    # ------------------------------------------------------------------

    @staticmethod
    def format_report(
        usages: Sequence[WorkspaceUsage], threshold_mb: int = 1024
    ) -> str:
        """Bảng rich-ready: workspace sắp theo size giảm dần + dòng tổng.

        Cột: icon 📦📦📄🖥️ cho breakdown, 💾 cho tổng. ⚠️ đánh dấu workspace
        vượt ``threshold_mb``, 🏁 đánh dấu workspace đã ended.
        """
        threshold_bytes = int(threshold_mb) * 1024 * 1024
        rows = sorted(usages, key=lambda u: u.total_bytes, reverse=True)

        name_w = max(
            [len("Workspace")]
            + [len(StorageManager._display_name(u)) for u in rows]
        )
        headers = ["Workspace", "📦 Attach", "📄 Writeup", "🖥️ Solver", "💾 Total"]
        col_ws = [
            [human_size(u.breakdown.get("attachments", 0)) for u in rows],
            [human_size(u.breakdown.get("writeups", 0)) for u in rows],
            [human_size(u.breakdown.get("solvers", 0)) for u in rows],
            [human_size(u.total_bytes) for u in rows],
        ]
        size_w = [
            max([len(h)] + [len(v) for v in col]) if rows else len(h)
            for h, col in zip(headers[1:], col_ws)
        ]

        lines: List[str] = []
        header = (
            f"{headers[0]:<{name_w}}  "
            + "  ".join(f"{h:>{w}}" for h, w in zip(headers[1:], size_w))
            + "  Challs  Note"
        )
        lines.append(header)
        lines.append("-" * len(header))
        grand = 0
        for idx, usage in enumerate(rows):
            grand += usage.total_bytes
            notes: List[str] = []
            if usage.total_bytes > threshold_bytes:
                notes.append("⚠️")
            if usage.ended is not None and usage.ended <= _dt.datetime.now(_TIMEZONE):
                notes.append("🏁")
            note_s = " ".join(notes)
            cells = "  ".join(
                f"{col[idx]:>{w}}" for col, w in zip(col_ws, size_w)
            )
            lines.append(
                f"{StorageManager._display_name(usage):<{name_w}}  {cells}"
                f"  {usage.challenge_count:>6}  {note_s}"
            )
        lines.append("-" * len(header))
        lines.append(
            f"{'💾 TOTAL':<{name_w}}  "
            + "  ".join(" " * w for w in size_w[:-1])
            + f"{human_size(grand):>{size_w[-1]}}"
        )
        if not rows:
            lines.append("(không có workspace nào)")
        return "\n".join(lines)

    @staticmethod
    def _display_name(usage: WorkspaceUsage) -> str:
        """Tên hiển thị: gắn marker ⚠️/🏁 ngay đầu tên để sort/filter dễ nhìn."""
        marks = ""
        now = _dt.datetime.now(_TIMEZONE)
        if usage.ended is not None and usage.ended <= now:
            marks += "🏁"
        return f"{marks}{usage.name}"

    # ------------------------------------------------------------------
    # 3. archive_workspace
    # ------------------------------------------------------------------

    @staticmethod
    def archive_workspace(
        ws_path: str | os.PathLike,
        out_dir: str | os.PathLike | None = None,
        *,
        strip_patterns: Optional[Sequence[str]] = None,
        git_remote: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Đóng gói workspace thành ``<name>_<YYYYMMDD>.tar.gz``.

        Exclude mặc định: ``__pycache__/``, ``*.pyc``, ``.pytest_cache/``,
        ``.git/``, ``*.part``, ``*.tmp``, ``.ctf/watch_state.json``; cộng thêm
        ``strip_patterns`` (fnmatch trên relative posix path, pattern kết thúc
        ``/`` khớp cả cây thư mục).

        Trả ``{archive_path, original_bytes, archived_bytes, ratio}``.

        ``git_remote``: sau khi tạo archive, đảm bảo ``out_dir`` là git repo
        (init + commit nếu chưa), rồi commit archive và push tới remote nếu
        out_dir đã có remote. Lỗi git → raise :class:`StorageError` kèm stderr
        (không nuốt).
        """
        src = Path(ws_path).expanduser().resolve()
        if not src.is_dir():
            raise StorageError(f"Workspace không tồn tại: {src}")
        dest = (
            Path(out_dir).expanduser()
            if out_dir is not None
            else src.parent / _ARCHIVE_DIR_NAME
        )
        dest.mkdir(parents=True, exist_ok=True)

        stamp = _dt.datetime.now(_TIMEZONE).strftime("%Y%m%d")
        archive_path = dest / f"{src.name}_{stamp}.tar.gz"
        patterns = list(strip_patterns or [])

        original_bytes = 0
        with tarfile.open(archive_path, "w:gz") as tf:
            for root, dirnames, filenames in os.walk(src):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS
                    and not StorageManager._dir_excluded(root, d, src, patterns)
                )
                for fname in sorted(filenames):
                    fpath = Path(root) / fname
                    rel = fpath.relative_to(src).as_posix()
                    if StorageManager._file_excluded(rel, patterns):
                        continue
                    try:
                        size = fpath.stat().st_size
                    except OSError:
                        continue
                    original_bytes += size
                    tf.add(fpath, arcname=rel, recursive=False)

        archived_bytes = archive_path.stat().st_size
        ratio = (
            round(archived_bytes / original_bytes, 4) if original_bytes else 0.0
        )
        result = {
            "archive_path": str(archive_path),
            "original_bytes": original_bytes,
            "archived_bytes": archived_bytes,
            "ratio": ratio,
        }

        if git_remote:
            StorageManager._git_commit_and_push(dest, git_remote)
        return result

    @staticmethod
    def _rel_to_src(root: str, src: Path) -> str:
        return str(Path(root).relative_to(src))

    @staticmethod
    def _dir_excluded(root: str, dirname: str, src: Path,
                      patterns: Sequence[str]) -> bool:
        rel_dir = f"{StorageManager._rel_to_src(root, src)}/{dirname}".lstrip("/")
        for pat in patterns:
            p = pat.rstrip("/") + "/"
            if fnmatch.fnmatch(rel_dir + "/", p) or fnmatch.fnmatch(
                rel_dir, pat.rstrip("/")
            ):
                return True
        return False

    @staticmethod
    def _file_excluded(rel: str, patterns: Sequence[str]) -> bool:
        if rel in DEFAULT_EXCLUDE_PATHS:
            return True
        if fnmatch.fnmatch(rel, "*.pyc") or fnmatch.fnmatch(
            rel, "*.part"
        ) or fnmatch.fnmatch(rel, "*.tmp"):
            return True
        for pat in patterns:
            base = pat.rstrip("/")
            # fnmatch '*' ăn cả '/', nên khớp cả prefix-less lẫn ở giữa cây.
            if fnmatch.fnmatch(rel, base) or fnmatch.fnmatch(
                rel, "*/" + base
            ):
                return True
        return False

    @staticmethod
    def _run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(cwd), capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise StorageError(
                f"git {' '.join(args)} thất bại (exit {proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        return proc

    @staticmethod
    def _has_remote(repo_dir: Path) -> bool:
        try:
            proc = subprocess.run(
                ["git", "remote"], cwd=str(repo_dir), capture_output=True, text=True
            )
            return proc.returncode == 0 and bool(proc.stdout.strip())
        except OSError:
            return False

    @staticmethod
    def _git_commit_and_push(out_dir: Path, git_remote: str) -> None:
        """Commit archive trong out_dir; push nếu repo đã có remote.

        KHÔNG tự tạo remote — chỉ push tới ``git_remote`` do user cấu hình
        (được dùng làm remote 'origin' khi repo chưa có remote nào).
        """
        if not (out_dir / ".git").exists():
            StorageManager._run_git(["init"], out_dir)
        remotes_proc = subprocess.run(
            ["git", "remote"], cwd=str(out_dir), capture_output=True, text=True
        )
        has_origin = bool(remotes_proc.stdout.strip())
        if git_remote and not has_origin:
            StorageManager._run_git(["remote", "add", "origin", git_remote],
                                    out_dir)

        StorageManager._run_git(["add", "."], out_dir)
        # Commit: cho phép "nothing to commit" (archive giống hệt lần trước)
        commit = subprocess.run(
            ["git", "commit", "-m",
             f"archive: backup {_dt.datetime.now(_TIMEZONE):%Y-%m-%d}"],
            cwd=str(out_dir), capture_output=True, text=True,
        )
        combined = (commit.stdout + commit.stderr).lower()
        if commit.returncode != 0 and "nothing to commit" not in combined:
            raise StorageError(
                f"git commit thất bại (exit {commit.returncode}): "
                f"{commit.stderr.strip()}"
            )

        if StorageManager._has_remote(out_dir):
            branch_proc = StorageManager._run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"], out_dir
            )
            branch = branch_proc.stdout.strip() or "master"
            StorageManager._run_git(["push", "-u", "origin", branch], out_dir)

    # ------------------------------------------------------------------
    # 4. delete_workspace
    # ------------------------------------------------------------------

    @staticmethod
    def delete_workspace(
        ws_path: str | os.PathLike,
        trash_dir: str | os.PathLike | None = None,
    ) -> str:
        """"Xoá" an toàn: rename workspace sang thùng rác
        ``<trash_dir>/<name>_DELETED_<YYYYmmdd_HHMMSS>``.

        KHÔNG rm -rf — dữ liệu luôn phục hồi được bằng mv ngược. Method này
        KHÔNG được gọi nội bộ; chỉ CLI dùng sau khi user confirm.

        Trả về đường dẫn mới (thùng rác).
        """
        src = Path(ws_path).expanduser().resolve()
        if not src.is_dir():
            raise StorageError(f"Workspace không tồn tại: {src}")
        dest_root = (
            Path(trash_dir).expanduser()
            if trash_dir is not None
            else src.parent / _ARCHIVE_DIR_NAME
        )
        dest_root.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_TIMEZONE).strftime("%Y%m%d_%H%M%S")
        target = dest_root / f"{src.name}{_DELETED_PREFIX}{ts}"
        try:
            src.rename(target)
        except OSError as exc:
            raise StorageError(f"Không rename được '{src}' → '{target}': {exc}")
        return str(target)

    # ------------------------------------------------------------------
    # 5. suggest_actions
    # ------------------------------------------------------------------

    ARCHIVE_AFTER_DAYS = 7

    @classmethod
    def suggest_actions(
        cls,
        base_dir: str | os.PathLike,
        threshold_mb: int = 1024,
    ) -> List[str]:
        """Gợi ý tiếng Việt: archive ws ended quá hạn, warn vượt ngưỡng,
        cảnh báo đĩa gần đầy / workspace chiếm gần hết chỗ trống."""
        actions: List[str] = []
        usages = cls.scan_usage(base_dir)
        now = _dt.datetime.now(_TIMEZONE)
        threshold_bytes = int(threshold_mb) * 1024 * 1024
        total_bytes = 0

        for usage in usages:
            total_bytes += usage.total_bytes
            if usage.ended is not None and usage.ended <= now:
                days = (now - usage.ended).days
                if days > cls.ARCHIVE_AFTER_DAYS:
                    end_s = usage.ended.strftime("%Y-%m-%d")
                    actions.append(
                        f"📦 Workspace '{usage.name}' đã kết thúc từ {end_s} "
                        f"({days} ngày trước) — nên archive để giải phóng "
                        f"{human_size(usage.total_bytes)}."
                    )
            if usage.total_bytes > threshold_bytes:
                actions.append(
                    f"⚠️ Workspace '{usage.name}' vượt ngưỡng "
                    f"{threshold_mb} MiB (hiện {human_size(usage.total_bytes)}) "
                    f"— cân nhắc archive hoặc dọn solver/misc."
                )

        try:
            disk = shutil.disk_usage(base_dir)
        except OSError:
            disk = None
        if disk is not None:
            used_pct = disk.used / disk.total * 100 if disk.total else 0
            if used_pct >= 90:
                actions.append(
                    f"🖥️ Đĩa đã dùng {used_pct:.0f}% "
                    f"(còn trống {human_size(disk.free)}) — nên archive bớt."
                )
            elif total_bytes > disk.free and total_bytes > 0:
                actions.append(
                    f"💾 Tổng workspace ({human_size(total_bytes)}) lớn hơn chỗ "
                    f"trống trên đĩa ({human_size(disk.free)}) — archive sớm."
                )

        if not actions:
            actions.append("✅ Mọi thứ ổn — không có hành động nào cần thiết.")
        return actions
