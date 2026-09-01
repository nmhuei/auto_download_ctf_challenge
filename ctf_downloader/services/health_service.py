"""P1-3 — ``ctf doctor``: health-check platform trước giờ giải.

HealthService.check(url, ...) chạy 6 check TUẦN TỰ, mỗi check tự bắt
exception riêng (offline-safe: mạng chết → mọi check ✗ nhưng report vẫn
render đủ, không chết cả report):

  1. URL sống        — GET base với timeout ngắn.
  2. Platform detect — detect_platform_info() → label + confidence.
  3. Auth hợp lệ     — adapter.authenticate(); lỗi → hint "cookie hết hạn?".
  4. Capabilities    — container/scoreboard/rules từ PlatformInfo.
  5. Event window    — fetch_event_times() → start/end + LIVE/countdown.
  6. Flag format     — fetch_rules() + extract_flag_format().

DoctorReport.render() vẽ report PHOSPHOR không viền dọc: ✔ pass · check lỗi
dạng diagnostic mini ``✗ tên → ╰─▶ nguyên nhân → ℹ lệnh fix`` + tổng kết
X/Y checks pass (amber khi chưa pass đủ) + FooterBar cuối surface
(codex-r3 #2 — doctor tự vẽ vì không chạy qua cli._run_framed). Wrap có
chủ đích: continuation của mọi khối thụt đúng cột nội dung, không về cột 1
(codex-r3 #3).
"""
import datetime as _dt
import importlib.metadata as _metadata
import importlib.util as _importlib_util
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.text import Text

from ..platforms.base import EventTimes  # noqa: F401 — type hint
from ..platforms.capabilities import PlatformInfo
from ..platforms.registry import get_spec
from ..ui.style import BRANCH, FAIL, OK, WARN as WARN_GLYPH
from ..ui.theme import (ACCENT, ERROR, FG_BASE, FG_FAINT, FG_MUTED, INFO,
                        SOLVED, WARN)
from ..utils.failure_diagnostics import diagnose_os_error
from ..utils.flag_format import extract_flag_format
from ..utils.http_client import diagnose_request_exception
from ..utils.logger import Logger, console as _default_console
from .session_factory import create_session

# Platform type "yếu" — doctor coi như KHÔNG nhận diện được nền tảng
_WEAK_TYPES = ("unknown", "generic_html")

_CAP_ITEMS = (
    ("container", "container động"),
    ("scoreboard", "scoreboard"),
    ("rules_via_api", "rules qua API"),
)

#: FooterBar chuẩn cho lệnh thường (spec §4.7). Doctor render-một-lần-rồi-
#: thoát (handle_doctor gọi report.render() rồi exit — KHÔNG có vòng đọc
#: phím) → không gợi ý phím ảo; thay bằng các lệnh THẬT, cùng bộ với
#: cli._FRAME_FOOTER (commit 03030b7).
_FOOTER_BINDINGS = (("ctf sync", "đồng bộ"), ("ctf submit", "nộp flag"),
                    ("ctf menu", "console tương tác"))

#: Cột nội dung sau glyph kết quả: ``✔/✗`` + 5 spaces (layout bảng cũ).
_LABEL_COL = 6


def _wrap_words(text: str, first_width: int, rest_width: int) -> List[str]:
    """Chia ``text`` thành các chunk greedy theo bề rộng cho trước.

    Chunk đầu rộng ``first_width``, các chunk sau ``rest_width`` — caller
    render chunk sau ở cột thụt của khối nên continuation KHÔNG BAO GIỜ
    về cột 1 khi terminal hẹp (codex-r3 #3).
    """
    limit_first = max(20, int(first_width))
    limit_rest = max(20, int(rest_width))
    lines: List[str] = []
    cur: List[str] = []
    cur_len = 0
    limit = limit_first
    for word in str(text).split(" "):
        if not word:
            continue
        extra = len(word) + (1 if cur else 0)
        if cur and cur_len + extra > limit:
            lines.append(" ".join(cur))
            cur, cur_len, limit = [word], len(word), limit_rest
        else:
            cur.append(word)
            cur_len += extra
    if cur:
        lines.append(" ".join(cur))
    return lines or [""]


@dataclass
class RuntimeCheck:
    """One local capability check used by ctf doctor --runtime."""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = False


