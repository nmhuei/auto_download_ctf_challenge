"""WatchService — auto-sync dữ liệu giải trong event window (spec event-window §4-§6).

Thành phần:
  - PollScheduler: stdlib-only, dict task→deadline_monotonic, jitter ±20%,
    backoff ×2 cap 600s (429 tôn trọng Retry-After ở tầng caller).
  - WindowGuard: monotonic cho mọi sleep nội bộ; wall-clock chỉ so start/end;
    clock-skew phát hiện qua lệch Date header server.
  - WatchStateStore: .ctf/watch_state.json atomic + lockfile pid chống chạy đôi.
  - run_event_window_wizard: 3 câu hỏi ghi .ctf/config.json đúng 1 lần.
  - WatchService.run(): foreground rich Live (🩸 ✨ 💡 📢 ⏱️ 🔴), --once mode,
    SIGINT/SIGTERM sạch qua _shutdown().

Keep-alive instance là MỘT task trong scheduler (interval ~60s) — xem
services/instance_keepalive.py (state machine + R-A/R-B).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import signal
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..storage.fileio import atomic_write_json
from ..storage.workspace_repo import WorkspaceRepo
from ..utils.logger import Logger

try:
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.prompt import Confirm, Prompt
except Exception:      # pragma: no cover — rich luôn có trong requirements
    Live = Panel = Text = Confirm = Prompt = None

WATCH_STATE_VERSION = 1
CONFIG_VERSION = 1

DEFAULT_INTERVALS = {"notices": 15, "scoreboard": 60,
                     "challenges": 120, "keepalive": 60}
ADAPTIVE_SCOREBOARD_INTERVAL = 120   # tăng nếu 3 kỳ không đổi
SCOREBOARD_IDLE_ROUNDS = 3
CHALLENGE_BURST_INTERVAL = 25        # burst re-scan khi tổng số bài đổi
CHALLENGE_BURST_DURATION = 120       # ...trong 2 phút
BACKOFF_CAP = 600                    # backoff ×2 cap 600s
JITTER_FRACTION = 0.2                # ±20%
CLOCK_SKEW_WARN_SECONDS = 120
GRACE_DEFAULT = 300                  # wall > end+grace → final sync rồi exit


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def parse_time_arg(value: Optional[str]) -> Optional[_dt.datetime]:
    """--start/--end: ISO-8601 hoặc epoch giây/ms → datetime aware UTC."""
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.isdigit():
            num = float(s)
            return _dt.datetime.fromtimestamp(
                num if num < 1e11 else num / 1000.0, tz=_dt.timezone.utc)
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt
    except Exception:
        return None


def fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{s:02d}s" if h else f"{m:d}m{s:02d}s"


# ---------------------------------------------------------------------- #
# PollScheduler — stdlib-only, deadline monotonic
# ---------------------------------------------------------------------- #
class PollScheduler:
    def __init__(self, jitter: float = JITTER_FRACTION,
                 rng: Optional[Callable[[float, float], float]] = None):
        self.jitter = jitter
        self._rng = rng or random.uniform
        self._tasks: Dict[str, Dict[str, float]] = {}

    def register(self, task: str, interval: float, due_now: bool = True) -> None:
        self._tasks[task] = {"interval": max(1.0, float(interval)),
                             "mult": 1.0,
                             "deadline": 0.0 if due_now else self._deadline(interval)}
        if not due_now:
            self._tasks[task]["deadline"] = self._deadline(interval)

    def _deadline(self, interval: float) -> float:
        lo, hi = interval * (1 - self.jitter), interval * (1 + self.jitter)
        return time.monotonic() + self._rng(lo, hi)

    def _effective_interval(self, name: str) -> float:
        t = self._tasks[name]
        return min(t["interval"] * t["mult"], BACKOFF_CAP)

    def due(self, task: str, now: Optional[float] = None) -> bool:
        if task not in self._tasks:
            return False
        return (now if now is not None else time.monotonic()) \
            >= self._tasks[task]["deadline"]

    def due_tasks(self, now: Optional[float] = None) -> List[str]:
        return [t for t in self._tasks if self.due(t, now)]

    def postpone(self, task: str, interval: Optional[float] = None,
                 now: Optional[float] = None) -> float:
        """Hẹn kỳ tiếp theo (jitter ±20%); ``interval`` override base."""
        t = self._tasks[task]
        if interval is not None:
            t["interval"] = max(1.0, float(interval))
        eff = self._effective_interval(task)
        t["deadline"] = (now if now is not None else time.monotonic()) \
            + self._deadline(eff) - time.monotonic()
        return t["deadline"]

    def penalize(self, task: str) -> float:
        """Backoff ×2 cap 600s (tick lỗi). Trả effective interval mới."""
        t = self._tasks.get(task)
        if t is None:
            return BACKOFF_CAP
        t["mult"] = min(t["mult"] * 2, BACKOFF_CAP / max(1.0, t["interval"]))
        return self._effective_interval(task)

    def reward(self, task: str) -> None:
        """Tick thành công → reset multiplier."""
        t = self._tasks.get(task)
        if t is not None:
            t["mult"] = 1.0

    def set_interval(self, task: str, interval: float) -> None:
        t = self._tasks.get(task)
        if t is not None:
            t["interval"] = max(1.0, float(interval))

    def next_timeout(self, now: Optional[float] = None) -> float:
        """Số giây tới deadline sớm nhất (≥0.05) — dùng cho sleep monotonic."""
        if not self._tasks:
            return 1.0
        cur = now if now is not None else time.monotonic()
        soonest = min(t["deadline"] for t in self._tasks.values())
        return max(0.05, soonest - cur)


# ---------------------------------------------------------------------- #
# WindowGuard — window so bằng wall-clock, sleep nội bộ bằng monotonic
# ---------------------------------------------------------------------- #
class WindowGuard:
    BEFORE, LIVE, ENDED = "before", "live", "ended"

    def __init__(self, start_utc: Optional[_dt.datetime],
                 end_utc: Optional[_dt.datetime],
                 grace_seconds: int = GRACE_DEFAULT):
        self.start_utc = start_utc
        self.end_utc = end_utc
        self.grace_seconds = grace_seconds
        # Anchor: monotonic nội bộ để system-clock nhảy không phá sleep
        self._mono_anchor = time.monotonic()
        self._wall_anchor = time.time()

    def wall_now(self) -> float:
        return self._wall_anchor + (time.monotonic() - self._mono_anchor)

    @staticmethod
    def _ts(dt: Optional[_dt.datetime]) -> Optional[float]:
        return dt.timestamp() if dt is not None else None

    def state(self, now_wall: Optional[float] = None) -> str:
        w = self.wall_now() if now_wall is None else now_wall
        start, end = self._ts(self.start_utc), self._ts(self.end_utc)
        if start is not None and w < start:
            return self.BEFORE
        if end is not None and w > end + self.grace_seconds:
            return self.ENDED
        return self.LIVE

    def seconds_to_start(self, now_wall: Optional[float] = None) -> Optional[float]:
        start = self._ts(self.start_utc)
        if start is None:
            return None
        return start - (self.wall_now() if now_wall is None else now_wall)

    def seconds_to_end(self, now_wall: Optional[float] = None) -> Optional[float]:
        end = self._ts(self.end_utc)
        if end is None:
            return None
        return end - (self.wall_now() if now_wall is None else now_wall)

    @staticmethod
    def date_header_offset(date_header: Optional[str]) -> Optional[float]:
        """Lệch (giây) giữa Date header server và đồng hồ local — dương nghĩa
        server nhanh hơn. |offset| lớn → cảnh báo NTP/clock-skew."""
        if not date_header:
            return None
        try:
            from email.utils import parsedate_to_datetime
            server = parsedate_to_datetime(date_header)
            if server.tzinfo is None:
                server = server.replace(tzinfo=_dt.timezone.utc)
            return server.timestamp() - time.time()
        except Exception:
            return None


# ---------------------------------------------------------------------- #
# Config & state stores (.ctf/)
# ---------------------------------------------------------------------- #
class EventWindowConfigStore:
    """workspace/.ctf/config.json — user sở hữu, wizard ghi ĐÚNG 1 LẦN."""

    def __init__(self, workspace_root: str):
        self.path = os.path.join(workspace_root, ".ctf", "config.json")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def save(self, cfg: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, cfg)


def default_auto_sync_config(mode: str = "window",
                             intervals: Optional[dict] = None,
                             notices: bool = True,
                             scoreboard: bool = True,
                             challenge_rescan: bool = True,
                             grace_seconds: int = GRACE_DEFAULT,
                             auto_exit_on_end: bool = True) -> dict:
    mode = mode if mode in ("window", "always", "manual") else "window"
    return {
        "version": CONFIG_VERSION,
        "auto_sync": {
            "enabled": mode != "manual",
            "mode": mode,
            "policy": {
                "notices": notices,
                "scoreboard": scoreboard,
                "challenge_rescan": challenge_rescan,
            },
            "intervals_sec": {**DEFAULT_INTERVALS, **(intervals or {})},
            "grace_seconds": grace_seconds,
            "auto_exit_on_end": auto_exit_on_end,
        },
    }


class WatchStateStore:
    """workspace/.ctf/watch_state.json — runtime checkpoint atomic + lockfile."""

    def __init__(self, workspace_root: str):
        self.dir = os.path.join(workspace_root, ".ctf")
        self.path = os.path.join(self.dir, "watch_state.json")
        self.lock_path = self.path + ".lock"

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("version", WATCH_STATE_VERSION)
                data.setdefault("last_synced_at", {})
                data.setdefault("etag_cache", {})
                data.setdefault("seen_notice_ids", [])
                data.setdefault("backoff", {"multiplier": 1.0})
                return data
        except Exception:
            pass
        return {
            "version": WATCH_STATE_VERSION,
            "session_id": str(uuid.uuid4()),
            "window": {},
            "last_synced_at": {},
            "etag_cache": {},
            "seen_notice_ids": [],
            "backoff": {"multiplier": 1.0},
        }

    def save(self, state: dict) -> None:
        """Checkpoint per-type atomic — crash-safe."""
        os.makedirs(self.dir, exist_ok=True)
        atomic_write_json(self.path, state)

    def checkpoint_type(self, state: dict, sync_type: str) -> None:
        state.setdefault("last_synced_at", {})[sync_type] = \
            utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.save(state)

    # ---- lockfile pid chống chạy đôi ---------------------------------- #
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True     # tồn tại nhưng của user khác
        except OSError:
            return False

    def acquire_lock(self) -> bool:
        """True = chiếm được lock. Stale-pid → chiếm lại; live-pid → False."""
        os.makedirs(self.dir, exist_ok=True)
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    pid = int((f.read() or "0").strip() or 0)
            except Exception:
                pid = 0
            if pid != os.getpid() and self._pid_alive(pid):
                return False     # watch đang chạy
        tmp = self.lock_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        os.replace(tmp, self.lock_path)
        return True

    def release_lock(self) -> None:
        try:
            if os.path.exists(self.lock_path):
                os.unlink(self.lock_path)
        except OSError:
            pass


# ---------------------------------------------------------------------- #
# Wizard 3 câu hỏi (spec §4) — chạy ĐÚNG 1 LẦN sau pull thành công đầu tiên
# ---------------------------------------------------------------------- #
def run_event_window_wizard(workspace_root: str,
                            force_prompt: bool = False) -> Optional[dict]:
    """3 câu hỏi SyncPolicy → ghi .ctf/config.json. Trả config hoặc None.

    Không tty (pipe/CI/test) → KHÔNG prompt, trả None.
    """
    store = EventWindowConfigStore(workspace_root)
    if store.exists():
        return None
    if not force_prompt and not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    try:
        enabled = Confirm.ask(
            "⏱️  Tự động cập nhật trong lúc giải diễn ra?", default=True)
        if enabled:
            mode_raw = Prompt.ask(
                "Chế độ: (1) chỉ trong window giải (2) luôn cập nhật (3) thủ công",
                default="1")
            mode = {"1": "window", "2": "always", "3": "manual"}.get(
                str(mode_raw).strip(), "window")
        else:
            mode = "manual"
        notices = Confirm.ask("Nhận báo challenge mới/hint mới?",
                              default=True)
    except Exception:
        return None

    cfg = default_auto_sync_config(mode=mode, notices=notices,
                                   scoreboard=notices,
                                   challenge_rescan=enabled)
    try:
        store.save(cfg)
        Logger.success(f"💾 Đã lưu cấu hình auto-sync: {store.path}")
    except Exception as exc:
        Logger.warning(f"Không lưu được .ctf/config.json: {exc}")
    return cfg


# ---------------------------------------------------------------------- #
# Resolve event window (manual > platform > ctftime) + mirror challenges.json
# ---------------------------------------------------------------------- #
def resolve_event_window(platform: Any, repo: WorkspaceRepo,
                         title_hint: Optional[str] = None,
                         url_hint: Optional[str] = None,
                         interactive: bool = False,
                         ) -> tuple:
    """Trả (EventTimes|None, candidates|None).

    Thứ tự ưu tiên spec §2: manual > platform API > CTFtime. Chênh lệch
    >5 phút giữa nguồn → cảnh báo (platform server thắng).
    """
    from ..platforms.base import EventTimes
    from ..platforms.ctftime_resolver import CTFtimeResolver

    times: Optional[EventTimes] = None
    candidates = None

    fetcher = getattr(platform, "fetch_event_times", None)
    if callable(fetcher):
        try:
            times = fetcher()
        except Exception:
            times = None

    if times is None:
        title = title_hint or getattr(getattr(platform, "ctf_info", None),
                                      "title", "") or ""
        base_url = url_hint or getattr(platform, "base_url", "") or ""
        cached_id = ((repo.read_challenges().get("ctf_info") or {})
                     .get("ctftime_id"))
        resolver = CTFtimeResolver()
        try:
            if cached_id:
                event = resolver.get_event(cached_id)
                times = resolver.event_times_from(event) if event else None
            else:
                times, candidates = resolver.resolve_event_times(title, base_url)
                if times is not None:
                    try:
                        cid = int(times.source.split(":")[1])
                        repo.update_ctf_info(ctftime_id=cid)   # cache lần sau
                    except (IndexError, ValueError):
                        pass
        except Exception:
            times, candidates = None, None

    # Mirror vào challenges.json.ctf_info.event_window cho SUMMARY/dashboard
    if times is not None:
        try:
            iso = lambda dt: dt.isoformat() if dt else None
            repo.update_ctf_info(event_window={
                "start": iso(times.start_utc), "end": iso(times.end_utc),
                "source": times.source, "confidence": times.confidence})
        except Exception:
            pass
    return times, candidates


def maybe_run_event_window_wizard(output_dir: str, platform: Any = None) -> None:
    """Hook cuối PullService.run — chỉ prompt khi tty và chưa có config."""
    try:
        repo = WorkspaceRepo(output_dir)
        cfg = run_event_window_wizard(output_dir)
        times, candidates = resolve_event_window(
            platform, repo,
            title_hint=(repo.read_challenges().get("ctf_info") or {}).get("title"),
            url_hint=repo.resolve_platform_url())
        if times:
            icon = "⏱️"
            Logger.info(
                f"{icon} Event window: {times.start_utc:%Y-%m-%d %H:%M} → "
                f"{times.end_utc:%Y-%m-%d %H:%M} UTC "
                f"(nguồn {times.source}, confidence {times.confidence})")
            guard = WindowGuard(times.start_utc, times.end_utc)
            if cfg and (cfg.get("auto_sync") or {}).get("enabled", True) \
                    and guard.state() == WindowGuard.LIVE:
                Logger.info("👀 Giải đang diễn ra — chạy `ctf watch` để tự động cập nhật.")
        elif candidates:
            Logger.info("🔎 CTFtime có nhiều giải khớp tên — chạy `ctf watch` "
                        "để chọn thủ công.")
    except Exception:
        pass   # hook phụ sau pull — không bao giờ làm pull fail


# ---------------------------------------------------------------------- #
# WatchService
# ---------------------------------------------------------------------- #
class WatchService:
    """``ctf watch`` — vòng lặp auto-sync foreground."""

    def __init__(self, workspace_path: str, cookie: Optional[str] = None,
                 token: Optional[str] = None, once: bool = False,
                 no_scoreboard: bool = False,
                 start_utc=None, end_utc=None,
                 practice_mode: bool = False,
                 use_live_ui: Optional[bool] = None,
                 scheduler: Optional[PollScheduler] = None):
        self.workspace_path = os.path.abspath(workspace_path)
        self.cookie = cookie
        self.token = token
        self.once = once
        self.no_scoreboard = no_scoreboard
        self.manual_start = start_utc
        self.manual_end = end_utc
        self.practice_mode = practice_mode
        self.use_live_ui = use_live_ui
        self.scheduler = scheduler or PollScheduler()
        self.repo = WorkspaceRepo(self.workspace_path)
        self.state_store = WatchStateStore(self.workspace_path)
        self.cfg_store = EventWindowConfigStore(self.workspace_path)
        self.platform = None
        self.keepalive = None
        self.guard: Optional[WindowGuard] = None
        self.times = None
        self.state: Dict[str, Any] = {}
        self._stop = False
        self._exit_code = 0
        self._live = None
        self._feed: List[str] = []
        self._scoreboard_idle = 0
        self._burst_until_mono: Optional[float] = None
        self._known_chall_count: Optional[int] = None
        self._last_score: Optional[tuple] = None

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #
    def _setup_platform(self) -> None:
        from ..services.platform_resolver import PlatformResolver
        session, platform, _info = PlatformResolver.for_workspace(
            self.repo, cookie=self.cookie, token=self.token)
        self.platform = platform

    def _resolve_cfg(self) -> dict:
        cfg = self.cfg_store.load() or default_auto_sync_config()
        return cfg.get("auto_sync") or default_auto_sync_config()["auto_sync"]

    def _resolve_window(self, auto_cfg: dict) -> Optional[WindowGuard]:
        """Ưu tiên: --start/--end (manual HIGH) > platform > CTFtime."""
        start, end = self.manual_start, self.manual_end

        if start is None or end is None:
            times, _cands = resolve_event_window(self.platform, self.repo)
            self.times = times
            if times is not None:
                start = start or times.start_utc
                end = end or times.end_utc
        if start is None and end is None:
            return None
        grace = int(auto_cfg.get("grace_seconds", GRACE_DEFAULT))
        self.guard = WindowGuard(start, end, grace_seconds=grace)
        self.state["window"] = {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "source": getattr(self.times, "source", "manual"),
        }
        return self.guard

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        self._install_signal_handlers()

        auto_cfg = self._resolve_cfg()
        policy = auto_cfg.get("policy", {})
        intervals = {**DEFAULT_INTERVALS, **auto_cfg.get("intervals_sec", {})}

        if not self.state_store.acquire_lock():
            Logger.error("🔒 watch đang chạy trong process khác — thoát.")
            return 1
        self.state = self.state_store.load()

        try:
            self._setup_platform()
        except Exception as exc:
            Logger.error(f"Không khởi tạo được platform: {exc}")
            self.state_store.release_lock()
            return 1

        guard = self._resolve_window(auto_cfg)
        if guard is None:
            Logger.warning("⏱️ Không xác định được event window "
                           "(platform + CTFtime đều fail) — dùng `ctf watch "
                           "--start ... --end ...` hoặc wizard nhập tay.")
            if auto_cfg.get("mode") == "window":
                Logger.info("Chuyển sang chế độ luôn-cập-nhật cho phiên này.")

        # Scheduler tasks
        self.scheduler.register("keepalive", intervals.get("keepalive", 60))
        if not self.no_scoreboard and policy.get("scoreboard", True):
            self.scheduler.register("scoreboard", intervals.get("scoreboard", 60))
        if policy.get("challenge_rescan", True):
            self.scheduler.register("challenges", intervals.get("challenges", 120))
        if policy.get("notices", True):
            self.scheduler.register("notices", intervals.get("notices", 15))

        # Keep-alive integration (spec §9: tick là 1 task, interval 60s)
        try:
            from ..services.instance_service import InstanceService
            from .instance_keepalive import InstanceKeepAlive
            svc = InstanceService(self.workspace_path, cookie=self.cookie,
                                  token=self.token)
            self.keepalive = InstanceKeepAlive(
                svc, repo=self.svc_repo(svc), practice_mode=self.practice_mode)
        except Exception:
            self.keepalive = None

        self._open_live()
        try:
            while not self._stop:
                ran = self._run_round(auto_cfg)
                if self.once:
                    break
                self._sleep_until_next(guard)
            if guard is not None and guard.state() == WindowGuard.ENDED:
                self._final_sync()
        finally:
            self._shutdown()
        return self._exit_code

    @staticmethod
    def svc_repo(svc: Any):
        return getattr(svc, "repo", None)

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self._stop = True
            # SIGINT → exit 130, SIGTERM → 0 (spec §5)
            self._exit_code = 130 if signum == signal.SIGINT else 0
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):   # non-main thread / Windows
                pass

    # ------------------------------------------------------------------ #
    def _run_round(self, auto_cfg: dict) -> List[str]:
        lines: List[str] = []
        guard = self.guard
        window_active = True
        if guard is not None:
            st = guard.state()
            if st == WindowGuard.BEFORE:
                secs = guard.seconds_to_start() or 0
                lines.append(f"⏳ Giải bắt đầu sau {fmt_countdown(secs)} — pause.")
                self._checkpoint_all()
                return lines
            if st == WindowGuard.ENDED:
                secs_end = guard.seconds_to_end()
                lines.append(f"🔴 Giải đã kết thúc (grace hết "
                             f"{'%.0fs' % (-(secs_end)) if secs_end else ''}).")
                window_active = False
            else:
                secs = guard.seconds_to_end()
                if secs is not None:
                    lines.append(f"⏱️ Kết thúc sau {fmt_countdown(secs)}")

        for task in ("notices", "scoreboard", "challenges", "keepalive"):
            if not self.scheduler.due(task):
                continue
            handler = getattr(self, f"_tick_{task}", None)
            if handler is None:
                self.scheduler.postpone(task)
                continue
            try:
                out = handler(window_active=window_active)
                lines.extend(out or [])
                self.scheduler.reward(task)
                self.scheduler.postpone(task)
                self.state_store.checkpoint_type(self.state, task)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                interval = self.scheduler.penalize(task)
                lines.append(f"⚠️ task {task} lỗi: {str(exc)[:80]} "
                             f"— backoff {interval:.0f}s")
                self.scheduler.postpone(task)
        return lines

    def _sleep_until_next(self, guard: Optional[WindowGuard]) -> None:
        """Sleep monotonic tới deadline sớm nhất; wake-from-sleep → tick ngay
        (deadline đã quá ⇒ next_timeout ≈ 0)."""
        timeout = self.scheduler.next_timeout()
        if guard is not None and guard.state() == WindowGuard.BEFORE:
            to_start = guard.seconds_to_start() or 0
            timeout = min(timeout, max(0.05, to_start))   # đếm ngược từng nhịp
        deadline = time.monotonic() + timeout
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    def _final_sync(self) -> None:
        """wall > end+grace → final sync (scoreboard + rank cuối) rồi exit 0."""
        try:
            lines = self._tick_scoreboard(window_active=False, final=True)
            for ln in lines:
                Logger.info(ln)
        except Exception:
            pass
        self.state_store.save(self.state)
        auto_cfg = self._resolve_cfg()
        if not auto_cfg.get("auto_exit_on_end", True):
            Logger.info("👀 auto_exit_on_end=false — watch chuyển idle (Ctrl-C để thoát).")
            while not self._stop:
                time.sleep(0.5)
        Logger.success("🏁 Event đã kết thúc — watch exit.")

    def _checkpoint_all(self) -> None:
        self.state_store.save(self.state)

    # ------------------------------------------------------------------ #
    # Task: notices 📢 (best-effort, CTFd notifications API)
    # ------------------------------------------------------------------ #
    def _tick_notices(self, window_active: bool = True) -> List[str]:
        ptype = getattr(getattr(self.platform, "ctf_info", None), "platform_type", "")
        if ptype != "ctfd":
            return []     # platform khác: chưa có endpoint công khai ổn định
        resp = self.platform.session.get(
            f"{self.platform.base_url}/api/v1/notices", timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json() or {}
        if not data.get("success"):
            return []
        seen = set(self.state.setdefault("seen_notice_ids", []))
        lines = []
        for item in (data.get("data") or []):
            nid = str(item.get("id"))
            if nid in seen:
                continue
            seen.add(nid)
            title = item.get("title") or ""
            body = (item.get("body") or "")[:120]
            lines.append(f"📢 Thông báo mới: {title} — {body}")
        self.state["seen_notice_ids"] = sorted(seen)[-500:]
        return lines

    # ------------------------------------------------------------------ #
    # Task: scoreboard 🩸
    # ------------------------------------------------------------------ #
    def _tick_scoreboard(self, window_active: bool = True,
                         final: bool = False) -> List[str]:
        result = self.platform.fetch_scoreboard() or {}
        my_rank, my_score = result.get("my_rank"), result.get("my_score")
        total = result.get("total_teams") or len(result.get("standings") or [])

        cur = (str(my_rank), str(my_score))
        lines = []
        if self._last_score is not None and cur != self._last_score:
            old_rank, old_score = self._last_score
            lines.append(f"🩸 Rank thay đổi: {old_rank or '-'} ({old_score}) → "
                         f"{my_rank or '-'} ({my_score}) · tổng {total} teams")
        self._last_score = cur

        # Adaptive: 3 kỳ liên tiếp không đổi → nới interval lên 120s
        if cur == getattr(self, "_prev_score_same", None):
            self._scoreboard_idle += 1
        else:
            self._scoreboard_idle = 0
        self._prev_score_same = cur
        if self._scoreboard_idle >= SCOREBOARD_IDLE_ROUNDS:
            self.scheduler.set_interval("scoreboard", ADAPTIVE_SCOREBOARD_INTERVAL)

        # Mirror live rank vào SUMMARY.md
        if my_rank:
            try:
                self.repo.patch_summary_live_rank(
                    f"- **Live Rank**: {my_rank} · score {my_score}")
            except Exception:
                pass
        self.state["last_scoreboard"] = {"rank": my_rank, "score": my_score}
        return lines

    # ------------------------------------------------------------------ #
    # Task: challenges rescan ✨ / 💡 (+burst khi tổng số bài đổi)
    # ------------------------------------------------------------------ #
    def _tick_challenges(self, window_active: bool = True) -> List[str]:
        challs = self.platform.fetch_challenges() or []
        count = len(challs)
        lines = []
        if self._known_chall_count is not None and count != self._known_chall_count:
            delta = count - self._known_chall_count
            icon = "✨" if delta > 0 else "🗑️"
            lines.append(f"{icon} Tổng số challenge đổi: "
                         f"{self._known_chall_count} → {count}")
            if delta > 0:
                # Burst re-scan 20-30s trong 2 phút (spec §5)
                self._burst_until_mono = time.monotonic() + CHALLENGE_BURST_DURATION
                self.scheduler.set_interval("challenges", CHALLENGE_BURST_INTERVAL)
        self._known_chall_count = count
        if (self._burst_until_mono is not None
                and time.monotonic() > self._burst_until_mono):
            self._burst_until_mono = None
            base = {**DEFAULT_INTERVALS, **(self._resolve_cfg()
                                            .get("intervals_sec", {}))}
            self.scheduler.set_interval("challenges",
                                        base.get("challenges", 120))

        # Hint mới 💡: so hints per id với snapshot trong state
        prev_hints = self.state.setdefault("_hints", {})
        new_hints = {}
        for c in challs:
            cid = str(c.id)
            n = len(getattr(c, "hints", None) or [])
            new_hints[cid] = n
            old = prev_hints.get(cid)
            if old is not None and n > int(old or 0):
                lines.append(f"💡 Challenge {getattr(c, 'name', cid)} có hint mới!")
        self.state["_hints"] = new_hints
        return lines

    # ------------------------------------------------------------------ #
    # Task: keep-alive ♻️ (spec §9)
    # ------------------------------------------------------------------ #
    def _tick_keepalive(self, window_active: bool = True) -> List[str]:
        if self.keepalive is None:
            return []
        events = self.keepalive.tick_all(window_active=window_active)
        level_icon = {"info": "", "warning": "⚠️ ", "error": "❌ ",
                      "critical": "📢 "}
        return [f"{level_icon.get(lv, '')}{msg}" for lv, msg in events]

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _render_panel(self, lines: List[str]):
        if Panel is None or Text is None:
            return None
        head = "👀 ctf watch"
        if self.guard is not None:
            st = self.guard.state()
            icon = {"before": "⏳", "live": "🔴 LIVE", "ended": "✅ ended"}[st]
            head += f" · {icon}"
            if self.keepalive is not None:
                for tr in self.keepalive.trackers.values():
                    if tr.remaining is not None:
                        head += (f"\n🐳 {tr.name}: {tr.state} · còn "
                                 f"{fmt_countdown(tr.remaining)}")
                    if tr.platform_kind != "gzctf":
                        left = 5 - tr.renew_count if hasattr(tr, "renew_count") else "?"
                        if isinstance(left, int) and left <= 1:
                            head += " 🔴"
        body = Text("\n".join([head] + ([""] + self._feed[-8:] if self._feed else [])))
        return Panel(body, title="ctf watch", border_style="cyan")

    def _open_live(self) -> None:
        if self.use_live_ui is False or self.once or Live is None \
                or not sys.stdout.isatty():
            self._live = None
            return
        panel = self._render_panel([])
        try:
            self._live = Live(panel, refresh_per_second=2, console=None)
            self._live.__enter__()
        except Exception:
            self._live = None

    def _refresh_live(self, lines: List[str]) -> None:
        for ln in lines:
            self._feed.append(ln)
            Logger.info(ln) if self._live is None else None
        if self._live is not None and self._render_panel:
            try:
                self._live.update(self._render_panel(lines))
            except Exception:
                pass

    def _shutdown(self) -> None:
        """SIGINT/SIGTERM chung một lối: stop Live, flush state atomic, unlock."""
        try:
            if self._live is not None:
                self._live.__exit__(None, None, None)
                self._live = None
        except Exception:
            pass
        try:
            self.state_store.save(self.state)
        except Exception:
            pass
        try:
            self.state_store.release_lock()
        except Exception:
            pass
