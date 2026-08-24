"""P1-3 — ``ctf doctor`` 🩺: health-check platform trước giờ giải.

HealthService.check(url, ...) chạy 6 check TUẦN TỰ, mỗi check tự bắt
exception riêng (offline-safe: mạng chết → mọi check ❌ nhưng report vẫn
render đủ, không chết cả report):

  1. 🌐 URL sống        — GET base với timeout ngắn.
  2. 🔍 Platform detect — detect_platform_info() → label + confidence.
  3. 🔑 Auth hợp lệ     — adapter.authenticate(); lỗi → hint "cookie hết hạn?".
  4. 🧩 Capabilities    — container/scoreboard/rules từ PlatformInfo (bảng ✅/❌).
  5. ⏱️ Event window    — fetch_event_times() → start/end + LIVE/countdown.
  6. 🏴 Flag format     — fetch_rules() + extract_flag_format().

DoctorReport.render() vẽ rich table tiếng Việt + tổng kết X/Y checks pass.
"""
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..platforms.base import EventTimes  # noqa: F401 — type hint
from ..platforms.capabilities import PlatformInfo
from ..platforms.registry import get_spec
from ..utils.flag_format import extract_flag_format
from ..utils.logger import Logger, console as _default_console
from .session_factory import create_session

# Platform type "yếu" — doctor coi như KHÔNG nhận diện được nền tảng
_WEAK_TYPES = ("unknown", "generic_html")

_CAP_ITEMS = (
    ("container", "container động"),
    ("scoreboard", "scoreboard"),
    ("rules_via_api", "rules qua API"),
)


@dataclass
class DoctorCheck:
    """Kết quả một check riêng lẻ của báo cáo doctor."""
    name: str
    ok: bool
    detail: str = ""
    icon: str = "❓"


class DoctorReport:
    """Gom kết quả các check + render rich table tiếng Việt."""

    def __init__(self, url: str = ""):
        self.url = url
        self.checks: List[DoctorCheck] = []

    def add(self, name: str, ok: bool, detail: str = "", icon: str = "❓") -> DoctorCheck:
        chk = DoctorCheck(name=name, ok=ok, detail=detail, icon=icon)
        self.checks.append(chk)
        return chk

    @staticmethod
    def _mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def total(self) -> int:
        return len(self.checks)

    def all_passed(self) -> bool:
        return self.total > 0 and self.passed == self.total

    def render(self, console=None) -> None:
        from rich.table import Table

        out = console or _default_console
        table = Table(
            title=f"🩺 ctf doctor — {self.url or '(chưa có URL)'}",
            show_lines=False, header_style="bold magenta")
        table.add_column("", justify="center", width=3)
        table.add_column("Check", style="bold cyan", no_wrap=True)
        table.add_column("Kết quả")

        for chk in self.checks:
            table.add_row(self._mark(chk.ok), f"{chk.icon} {chk.name}",
                          chk.detail or "-")

        out.print(table)
        if self.all_passed():
            out.print(f"[bold green]Tổng kết: {self.passed}/{self.total} checks pass "
                      f"— platform sẵn sàng cho giờ giải! 🩺[/bold green]")
        else:
            out.print(f"[bold yellow]Tổng kết: {self.passed}/{self.total} checks pass "
                      f"— xem lại các dòng ❌ phía trên.[/bold yellow]")


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
    return "🔴 ĐANG DIỄN RA"