class RuntimeReport:
    """Dense local-runtime capability report.

    Missing optional features never make the whole toolkit broken; only
    checks marked required affect core_ok.
    """

    def __init__(self, checks: Optional[List[RuntimeCheck]] = None):
        self.checks = list(checks or [])

    def add(
        self,
        name: str,
        ok: bool,
        detail: str,
        *,
        fix: str = "",
        required: bool = False,
    ) -> RuntimeCheck:
        chk = RuntimeCheck(
            name=name,
            ok=bool(ok),
            detail=str(detail),
            fix=str(fix),
            required=bool(required),
        )
        self.checks.append(chk)
        return chk

    @property
    def core_checks(self) -> List[RuntimeCheck]:
        return [c for c in self.checks if c.required]

    @property
    def feature_checks(self) -> List[RuntimeCheck]:
        return [c for c in self.checks if not c.required]

    @property
    def core_ok(self) -> bool:
        return all(c.ok for c in self.core_checks)

    def render(self, console=None) -> None:
        from ..ui.banner import app_header

        out = console or _default_console
        width = getattr(out, "width", None) or 80
        out.print(
            app_header(
                "doctor/runtime",
                context="local capabilities",
                timestamp=DoctorReport._timestamp(),
                width=width,
            )
        )
        out.print(Text("RUNTIME", style=FG_FAINT))

        for chk in self.checks:
            line = Text()
            if chk.ok:
                glyph, glyph_style = OK, SOLVED
            elif chk.required:
                glyph, glyph_style = FAIL, ERROR
            else:
                glyph, glyph_style = WARN_GLYPH, WARN

            line.append(glyph, style=glyph_style)
            line.append("  ")
            line.append("CORE " if chk.required else "FEATURE ", style=FG_FAINT)
            line.append(chk.name, style=FG_BASE)
            if chk.detail:
                line.append(" · ", style=FG_FAINT)
                chunks = _wrap_words(
                    chk.detail,
                    max(20, width - len(chk.name) - 13),
                    max(20, width - 6),
                )
                line.append(chunks[0], style=FG_MUTED)
                for chunk in chunks[1:]:
                    line.append("\n      ")
                    line.append(chunk, style=FG_MUTED)
            if not chk.ok and chk.fix:
                line.append("\n      ")
                line.append("ℹ ", style=INFO)
                fix_indent = 8  # six-space block indent + "ℹ "
                fix_chunks = _wrap_words(
                    chk.fix,
                    max(20, width - fix_indent),
                    max(20, width - fix_indent),
                )
                line.append(fix_chunks[0], style=FG_MUTED)
                for chunk in fix_chunks[1:]:
                    line.append("\n" + " " * fix_indent)
                    line.append(chunk, style=FG_MUTED)
            out.print(line)

        core = self.core_checks
        features = self.feature_checks
        core_ready = sum(1 for c in core if c.ok)
        feature_ready = sum(1 for c in features if c.ok)
        out.print(Text("KẾT QUẢ", style=FG_FAINT))
        summary = Text()
        summary.append(
            (OK if self.core_ok else FAIL) + " ",
            style=SOLVED if self.core_ok else ERROR,
        )
        summary.append(
            f"core {core_ready}/{len(core)} ready",
            style=FG_BASE,
        )
        summary.append(
            f" · feature {feature_ready}/{len(features)} available",
            style=FG_MUTED,
        )
        out.print(summary)


@dataclass
class DoctorCheck:
    """Kết quả một check riêng lẻ của báo cáo doctor.

    ``fix`` (tuỳ chọn) là lệnh/hành động sửa lỗi, render sau cause chain
    với glyph ``ℹ``. ``caps`` (tuỳ chọn) là capability map dạng
    ``(nhãn, ok)`` — render từng ``✔/✗`` tô semantic riêng thay vì nhét
    glyph vào một chuỗi detail muted.
    """
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    caps: Optional[List[Tuple[str, bool]]] = None