class HealthService:
    """Chạy bộ health-check ``ctf doctor`` cho một platform URL."""

    def __init__(self, timeout: int = 8):
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    def check(self, url: str, cookie: Optional[str] = None,
              token: Optional[str] = None, workspace: Optional[str] = None,
              session: Any = None) -> DoctorReport:
        """Chạy 6 check tuần tự; mỗi check tự chịu trách nhiệm bắt exception."""
        report = DoctorReport(url=url)

        cookie_eff, token_eff = self._resolve_auth(workspace, cookie, token)

        if session is None:
            session = create_session(cookie=cookie_eff, token=token_eff,
                                     timeout=self.timeout)
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
        self._check_flag_format(report, platform)
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

    # ---------------------------- 1. 🌐 URL --------------------------- #
    def _check_url(self, report: DoctorReport, url: str, session) -> bool:
        try:
            resp = session.get(url, timeout=min(self.timeout, 5))
            status = getattr(resp, "status_code", 0)
            ok = 200 <= status < 400
            report.add("URL sống", ok,
                       f"HTTP {status}" if ok else f"HTTP {status} (server trả lỗi)",
                       icon="🌐")
            return ok
        except Exception as exc:
            report.add("URL sống", False,
                       f"Không kết nối được ({exc.__class__.__name__}) — "
                       f"kiểm tra mạng/VPN/địa chỉ?", icon="🌐")
            return False

    # ------------------------ 2. 🔍 Detect ---------------------------- #
    def _check_detect(self, report: DoctorReport, url: str, session):
        from ..platforms.detector import detect_platform_info
        try:
            platform, info = detect_platform_info(url, session)
        except Exception as exc:
            report.add("Platform detect", False,
                       f"Lỗi nhận diện: {exc.__class__.__name__}: {exc}", icon="🔍")
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
                   + ("" if not weak else " — không chắc là platform CTF đã biết"),
                   icon="🔍")
        return platform, info

    # -------------------------- 3. 🔑 Auth ---------------------------- #
    def _check_auth(self, report: DoctorReport, platform,
                    cookie_eff=None, token_eff=None):
        if platform is None:
            report.add("Auth hợp lệ", False,
                       "Bỏ qua — không nhận diện được platform adapter", icon="🔑")
            return
        if not cookie_eff and not token_eff:
            report.add("Auth hợp lệ", False,
                       "Chưa có cookie/token (truyền -c/-t hoặc -w workspace "
                       "có auth map)", icon="🔑")
            return
        try:
            ok = bool(platform.authenticate())
        except Exception as exc:
            Logger.warning(f"authenticate() raised: {exc}")
            ok = False
        if ok:
            user = getattr(getattr(platform, "ctf_info", None), "user_name", None)
            report.add("Auth hợp lệ", True,
                       "Auth OK" + (f" — user: {user}" if user else ""), icon="🔑")
        else:
            report.add("Auth hợp lệ", False,
                       "Auth thất bại — cookie hết hạn?", icon="🔑")

    # --------------------- 4. 🧩 Capabilities ------------------------- #
    def _check_capabilities(self, report: DoctorReport, info: Optional[PlatformInfo]):
        if info is None or getattr(info, "platform_type", "unknown") in _WEAK_TYPES:
            report.add("Capabilities", False,
                       "Không đọc được capability map (platform chưa nhận diện)",
                       icon="🧩")
            return
        caps: Dict[str, Any] = getattr(info, "capabilities", {}) or {}
        parts = [f"{'✅' if caps.get(key) else '❌'} {label}"
                 for key, label in _CAP_ITEMS]
        report.add("Capabilities", True, " | ".join(parts), icon="🧩")

    # -------------------- 5. ⏱️ Event window -------------------------- #
    def _check_event_window(self, report: DoctorReport, platform):
        if platform is None:
            report.add("Event window", False,
                       "Bỏ qua — không có platform adapter", icon="⏱️")
            return
        try:
            times = platform.fetch_event_times()
        except Exception as exc:
            Logger.warning(f"fetch_event_times raised: {exc}")
            times = None
        if times is None:
            report.add("Event window", False,
                       "Platform không khai báo start/end giải", icon="⏱️")
            return
        report.add("Event window", True,
                   f"Bắt đầu {_fmt_dt(times.start_utc)} — kết thúc "
                   f"{_fmt_dt(times.end_utc)} → {_window_status(times)}",
                   icon="⏱️")

    # ---------------------- 6. 🏴 Flag format ------------------------- #
    def _check_flag_format(self, report: DoctorReport, platform):
        if platform is None:
            report.add("Flag format", False,
                       "Bỏ qua — không có platform adapter", icon="🏴")
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
        if fmt:
            report.add("Flag format", True, f"Tìm thấy: {fmt}", icon="🏴")
        elif rules:
            report.add("Flag format", False,
                       "Có rules nhưng không suy ra format — nhập tay qua "
                       "--flag-format", icon="🏴")
        else:
            report.add("Flag format", False,
                       "Không tải được rules — nhập tay qua --flag-format",
                       icon="🏴")