class DoctorReport:
    """Gom kết quả các check + render PHOSPHOR (không bảng kẻ dọc)."""

    def __init__(self, url: str = ""):
        self.url = url
        self.checks: List[DoctorCheck] = []

    def add(self, name: str, ok: bool, detail: str = "",
            fix: str = "", caps: Optional[List[Tuple[str, bool]]] = None) -> DoctorCheck:
        chk = DoctorCheck(name=name, ok=ok, detail=detail, fix=fix, caps=caps)
        self.checks.append(chk)
        return chk

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def total(self) -> int:
        return len(self.checks)

    def all_passed(self) -> bool:
        return self.total > 0 and self.passed == self.total

    @staticmethod
    def _fail_content(chk: DoctorCheck, width: int = 80) -> Text:
        """Diagnostic mini: tên → ``╰─▶`` nguyên nhân → ``ℹ`` lệnh fix.

        ``╰─▶`` là connector cấu trúc → muted (KHÔNG đỏ — đỏ chỉ dành cho
        glyph kết quả ``✗``, luật vai trò màu PHOSPHOR §3). Glyph kết quả
        đứng cột đầu, continuation thụt đúng cột nội dung của khối (sau
        ``╰─▶ `` / sau ``ℹ ``) — không bao giờ wrap về cột 1 (codex-r3 #3).
        """
        branch_col = _LABEL_COL + len(BRANCH) + 1   # sau "╰─▶ "
        fix_col = _LABEL_COL + 2                    # sau "ℹ "
        content = Text()
        content.append(FAIL, style=ERROR)
        content.append("     ")
        content.append(chk.name)
        if chk.detail:
            content.append("\n" + " " * _LABEL_COL)
            content.append(BRANCH + " ", style=FG_MUTED)
            for i, chunk in enumerate(
                    _wrap_words(chk.detail, width - branch_col,
                                width - branch_col)):
                if i:
                    content.append("\n" + " " * branch_col)
                content.append(chunk)
        if chk.fix:
            content.append("\n" + " " * _LABEL_COL)
            content.append("ℹ ", style=INFO)
            for i, chunk in enumerate(
                    _wrap_words(chk.fix, width - fix_col, width - fix_col)):
                if i:
                    content.append("\n" + " " * fix_col)
                content.append(chunk)
        return content

    @staticmethod
    def _timestamp() -> str:
        """Timestamp faint mép phải AppHeader — giờ local + offset UTC
        (cùng format với cli._frame_timestamp, replicate để tránh import
        vòng cli ↔ services)."""
        try:
            now = _dt.datetime.now().astimezone()
            offset = now.utcoffset() or _dt.timedelta(0)
            off_h = int(offset.total_seconds() // 3600)
            return f"{now:%H:%M} UTC{off_h:+d}"
        except Exception:
            return ""

    def render(self, console=None) -> None:
        from ..ui.banner import app_header

        out = console or _default_console
        width = getattr(out, "width", None) or 80

        # AppHeader chuẩn như các lệnh khác (spec §4.1):
        # UCS_ExOdia // doctor + spectral rail + URL/timestamp context.
        out.print(app_header("doctor", context=self.url or "(chưa có URL)",
                             timestamp=DoctorReport._timestamp()))
        out.print(Text("CHECK", style=FG_FAINT))
        # Render từng check thành Text độc lập (không rich Table) — bảng
        # natural-width của rich vẫn đệm trắng mọi dòng tới width của dòng
        # dài nhất; in trực tiếp thì không có padding thừa (codex-r2 P0c).
        for chk in self.checks:
            if chk.ok:
                out.print(DoctorReport._ok_content(chk, width))
            else:
                out.print(DoctorReport._fail_content(chk, width))

        out.print(Text("KẾT QUẢ", style=FG_FAINT))
        summary = f"Tổng kết: {self.passed}/{self.total} checks pass"
        if self.all_passed():
            # Green chỉ đi kèm glyph ✔ (luật palette §3): Text NỀN TRUNG TÍNH
            # (synthesis-v6 MF1 — ``Text(style=SOLVED)`` đặt base-style cho cả
            # object nên nhuộm green cả nhãn), glyph tô solved riêng, phần
            # text fg.base.
            line = Text()
            line.append(OK + " ", style=SOLVED)
            line.append(summary + " — platform sẵn sàng cho giờ giải!",
                        style=FG_BASE)
        else:
            # Tổng kết accent amber; glyph ``!`` warn #EAC54F đúng vai trò
            # riêng (không tô cả cụm một màu).
            line = Text(style=ACCENT)
            line.append(WARN_GLYPH + " ", style=WARN)
            line.append(summary + " — xem lại các dòng ✗ phía trên.")
        out.print(line)
        out.print()
        # FooterBar hoàn thiện chrome (codex-r3 #2): mọi lệnh thường kết thúc
        # bằng thanh phím tắt — phím amber (hi_fg = accent), nhãn fg.base.
        out.print(DoctorReport._footer_text())

    @staticmethod
    def _footer_text() -> Text:
        """FooterBar spec §4.7 dạng Text (token thuần — in được trên console
        nào, kể cả logger console không theme). Cùng hình dạng với
        ``ui.widgets.footer_bar``: ``ctf sync đồng bộ · ctf submit nộp flag
        · ctf menu console tương tác``."""
        bar = Text()
        for i, (key, label) in enumerate(_FOOTER_BINDINGS):
            if i:
                bar.append(" · ", style="dim")
            bar.append(key, style=ACCENT)
            bar.append(f" {label}", style=FG_BASE)
        return bar

    @staticmethod
    def _ok_content(chk: DoctorCheck, width: int = 80) -> Text:
        # Text nền TRUNG TÍNH (synthesis-v6 MF1): ``Text(OK, style=SOLVED)``
        # đặt base-style SOLVED cho cả object → mọi phần append sau đều
        # tràn green. Glyph ✔ tự mang solved; nhãn/caps/detail style riêng.
        line = Text()
        line.append(OK, style=SOLVED)
        line.append("     ")
        line.append(chk.name)
        if chk.caps is not None:
            # Capability map: từng ✔/✗ tô semantic riêng (✔ solved / ✗
            # error), nhãn muted — không nhét glyph vào chuỗi muted chung.
            for i, (label, ok_cap) in enumerate(chk.caps):
                line.append("  ·  " if i == 0 else " · ", style=FG_FAINT)
                line.append(OK if ok_cap else FAIL,
                            style=SOLVED if ok_cap else ERROR)
                line.append(f" {label}", style=FG_MUTED)
        elif chk.detail:
            # Detail inline sau tên; khi tràn width, phần dôi ra xuống dòng
            # riêng thụt đúng cột nội dung (codex-r3 #3 — không về cột 1).
            sep = " · "
            first_avail = width - _LABEL_COL - len(chk.name) - len(sep)
            chunks = _wrap_words(chk.detail, first_avail, width - _LABEL_COL)
            line.append(sep, style=FG_MUTED)
            line.append(chunks[0], style=FG_MUTED)
            for chunk in chunks[1:]:
                line.append("\n" + " " * _LABEL_COL)
                line.append(chunk, style=FG_MUTED)
        return line


def _fmt_dt(dt: Optional[_dt.datetime]) -> str:
    if dt is None:
        return "?"
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _window_status(times: "EventTimes") -> str:
    """Trạng thái cửa sổ giải so với bây giờ: LIVE / countdown / đã kết thúc."""
    now = _dt.datetime.now(_dt.timezone.utc)
    start, end = times.start_utc, times.end_utc
    if start is not None and now < start:
        remain = (start - now).total_seconds()
        m, s = divmod(max(0, int(remain)), 60)
        h, m = divmod(m, 60)
        return f"CHƯA BẮT ĐẦU (còn {h:d}h{m:02d}m{s:02d}s)"
    if end is not None and now > end:
        return "ĐÃ KẾT THÚC"
    return "ĐANG DIỄN RA (LIVE)"


class HealthService:
    """Chạy bộ health-check ``ctf doctor`` cho một platform URL."""

    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    @staticmethod
    def _distribution_version(name: str) -> str:
        try:
            return _metadata.version(name)
        except _metadata.PackageNotFoundError:
            return "?"

    @classmethod
    def check_runtime(cls, workspace: Optional[str] = None) -> RuntimeReport:
        """Inspect local capabilities without touching the network.

        Core failures affect the exit code. Optional feature gaps are reported
        with a concrete fallback or remediation so users can distinguish
        unavailable integrations from a broken toolkit installation.
        """
        report = RuntimeReport()

        py_ok = sys.version_info >= (3, 10)
        report.add(
            "Python",
            py_ok,
            f"{sys.version.split()[0]} · cần >=3.10",
            fix="Cài Python >=3.10 và tạo lại virtualenv." if not py_ok else "",
            required=True,
        )

        core_packages = (
            ("requests", "requests"),
            ("bs4", "beautifulsoup4"),
            ("rich", "rich"),
            ("urllib3", "urllib3"),
            ("cryptography", "cryptography"),
        )
        missing_core = []
        core_versions = []
        for module_name, dist_name in core_packages:
            if _importlib_util.find_spec(module_name) is None:
                missing_core.append(dist_name)
            else:
                core_versions.append(
                    f"{dist_name}={cls._distribution_version(dist_name)}"
                )
        report.add(
            "Core packages",
            not missing_core,
            (
                " · ".join(core_versions)
                if not missing_core
                else "missing: " + ", ".join(missing_core)
            ),
            fix="python -m pip install -r requirements.txt" if missing_core else "",
            required=True,
        )

        # Config/auth/Bridge token persistence. os.access() alone can lie on
        # ACL/read-only/quota edge cases, so verify with a real fsync'd temp
        # write in the nearest existing parent and remove it immediately.
        config_probe: Optional[Path] = None
        try:
            from ..storage.global_config import CONFIG_DIR

            config_path = Path(CONFIG_DIR).expanduser()
            probe = config_path
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            config_probe = probe
            config_ok = probe.exists() and probe.is_dir()
            if not config_ok:
                raise FileNotFoundError(f"config parent không tồn tại: {probe}")

            tmp_name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(probe),
                    prefix=".ucs-runtime-doctor-",
                    delete=False,
                ) as tf:
                    tmp_name = tf.name
                    tf.write(b"ok")
                    tf.flush()
                    os.fsync(tf.fileno())
                config_detail = f"{config_path} · write/fsync probe OK tại {probe}"
            finally:
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except Exception as exc:
            config_ok = False
            local = diagnose_os_error(exc)
            config_detail = f"{local.code} · {local.summary}"
            config_fix = local.hint
        else:
            config_fix = ""
        report.add(
            "Config storage",
            config_ok,
            config_detail,
            fix=config_fix,
            required=True,
        )

        # Proactive local-resource headroom. These are feature warnings rather
        # than core failures: the CLI can still render status/help with low
        # disk or FD headroom, but downloads/Bridge may fail soon.
        resource_target = config_probe
        if workspace:
            candidate = Path(workspace).expanduser()
            if candidate.exists():
                resource_target = candidate
        if resource_target is not None:
            try:
                usage = shutil.disk_usage(resource_target)
                free_bytes = int(usage.free)
                disk_floor = 256 * 1024 * 1024
                disk_ok = free_bytes >= disk_floor
                if free_bytes >= 1024 ** 3:
                    free_text = f"{free_bytes / (1024 ** 3):.1f} GiB free"
                else:
                    free_text = f"{free_bytes / (1024 ** 2):.0f} MiB free"
                disk_detail = f"{resource_target} · {free_text}"
                disk_fix = (
                    "Giải phóng dung lượng hoặc đổi workspace/output filesystem."
                    if not disk_ok else ""
                )
            except OSError as exc:
                disk_ok = False
                local = diagnose_os_error(exc)
                disk_detail = f"{local.code} · {local.summary}"
                disk_fix = local.hint
            report.add(
                "Disk headroom",
                disk_ok,
                disk_detail,
                fix=disk_fix,
            )

        try:
            import resource

            soft_fd, hard_fd = resource.getrlimit(resource.RLIMIT_NOFILE)
            open_fd_count = None
            proc_fd = Path("/proc/self/fd")
            if proc_fd.is_dir():
                try:
                    open_fd_count = len(list(proc_fd.iterdir()))
                except OSError:
                    open_fd_count = None
            infinity = soft_fd == resource.RLIM_INFINITY
            if infinity:
                fd_ok = True
                fd_detail = "soft limit unlimited"
            elif open_fd_count is None:
                fd_ok = int(soft_fd) >= 128
                fd_detail = f"soft={soft_fd} · open count unavailable"
            else:
                headroom = max(0, int(soft_fd) - int(open_fd_count))
                fd_ok = headroom >= 64
                fd_detail = (
                    f"open={open_fd_count} · soft={soft_fd} · "
                    f"headroom={headroom}"
                )
            fd_fix = (
                "Đóng bớt worker/socket hoặc tăng 'ulimit -n'; chạy lại doctor."
                if not fd_ok else ""
            )
        except (ImportError, AttributeError, OSError, ValueError) as exc:
            # Non-POSIX platforms may not expose RLIMIT_NOFILE. That's an
            # observability gap, not a broken toolkit feature.
            fd_ok = True
            fd_detail = f"không đo được trên OS này ({type(exc).__name__})"
            fd_fix = ""
        report.add(
            "File descriptors",
            fd_ok,
            fd_detail,
            fix=fd_fix,
        )

        feature_packages = (
            ("curl_cffi", "curl_cffi", "Cloudflare browser TLS"),
            ("websockets", "websockets", "Browser Bridge WebSocket"),
            ("gdown", "gdown", "Google Drive attachments"),
        )
        feature_module_ok: Dict[str, bool] = {}
        for module_name, dist_name, label in feature_packages:
            found = _importlib_util.find_spec(module_name) is not None
            version = cls._distribution_version(dist_name) if found else None
            version_ok = True
            if module_name == "websockets" and found and version:
                try:
                    version_ok = int(version.split(".", 1)[0]) >= 15
                except (TypeError, ValueError):
                    version_ok = False
            ok = found and version_ok
            feature_module_ok[module_name] = ok
            detail = (
                f"{version} · {label}"
                if found and version_ok
                else (
                    f"{version} quá cũ; cần >=15 · {label}"
                    if found
                    else f"missing · {label}"
                )
            )
            report.add(
                dist_name,
                ok,
                detail,
                fix="python -m pip install -r requirements.txt" if not ok else "",
            )

        git_path = shutil.which("git")
        report.add(
            "Git workflow",
            bool(git_path),
            git_path or "git không có trong PATH",
            fix=(
                "Cài Git hoặc dùng --no-git / --no-git-push cho pull."
                if not git_path else ""
            ),
        )

        if sys.platform == "darwin":
            opener = shutil.which("open")
        elif os.name == "nt":
            opener = "Windows shell (os.startfile)" if hasattr(os, "startfile") else None
        else:
            opener = shutil.which("xdg-open") or shutil.which("gio")
        report.add(
            "Desktop opener",
            bool(opener),
            str(opener) if opener else "không có xdg-open/gio/open",
            fix=(
                "Headless vẫn dùng được: cd vào path tool in ra; GUI Linux cài xdg-utils hoặc GLib/GIO."
                if not opener else ""
            ),
        )

        try:
            from ..downloaders.mega import MegaDownloader

            mega_tool = MegaDownloader.available_tool()
        except Exception as exc:
            mega_tool = None
            mega_detail = f"{type(exc).__name__}: {exc}"
        else:
            mega_detail = mega_tool or "megadl/mega-get không có trong PATH"
        report.add(
            "Mega download",
            bool(mega_tool),
            mega_detail,
            fix="Cài megatools; các attachment không phải Mega vẫn tải bình thường." if not mega_tool else "",
        )

        tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        report.add(
            "Output mode",
            True,
            (
                f"TTY · {cols} cols · truecolor/ANSI tùy terminal"
                if tty
                else f"non-TTY · {cols} cols · plain/no-ANSI fallback"
            ),
        )

        if feature_module_ok.get("websockets"):
            try:
                bridge = cls.check_bridge_health()
                bridge_state = str(bridge.get("state") or "unknown")
                bridge_ok = bridge_state == "ready"
                bridge_detail = (
                    f"{bridge_state} · {bridge.get('host')}:{bridge.get('port')}"
                )
                if bridge.get("error"):
                    bridge_detail += f" · {bridge['error']}"
                bridge_fix = {
                    "runtime-unavailable": (
                        "Chạy 'python -m pip install -r requirements.txt'."
                    ),
                    "port-conflict": (
                        "Dừng process đang chiếm port Bridge hoặc cấu hình port "
                        "Bridge khác, rồi chạy 'ctf bridge start'."
                    ),
                    "stopped": "Chạy 'ctf bridge start'.",
                    "token-unavailable": (
                        "Kiểm tra quyền token file hoặc chạy 'ctf bridge token'."
                    ),
                    "daemon-only": (
                        "Mở browser có CTF Bridge Extension và đồng bộ token."
                    ),
                    "degraded": (
                        "Chạy 'ctf bridge status', kiểm tra daemon/port/token rồi "
                        "mở lại Browser Extension."
                    ),
                }.get(
                    bridge_state,
                    "Chạy 'ctf bridge status' để xem chẩn đoán.",
                )
            except Exception as exc:
                bridge_ok = False
                bridge_detail = f"{type(exc).__name__}: {exc}"
                bridge_fix = "Chạy 'ctf bridge status' để xem chẩn đoán."
        else:
            bridge_ok = False
            bridge_detail = "websockets unavailable; bridge không thể import/chạy"
            bridge_fix = "Chạy 'python -m pip install -r requirements.txt'."
        report.add(
            "Browser Bridge",
            bridge_ok,
            bridge_detail,
            fix=bridge_fix if not bridge_ok else "",
        )

        if workspace:
            ws = Path(workspace).expanduser()
            ws_ok = ws.is_dir() and os.access(ws, os.R_OK | os.X_OK)
            detail = f"{ws.resolve() if ws.exists() else ws} · " + (
                "readable" if ws_ok else "missing/unreadable"
            )
            report.add(
                "Workspace",
                ws_ok,
                detail,
                fix="Kiểm tra -w và quyền đọc workspace." if not ws_ok else "",
                required=True,
            )

        return report

    @classmethod
    def check_bridge_health(cls) -> Dict[str, Any]:
        """Check daemon, token, and browser-extension readiness separately."""
        import os

        from ..bridge.daemon import BridgeDaemon

        bridge_transport_cls: Any = None
        transport_import_error: Optional[str] = None
        try:
            from ..bridge.transport import (
                BrowserBridgeTransport as _BrowserBridgeTransport,
            )
        except Exception as exc:
            transport_import_error = (
                f"Bridge runtime import failed: {type(exc).__name__}: {exc}"
            )
        else:
            bridge_transport_cls = _BrowserBridgeTransport

        daemon = BridgeDaemon()
        daemon_status = daemon.inspect_status()
        token_exists = os.path.exists(daemon.token_path)
        is_running = bool(daemon_status["owned"])
        extension_connected = False
        error = transport_import_error
        if daemon_status["port_conflict"] and error is None:
            error = (
                f"Port {daemon.host}:{daemon.port} đang listen nhưng không thuộc "
                "PID Bridge được quản lý."
            )
        elif (
            daemon_status["pid_running"]
            and not daemon_status["port_open"]
            and error is None
        ):
            error = (
                f"Bridge PID {daemon_status['pid']} đang sống nhưng port "
                f"{daemon.host}:{daemon.port} chưa listen."
            )

        token = None
        if token_exists:
            try:
                token = daemon.read_token()
            except OSError as exc:
                error = f"Không đọc được token: {type(exc).__name__}: {exc}"

        if is_running and token and bridge_transport_cls is not None:
            try:
                probe = bridge_transport_cls(
                    host=daemon.host,
                    port=daemon.port,
                    token=token,
                    auto_start_daemon=False,
                    timeout=3.0,
                ).probe()
                extension_connected = bool(probe.get("extension_connected"))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

        if transport_import_error:
            state = "runtime-unavailable"
        elif daemon_status["port_conflict"]:
            state = "port-conflict"
        elif daemon_status["pid_running"] and not daemon_status["port_open"]:
            state = "degraded"
        elif not is_running:
            state = "stopped"
        elif not token:
            state = "token-unavailable"
        elif extension_connected:
            state = "ready"
        elif error:
            state = "degraded"
        else:
            state = "daemon-only"

        return {
            "bridge_running": is_running,
            "pid_running": bool(daemon_status["pid_running"]),
            "port_open": bool(daemon_status["port_open"]),
            "port_conflict": bool(daemon_status["port_conflict"]),
            "extension_connected": extension_connected,
            "state": state,
            "error": error,
            "port": daemon.port,
            "host": daemon.host,
            "token_exists": token_exists,
            "pid": daemon_status["pid"],
        }

    def check(self, url: str, cookie: Optional[str] = None,
              token: Optional[str] = None, workspace: Optional[str] = None,
              session: Any = None) -> DoctorReport:
        """Chạy 6 check tuần tự; mỗi check tự chịu trách nhiệm bắt exception."""
        report = DoctorReport(url=url)

        cookie_eff, token_eff = self._resolve_auth(workspace, cookie, token)

        if session is None:
            session = create_session(
                cookie=cookie_eff, token=token_eff,
                timeout=self.timeout, base_url=url,
            )
            # Doctor cần phản hồi NHANH: bỏ retry của session dùng chung
            # (Retry total=3 + backoff nhân thời gian chết mạng lên hàng phút).
            from requests.adapters import HTTPAdapter
            _no_retry = HTTPAdapter(max_retries=0)
            session.mount("http://", _no_retry)
            session.mount("https://", HTTPAdapter(max_retries=0))

        alive = self._check_url(report, url, session)
        platform, info = self._check_detect(report, url, session)
        self._check_auth(report, platform, cookie_eff=cookie_eff, token_eff=token_eff)
        self._check_capabilities(report, info)
        self._check_event_window(report, platform)
        self._check_flag_format(report, platform, workspace=workspace)
        return report

    # ------------------------------------------------------------------ #
    def _resolve_auth(self, workspace, cookie, token):
        """Ưu tiên tham số CLI > auth map (AuthService) khi có workspace."""
        if not workspace:
            return cookie, token
        try:
            from .auth_service import AuthService
            saved_cookie, saved_token = AuthService.resolve(
                str(workspace), cookie_arg=cookie, token_arg=token)
            return saved_cookie, saved_token or token
        except Exception as exc:
            Logger.warning(f"Không đọc được auth map cho workspace '{workspace}': {exc}")
            return cookie, token

    # --------------------------------- 1. URL ------------------------- #
    def _check_url(self, report: DoctorReport, url: str, session) -> bool:
        try:
            resp = session.get(url, timeout=min(self.timeout, 5))
            status = getattr(resp, "status_code", 0)
            try:
                from ..utils.http_client import is_cloudflare_challenge
                cf_challenge = is_cloudflare_challenge(resp)
            except Exception:
                cf_challenge = False
            if cf_challenge:
                report.add(
                    "URL sống", False,
                    f"HTTP {status} — Cloudflare Challenge Page đang chặn client tự động",
                    fix=("Tool đã thử browser fingerprint. Nếu vẫn bị chặn bởi Managed "
                         "Challenge/Turnstile, mở site bằng browser và truyền cookie "
                         "cf_clearance cùng cookie platform (-c 'cf_clearance=...; ...')."),
                )
                return False
            ok = 200 <= status < 400
            report.add("URL sống", ok,
                       f"HTTP {status}" if ok else f"HTTP {status} (server trả lỗi)")
            return ok
        except Exception as exc:
            diag = diagnose_request_exception(exc, method="GET")
            raw = str(exc).replace("\n", " ").strip()
            detail = diag.summary
            if raw and diag.code == "unknown-error":
                detail += f" · {type(exc).__name__}: {raw[:160]}"
            report.add(
                "URL sống",
                False,
                f"Không kết nối được · {diag.code} · {detail}",
                fix=diag.hint,
            )
            return False

    # ------------------------ 2. 🔍 Detect ---------------------------- #
    def _check_detect(self, report: DoctorReport, url: str, session):
        from ..platforms.detector import detect_platform_info
        try:
            # quiet=True: tắt log "[*] Detected Platform" 16-color của
            # detector — doctor tự render kết quả detect theo token
            # PHOSPHOR (P0 codex-r2: không lẫn rainbow vào surface).
            platform, info = detect_platform_info(url, session, quiet=True)
        except Exception as exc:
            report.add("Platform detect", False,
                       f"Lỗi nhận diện: {exc.__class__.__name__}: {exc}")
            return None, None

        ptype = getattr(info, "platform_type", "unknown")
        try:
            label = get_spec(ptype).label
        except Exception:
            label = ptype
        conf = getattr(info, "confidence", "low")
        weak = ptype in _WEAK_TYPES
        report.add("Platform detect", not weak,
                   f"{label} (confidence: {conf})"
                   + ("" if not weak else " — không chắc là platform CTF đã biết"))
        return platform, info

    # -------------------------- 3. 🔑 Auth ---------------------------- #
    def _check_auth(self, report: DoctorReport, platform,
                    cookie_eff=None, token_eff=None):
        if platform is None:
            report.add("Auth hợp lệ", False,
                       "Bỏ qua — không nhận diện được platform adapter")
            return
        if not cookie_eff and not token_eff:
            report.add(
                "Auth hợp lệ", False, "Chưa có cookie/token cho platform này",
                fix="Truyền -c <cookie> / -t <token>, hoặc -w <workspace> "
                    "có auth map đã lưu.")
            return
        try:
            ok = bool(platform.authenticate())
        except Exception as exc:
            Logger.warning(f"authenticate() raised: {exc}")
            ok = False
        if ok:
            user = getattr(getattr(platform, "ctf_info", None), "user_name", None)
            report.add("Auth hợp lệ", True,
                       "Auth OK" + (f" — user: {user}" if user else ""))
        else:
            report.add(
                "Auth hợp lệ", False, "Auth thất bại — cookie hết hạn?",
                fix="Đăng nhập lại platform rồi cập nhật auth map "
                    "(ctf register hoặc -c cookie mới).")

    # --------------------- 4. 🧩 Capabilities ------------------------- #
    def _check_capabilities(self, report: DoctorReport, info: Optional[PlatformInfo]):
        if info is None or getattr(info, "platform_type", "unknown") in _WEAK_TYPES:
            report.add("Capabilities", False,
                       "Không đọc được capability map (platform chưa nhận diện)")
            return
        caps: Dict[str, Any] = getattr(info, "capabilities", {}) or {}
        caps_list = [(label, bool(caps.get(key))) for key, label in _CAP_ITEMS]
        report.add("Capabilities", True, caps=caps_list)

    # -------------------- 5. ⏱️ Event window -------------------------- #
    def _check_event_window(self, report: DoctorReport, platform):
        if platform is None:
            report.add("Event window", False,
                       "Bỏ qua — không có platform adapter")
            return
        try:
            times = platform.fetch_event_times()
        except Exception as exc:
            Logger.warning(f"fetch_event_times raised: {exc}")
            times = None
        if times is None:
            report.add("Event window", False,
                       "Platform không khai báo start/end giải")
            return
        start, end = times.start_utc, times.end_utc
        if start is None and end is None:
            report.add("Event window", False,
                       "Platform trả event window rỗng (không có start/end)")
            return
        if start is not None and end is not None and start > end:
            report.add(
                "Event window", False,
                f"Event window không hợp lệ: start {_fmt_dt(start)} > end {_fmt_dt(end)}"
            )
            return
        status = _window_status(times)
        ended = status == "ĐÃ KẾT THÚC"
        report.add(
            "Event window",
            not ended,
            f"Bắt đầu {_fmt_dt(start)} — kết thúc {_fmt_dt(end)} → {status}",
            fix=("Giải đã kết thúc; kiểm tra URL/event hiện tại trước khi dùng "
                 "watch/sniper/submit.") if ended else "",
        )

    # ---------------------- 6. 🏴 Flag format ------------------------- #
    def _check_flag_format(self, report: DoctorReport, platform,
                           workspace: Optional[str] = None):
        if platform is None:
            report.add("Flag format", False,
                       "Bỏ qua — không có platform adapter")
            return
        try:
            rules = platform.fetch_rules()
        except Exception as exc:
            Logger.warning(f"fetch_rules raised: {exc}")
            rules = None
        fmt = None
        if rules:
            try:
                fmt = extract_flag_format(rules)
            except Exception:
                fmt = None

        local_fmt = None
        if workspace:
            try:
                from ..storage.workspace_repo import WorkspaceRepo
                data = WorkspaceRepo(str(workspace)).read_challenges()
                candidate = ((data.get("ctf_info") or {}).get("flag_format"))
                if isinstance(candidate, str) and candidate.strip():
                    # Reject a corrupt local regex instead of turning a stale
                    # workspace value into a false-green readiness check.
                    import re as _re
                    _re.compile(candidate)
                    local_fmt = candidate.strip()
            except Exception as exc:
                Logger.warning(
                    f"Không đọc/validate được flag_format từ workspace: {exc}"
                )

        if fmt:
            report.add("Flag format", True, f"Tìm thấy từ rules: {fmt}")
        elif local_fmt:
            report.add(
                "Flag format", True,
                f"Dùng baseline workspace: {local_fmt} (rules không cho format rõ)"
            )
        elif rules:
            report.add(
                "Flag format", False,
                "Có rules nhưng không suy ra format flag tự động",
                fix="Nhập tay qua --flag-format (vd FLAG{...}).")
        else:
            report.add(
                "Flag format", False, "Không tải được trang rules",
                fix="Nhập tay qua --flag-format (vd FLAG{...}).")
