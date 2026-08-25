"""WatchService — auto-sync dữ liệu giải trong event window (spec event-window §4-§6).

Thành phần:
  - PollScheduler: stdlib-only, dict task→deadline_monotonic, jitter ±20%,
    backoff ×2 cap 600s; 429 đi qua penalty ONE-SHOT (Retry-After hoặc
    backoff nội bộ) — sống qua reward, interval cơ sở bất biến.
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
import shutil
import signal
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from ..storage.fileio import atomic_write_json
from ..storage.workspace_repo import WorkspaceRepo
from .status_service import _METER_RAMP_3STOP
from ..ui.theme import (
    ACCENT, ACCENT_DEEP, ACCENT_HI,
    ERROR, FG_BASE, FG_FAINT, FG_MUTED, INFO, SOLVED, WARN,
    load_theme,
)
from ..ui.widgets import footer_bar, meter
from ..utils.logger import Logger

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.prompt import Confirm, Prompt
except Exception:      # pragma: no cover — rich luôn có trong requirements
    Live = Panel = Text = Group = Confirm = Prompt = Console = None

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
MAX_TRUSTED_SKEW_SECONDS = 21600     # R4: |offset| > 6h -> Date header bị coi
                                     # là giả/hỏng, KHÔNG hiệu chỉnh wall_now
GRACE_DEFAULT = 300                  # wall > end+grace → final sync rồi exit

# Mini-scoreboard dùng chung meter ramp amber 3 mốc (than hồng → hổ phách →
# vàng nhạt, PHOSPHOR FIELD KIT spec §3) đã chuẩn hoá ở
# ``status_service._METER_RAMP_3STOP`` — mỗi ô một màu, không nội suy.
MIN_PANEL_WIDTH = 40                 # dưới ngưỡng này ép width tối thiểu
DEGRADE_WIDTH = 80                   # width < 80 → bỏ mini-scoreboard
FEED_MAX_LINES = 200                 # trần feed 📢 — _refresh_live wire làm
                                     # feed sống lại, phải bound tránh phình

#: Console riêng cho panel (mẫu như status_service): mang theme PHOSPHOR
#: để các style token ``accent.deep`` / ``fg.faint`` / ``solved`` … resolve
#: đúng bên trong Live — Live(console=None) sẽ dùng console mặc định không
#: có theme và render mất màu.
_watch_console = (Console(theme=load_theme(None))
                  if Console is not None else None)


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
                             "rl_mult": 1.0,
                             "penalty": None,
                             "deadline": 0.0 if due_now else self._deadline(interval)}
        if not due_now:
            self._tasks[task]["deadline"] = self._deadline(interval)

    def _deadline(self, interval: float) -> float:
        lo, hi = interval * (1 - self.jitter), interval * (1 + self.jitter)
        return time.monotonic() + self._rng(lo, hi)

    def _effective_interval(self, name: str) -> float:
        t = self._tasks[name]
        # Cap chỉ chặn TĂNG vô hạn — không được cắt ngắn interval gốc đã
        # đăng ký (R3: interval > cap phải giữ nguyên, không bị kéo về cap).
        eff = min(t["interval"] * t["mult"], max(t["interval"], BACKOFF_CAP))
        # Penalty one-shot (rate-limit Retry-After / backoff 429): sàn
        # effective của kỳ postpone KẾ TIẾP — sống qua reward.
        return max(eff, float(t.get("penalty") or 0.0))

    def due(self, task: str, now: Optional[float] = None) -> bool:
        if task not in self._tasks:
            return False
        return (now if now is not None else time.monotonic()) \
            >= self._tasks[task]["deadline"]

    def due_tasks(self, now: Optional[float] = None) -> List[str]:
        return [t for t in self._tasks if self.due(t, now)]

    def postpone(self, task: str, interval: Optional[float] = None,
                 now: Optional[float] = None) -> float:
        """Hẹn kỳ tiếp theo (jitter ±20%); ``interval`` override base.

        Penalty one-shot (nếu có) áp cho ĐÚNG kỳ này rồi được tiêu —
        interval cơ sở không bao giờ bị đổi bởi penalty."""
        t = self._tasks[task]
        if interval is not None:
            t["interval"] = max(1.0, float(interval))
        eff = self._effective_interval(task)
        t["deadline"] = (now if now is not None else time.monotonic()) \
            + self._deadline(eff) - time.monotonic()
        t["penalty"] = None
        return t["deadline"]

    def penalize(self, task: str) -> float:
        """Backoff ×2 cap 600s (tick lỗi). Trả effective interval mới.
        Mult floor 1.0 (R3): interval > cap thì tick lỗi KHÔNG được làm
        task chạy sớm hơn lịch gốc."""
        t = self._tasks.get(task)
        if t is None:
            return BACKOFF_CAP
        t["mult"] = min(t["mult"] * 2,
                        max(1.0, BACKOFF_CAP / max(1.0, t["interval"])))
        return self._effective_interval(task)

    def reward(self, task: str) -> None:
        """Tick thành công → reset multiplier. KHÔNG xoá penalty one-shot
        (R1: backoff 429 phải sống qua reward của chính tick bị limit)."""
        t = self._tasks.get(task)
        if t is not None:
            t["mult"] = 1.0

    def set_interval(self, task: str, interval: float) -> None:
        t = self._tasks.get(task)
        if t is not None:
            t["interval"] = max(1.0, float(interval))

    def set_penalty(self, task: str, seconds: float) -> None:
        """Penalty ONE-SHOT cho kỳ postpone kế tiếp (R2): Retry-After của
        server là tạm thời — không đụng interval cơ sở, không bị ``reward``
        xoá, được tiêu ngay trong ``postpone`` kế tiếp."""
        t = self._tasks.get(task)
        if t is not None:
            t["penalty"] = max(1.0, min(float(seconds), BACKOFF_CAP))

    def rate_limit_backoff(self, task: str) -> float:
        """Backoff ×2 riêng cho rate-limit KHÔNG Retry-After (R1).

        Khác ``penalize`` ở chỗ mult lỗi thường bị ``reward`` reset ngay
        tick sau — ``rl_mult`` chỉ reset khi tick thực sự thành công
        (:meth:`clear_rate_limit`) nên giá trị trả về sống qua ít nhất
        1 chu kỳ khi dùng kèm :meth:`set_penalty`."""
        t = self._tasks.get(task)
        if t is None:
            return BACKOFF_CAP
        t["rl_mult"] = min((t.get("rl_mult") or 1.0) * 2,
                           max(1.0, BACKOFF_CAP / max(1.0, t["interval"])))
        return min(t["interval"] * t["rl_mult"],
                   max(t["interval"], BACKOFF_CAP))

    def clear_rate_limit(self, task: str) -> None:
        """Tick bình thường (không 429) → xoá streak backoff rate-limit."""
        t = self._tasks.get(task)
        if t is not None:
            t["rl_mult"] = 1.0

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
        self._server_offset = 0.0

    def wall_now(self) -> float:
        return (self._wall_anchor + (time.monotonic() - self._mono_anchor)
                + self._server_offset)

    def apply_server_offset(self, offset_seconds: float) -> None:
        """Hiệu chỉnh wall-clock theo lệch Date header server (F-3):
        offset > 0 nghĩa là server nhanh hơn local.

        R4: |offset| > MAX_TRUSTED_SKEW_SECONDS (6h) bị TỪ CHỐI — Date
        header lệch cực đại (năm 2099...) là giả/hỏng; tin mù quáng sẽ đẩy
        wall_now vài năm và kết thúc oan event window (ENDED + exit 0).
        """
        offset_seconds = float(offset_seconds)
        if abs(offset_seconds) > MAX_TRUSTED_SKEW_SECONDS:
            Logger.warning(
                f"🕐 Bỏ qua hiệu chỉnh clock-skew {offset_seconds:+.0f}s — "
                f"lệch vượt ngưỡng tin cậy "
                f"({MAX_TRUSTED_SKEW_SECONDS // 3600}h), nghi ngờ Date "
                f"header giả/lỗi mạng.")
            return
        self._server_offset = offset_seconds

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


def resolve_auto_sync_enabled(ws_cfg: Optional[dict],
                              global_cfg: Optional[dict]) -> bool:
    """Precedence hai tầng cho ``auto_sync.enabled`` (R6):

    - Global config (``ctf config auto-sync on/off``) = MẶC ĐỊNH.
    - ``.ctf/config.json`` của workspace = OVERRIDE — workspace thắng khi
      có key ``enabled`` bool.
    - Thiếu cả hai / dữ liệu lạ → mặc định BẬT (hành vi cũ).
    """
    for cfg in (ws_cfg, global_cfg):
        val = (cfg or {}).get("auto_sync") or {}
        if isinstance(val, dict) and isinstance(val.get("enabled"), bool):
            return val["enabled"]
    return True


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
        """True = chiếm được lock. Bước chiếm quyền là NGUYÊN TỬ qua
        ``os.open(O_CREAT|O_EXCL)`` (chống TOCTOU khi 2 process cùng lúc);
        EEXIST → đọc pid: live-pid → False, stale-pid → dọn rồi thử lại."""
        os.makedirs(self.dir, exist_ok=True)

        def _try_create() -> "int | None":
            try:
                return os.open(self.lock_path,
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return None

        fd = _try_create()
        if fd is None:
            pid = self._read_lock_pid()
            if pid == 0:
                # File vừa được process khác O_EXCL tạo nhưng CHƯA kịp ghi pid
                # → chờ ngắn rồi đọc lại trước khi kết luận stale (nếu không,
                # mình sẽ unlink lock của người vừa tạo — mất atomic hoàn toàn).
                for _ in range(10):
                    time.sleep(0.05)
                    pid = self._read_lock_pid()
                    if pid:
                        break
            if pid != os.getpid() and self._pid_alive(pid):
                return False     # watch đang chạy
            # stale / của chính process này → chiếm lại (1 lần thử nữa;
            # nếu vẫn thua tức là process khác vừa giành được — thua sạch sẽ)
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass
            fd = _try_create()
            if fd is None:
                return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True

    def _read_lock_pid(self) -> int:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return int((f.read() or "0").strip() or 0)
        except Exception:
            return 0

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
SOURCE_CONFLICT_SECONDS = 300   # spec §2: chênh >5 phút → cảnh báo


def warn_source_conflict(platform_times, ctftime_times) -> List[str]:
    """So hai nguồn thời gian; lệch >5 phút ở start/end nào → trả (và log)
    cảnh báo rõ cả hai giá trị. Platform server luôn thắng (spec §2)."""
    if platform_times is None or ctftime_times is None:
        return []
    messages = []
    for label, a, b in (("start", platform_times.start_utc,
                         ctftime_times.start_utc),
                        ("end", platform_times.end_utc,
                         ctftime_times.end_utc)):
        if a is None or b is None:
            continue
        delta = abs((a - b).total_seconds())
        if delta > SOURCE_CONFLICT_SECONDS:
            msg = (f"⚠️ Nguồn thời gian lệch nhau {delta / 60:.0f} phút "
                   f"(>{SOURCE_CONFLICT_SECONDS // 60}') ở '{label}': "
                   f"{platform_times.source}="
                   f"{a:%Y-%m-%d %H:%M}UTC vs {ctftime_times.source}="
                   f"{b:%Y-%m-%d %H:%M}UTC — ưu tiên {platform_times.source}.")
            messages.append(msg)
    for msg in messages:
        Logger.warning(msg)
    return messages


def resolve_event_window(platform: Any, repo: WorkspaceRepo,
                         title_hint: Optional[str] = None,
                         url_hint: Optional[str] = None,
                         interactive: bool = False,
                         resolver: Any = None,
                         ) -> tuple:
    """Trả (EventTimes|None, candidates|None).

    Thứ tự ưu tiên spec §2: manual > platform API > CTFtime. Có cả hai
    nguồn → so sánh, chênh >5 phút → cảnh báo + dùng nguồn cao hơn.
    """
    from ..platforms.base import EventTimes
    from ..platforms.ctftime_resolver import CTFtimeResolver

    times: Optional[EventTimes] = None
    candidates = None
    ctftime_times: Optional[EventTimes] = None

    fetcher = getattr(platform, "fetch_event_times", None)
    if callable(fetcher):
        try:
            times = fetcher()
        except Exception:
            times = None

    # Nguồn CTFtime — vẫn lấy cả khi platform đã có (để đối chiếu xung đột)
    try:
        r = resolver or CTFtimeResolver()
        title = title_hint or getattr(getattr(platform, "ctf_info", None),
                                      "title", "") or ""
        base_url = url_hint or getattr(platform, "base_url", "") or ""
        cached_id = ((repo.read_challenges().get("ctf_info") or {})
                     .get("ctftime_id"))
        if cached_id:
            event = r.get_event(cached_id)
            ctftime_times = r.event_times_from(event) if event else None
        elif title or base_url:
            ctftime_times, candidates = r.resolve_event_times(title, base_url)
        if ctftime_times is not None and ":" in (ctftime_times.source or ""):
            try:
                repo.update_ctf_info(
                    ctftime_id=int(ctftime_times.source.split(":")[1]))
            except (IndexError, ValueError):
                pass
    except Exception:
        ctftime_times, candidates = ctftime_times, candidates

    if times is None:
        times = ctftime_times
    else:
        warn_source_conflict(times, ctftime_times)   # F-4

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
        # CTFtime resolver inject được (test mock); None → tự tạo khi cần
        self.ctftime_resolver = None
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
        self._last_skew_check_mono: float = -10**9   # F-3 clock-skew active

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

    def _effective_auto_sync_enabled(self, auto_cfg: dict) -> bool:
        """Consumer ``auto_sync.enabled`` (R6): global = mặc định,
        workspace .ctf/config.json = override — xem
        :func:`resolve_auto_sync_enabled`."""
        try:
            from ..storage.global_config import load_global_config
            g_cfg = load_global_config()
        except Exception:
            g_cfg = None
        return resolve_auto_sync_enabled(self.cfg_store.load(), g_cfg)

    def _resolve_window(self, auto_cfg: dict) -> Optional[WindowGuard]:
        """Ưu tiên: --start/--end (manual HIGH) > platform > CTFtime."""
        start, end = self.manual_start, self.manual_end

        if start is None or end is None:
            times, _cands = resolve_event_window(
                self.platform, self.repo, resolver=self.ctftime_resolver)
            self.times = times
            if times is not None:
                start = start or times.start_utc
                end = end or times.end_utc
        if start is None and end is None:
            return None
        if start is not None and end is not None and start >= end:   # R6
            Logger.warning(
                f"⚠️ Event window bất thường: start ({start:%Y-%m-%d %H:%M} "
                f"UTC) không sớm hơn end ({end:%Y-%m-%d %H:%M} UTC) — kiểm "
                f"tra lại --start/--end hoặc dữ liệu giải; auto-sync sẽ "
                f"không bao giờ ở trạng thái LIVE.")
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
        # R6: gate auto_sync.enabled — global là mặc định, workspace
        # .ctf/config.json override. TẮT → watch không chạy (exit 0),
        # trước khi chiếm lock hay khởi tạo platform.
        if not self._effective_auto_sync_enabled(auto_cfg):
            Logger.warning(
                "⏸️ Auto-sync đang TẮT (`ctf config auto-sync off`, hoặc "
                ".ctf/config.json của workspace đặt enabled=false) — watch "
                "không chạy. Bật lại: `ctf config auto-sync on`.")
            return 0
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
            self._main_loop(guard, auto_cfg)
        finally:
            self._shutdown()
        return self._exit_code

    def _main_loop(self, guard: Optional[WindowGuard],
                   auto_cfg: dict) -> None:
        """Vòng lặp chính — THOÁT khi giải kết thúc (spec §5: wall >
        end+grace → final sync đúng 1 lần rồi exit 0; ``auto_exit_on_end=
        false`` → chuyển idle KHÔNG tick data nữa, chờ signal)."""
        ended_finalized = False
        while not self._stop:
            if guard is not None and guard.state() == WindowGuard.ENDED:
                if not ended_finalized:
                    ended_finalized = True
                    # Final sync (scoreboard/rank cuối); nếu auto_exit_on_end
                    # = false thì tự chuyển idle bên trong đến khi có signal.
                    self._final_sync()
                break   # exit 0 (hoặc 130 nếu signal đến giữa final/idle)
            self._clock_skew_tick()
            lines = self._run_round(auto_cfg)
            # Wire _refresh_live (deferred c11): đây là nguồn DUY NHẤT ghi
            # feed 📢 và rebuild panel rich (Live giữ nguyên object Panel
            # dựng 1 lần trong _open_live) — không gọi thì panel đóng băng
            # và chế độ non-Live mất toàn bộ dòng sự kiện của round.
            self._refresh_live(lines)
            if self.once:
                break
            self._sleep_until_next(guard)

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
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                interval = self.scheduler.penalize(task)
                lines.append(f"⚠️ task {task} lỗi: {str(exc)[:80]} "
                             f"— backoff {interval:.0f}s")
                self.scheduler.postpone(task)
                continue
            # C11-02: checkpoint nằm NGOÀI try của task — nó raise (vd
            # .ctf read-only) thì KHÔNG được penalize tick vừa thành công
            # hay postpone lần hai (mọi task backoff luỹ tiến tới cap dù
            # mạng khoẻ); đây là lỗi lưu state, không phải lỗi của task.
            try:
                self.state_store.checkpoint_type(self.state, task)
            except Exception as exc:
                lines.append(f"⚠️ checkpoint state {task} thất bại: "
                             f"{str(exc)[:80]} — sẽ thử lại kỳ sau.")
        return lines

    def _sleep_until_next(self, guard: Optional[WindowGuard]) -> None:
        """Sleep monotonic tới deadline sớm nhất; wake-from-sleep → tick ngay
        (deadline đã quá ⇒ next_timeout ≈ 0)."""
        timeout = self.scheduler.next_timeout()
        if guard is not None and guard.state() == WindowGuard.BEFORE:
            to_start = guard.seconds_to_start() or 0
            # Sàn nhịp BEFORE 1s: đếm ngược chỉ tới giây (fmt_countdown) và
            # Live render ≤2fps — deadline task overdue chưa được postpone
            # khi pause từng đẩy vòng lặp spin 50ms (đốt CPU + checkpoint
            # đĩa liên tục). Vẫn thức đúng lúc window mở (trễ ≤1s).
            timeout = max(1.0, min(timeout, max(0.05, to_start)))
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
    # Clock-skew chủ động (F-3): mỗi ~5 phút hỏi Date header server;
    # lệch > CLOCK_SKEW_WARN_SECONDS → cảnh báo NTP + hiệu chỉnh wall_now.
    # ------------------------------------------------------------------ #
    def _clock_skew_tick(self) -> Optional[float]:
        now_mono = time.monotonic()
        if now_mono - self._last_skew_check_mono < 300:   # ~5 phút/lần
            return None
        self._last_skew_check_mono = now_mono
        offset = None
        try:
            resp = self.platform.session.get(self.platform.base_url, timeout=10)
            offset = WindowGuard.date_header_offset(
                (getattr(resp, "headers", None) or {}).get("Date"))
        except Exception:
            return None
        if offset is not None and abs(offset) > CLOCK_SKEW_WARN_SECONDS:
            Logger.warning(f"🕐 Đồng hồ hệ thống lệch {offset:+.0f}s so với "
                           f"server — kiểm tra NTP/clock sync.")
        # R4: ngưỡng tin cậy do WindowGuard.apply_server_offset tự thi hành
        # (offset điên bị từ chối + cảnh báo tại đó).
        if offset is not None and self.guard is not None:
            self.guard.apply_server_offset(offset)
        return offset

    # ------------------------------------------------------------------ #
    # Task: notices 📢 (best-effort, CTFd notifications API)
    # ------------------------------------------------------------------ #
    def _tick_notices(self, window_active: bool = True) -> List[str]:
        ptype = getattr(getattr(self.platform, "ctf_info", None), "platform_type", "")
        if ptype != "ctfd":
            return []     # platform khác: chưa có endpoint công khai ổn định
        # Spec §5: ETag/304 cache per endpoint — gửi lại If-None-Match đã lưu
        url = f"{self.platform.base_url}/api/v1/notices"
        etag_cache = self.state.setdefault("etag_cache", {})
        req_headers = {}
        saved_etag = etag_cache.get("notices")
        if saved_etag:
            req_headers["If-None-Match"] = saved_etag
        resp = self.platform.session.get(url, timeout=10,
                                         headers=req_headers)
        resp_headers = getattr(resp, "headers", None) or {}
        if resp.status_code == 304:
            # C11-01: 304 vẫn là tick BÌNH THƯỜNG (endpoint khoẻ, không
            # đổi) — xoá streak rl_mult ngay tại đây; nếu thoát sớm thì
            # endpoint đứng im mãi sẽ giữ streak cũ và 429 kế tiếp (dù rất
            # sau) bị tính backoff luỹ tích ×8 thay vì bắt đầu lại ×2.
            self.scheduler.clear_rate_limit("notices")
            return []     # endpoint không đổi — dùng kết quả của kỳ trước
        if resp.status_code == 429:
            # Spec §5: tôn trọng Retry-After của server; thiếu header thì
            # backoff nội bộ ×2 lũy tiến. Cả hai đi qua penalty ONE-SHOT
            # (R1/R2): sống qua reward của tick này, không đổi interval
            # cơ sở — hết rate-limit là tự quay về lịch thường.
            ra = resp_headers.get("Retry-After")
            try:
                delay = max(1.0, float(ra))
                why = "theo Retry-After"
                self.scheduler.clear_rate_limit("notices")
            except (TypeError, ValueError):
                delay = self.scheduler.rate_limit_backoff("notices")
                why = "backoff ×2 nội bộ"
            self.scheduler.set_penalty("notices", delay)
            return [f"⏳ notices bị rate-limit (429) — lùi "
                    f"{delay:.0f}s ({why})."]
        # Tick bình thường (200/304/…): xoá streak backoff rate-limit
        self.scheduler.clear_rate_limit("notices")
        new_etag = resp_headers.get("ETag")
        if new_etag:
            etag_cache["notices"] = new_etag
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
        # UI-only stash cho mini-scoreboard (không đổi logic tick đã review)
        self._mini_sb_rows = list(result.get("standings") or [])[:8]
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

        # Solve attribution (spec challenge-status-model §4): watch tick gọi
        # fetch_solve_attribution qua CÙNG đường pull đang dùng
        # (PullService.sync_solve_attribution — chỉ-nâng + stamp synced_at),
        # để trạng thái by_team/by_other của team-mate cập nhật khi đang
        # `ctf watch` mà không cần pull lại. Tần suất = cadence task
        # ``challenges`` (120s mặc định, burst 25s khi có bài mới, backoff/
        # 429 do _run_round lo) — không lập task riêng gây spam API; ngoài
        # window (ENDED) bỏ qua.
        if window_active:
            synced = self._sync_solve_attribution()
            if synced:
                lines.append(f"🩸 Solve attribution đồng bộ: "
                             f"{synced} challenge(s) đổi trạng thái.")
        return lines

    def _sync_solve_attribution(self) -> Optional[int]:
        """Đồng bộ solve attribution bằng ĐÚNG helper pull đang dùng.

        Never-raise: platform không hỗ trợ ``fetch_solve_attribution`` → None
        (im lặng); mọi lỗi (mạng/repo) → log warning tiếng Việt và trả None —
        tick challenges (count/hint) vẫn tính là thành công, không backoff.
        """
        if not callable(getattr(self.platform, "fetch_solve_attribution", None)):
            return None
        from .pull_service import PullService
        try:
            updated = PullService.sync_solve_attribution(
                self.platform, self.workspace_path,
                on_error=lambda msg: Logger.warning(
                    f"⚠️ Đồng bộ solve attribution lỗi: {msg} — bỏ qua, "
                    f"chờ kỳ poll kế tiếp."))
        except Exception as exc:
            Logger.warning(f"⚠️ Đồng bộ solve attribution lỗi: {exc} — "
                           f"bỏ qua, chờ kỳ poll kế tiếp.")
            return None
        try:
            return int(updated or 0)
        except (TypeError, ValueError):
            return 0

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
    def _event_name(self) -> str:
        """Tên giải: challenges.json (nguồn chân lý workspace) trước, sau đó
        platform.ctf_info; fallback 'ctf watch'."""
        try:
            info = self.repo.read_challenges().get("ctf_info") or {}
            name = info.get("title")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except Exception:
            pass
        return "ctf watch"

    def _panel_snapshot(self) -> tuple:
        """Chữ ký dữ liệu panel cho skip-refresh (btop: không redraw khi nội
        dung không đổi). Giấy phép thay đổi duy nhất được tính là dữ liệu:
        giây wall-clock — đồng hồ/countdown phải tick từng giây."""
        return (
            int(time.time()),
            tuple(self._feed[-5:]),
            tuple((r.get("pos"), r.get("name"), r.get("score"))
                  for r in (getattr(self, "_mini_sb_rows", None) or [])[:5]),
            self._known_chall_count,
            tuple(
                (getattr(tr, "name", ""), getattr(tr, "state", ""),
                 getattr(tr, "remaining", None))
                for tr in (getattr(getattr(self, "keepalive", None),
                                   "trackers", {}) or {}).values()
            ),
            self.guard.state() if self.guard is not None else None,
        )

    def _render_header(self) -> "Text":
        """Header: tên giải + đồng hồ local + ⏱️ countdown + skew icon.

        Màu theo PHOSPHOR: tên bold fg.base, đồng hồ ``info`` (chỗ lạnh
        duy nhất), skew ``warn``; LIVE giữ semantic solved-green, ⏳ warn,
        ended muted."""
        head = Text()
        head.append(f"👀 {self._event_name()}", style=f"bold {FG_BASE}")
        now_local = _dt.datetime.now().astimezone()
        head.append(f"  ·  {now_local:%H:%M:%S}", style=INFO)
        if self.guard is not None:
            offset = float(getattr(self.guard, "_server_offset", 0.0) or 0.0)
            if abs(offset) >= 1:
                head.append(f"  🕐 lệch server {offset:+.0f}s", style=WARN)
            st = self.guard.state()
            icon, style = {
                WindowGuard.BEFORE: ("⏳", WARN),
                WindowGuard.LIVE: ("🔴 LIVE", f"bold {SOLVED}"),
                WindowGuard.ENDED: ("✅ ended", FG_MUTED),
            }[st]
            head.append("\n")
            head.append(icon, style=style)
            secs = (self.guard.seconds_to_start()
                    if st == WindowGuard.BEFORE
                    else self.guard.seconds_to_end())
            if secs is not None and st != WindowGuard.ENDED:
                label = ("bắt đầu sau" if st == WindowGuard.BEFORE
                         else "kết thúc sau")
                head.append(f"  ⏱️ {label} ", style=FG_MUTED)
                head.append(fmt_countdown(secs), style=style)
            # Keep-alive trackers (giữ nguyên thông tin như bản cũ)
            ka = getattr(self.keepalive, "trackers", None) or {}
            for tr in ka.values():
                remaining = getattr(tr, "remaining", None)
                if remaining is not None:
                    line = Text(style=FG_MUTED)
                    line.append(
                        f"\n🐳 {getattr(tr, 'name', '?')}: "
                        f"{getattr(tr, 'state', '?')} · còn ")
                    line.append(fmt_countdown(remaining))
                    head.append_text(line)
                if getattr(tr, "platform_kind", "") != "gzctf":
                    left = (5 - tr.renew_count
                            if hasattr(tr, "renew_count") else "?")
                    if isinstance(left, int) and left <= 1:
                        head.append(" 🔴", style=ERROR)
        return head

    def _render_notices(self, max_lines: int = 5) -> List[Any]:
        """📢 Khu vực sự kiện gần nhất (icon loại sự kiện nằm sẵn trong feed).

        Nhãn khu faint UPPERCASE; glyph loại sự kiện (🩸✨💡📢) trong feed
        giữ nguyên — màu ngữ nghĩa đi kèm từng dòng khi push vào feed."""
        parts: List[Any] = [Text("📢 SỰ KIỆN GẦN NHẤT", style=FG_FAINT)]
        feed_tail = self._feed[-max_lines:]
        block = Text()
        if feed_tail:
            for ln in feed_tail:
                if block.plain:
                    block.append("\n")
                block.append(ln)
        else:
            block.append("(chưa có sự kiện)", style=FG_FAINT)
        parts.append(block)
        return parts

    def _render_challenges_summary(self) -> Text:
        """✨ Tổng số bài + số bài mới phát hiện từ lần render trước."""
        line = Text("✨ CHALLENGES ", style=FG_FAINT)
        total = self._known_chall_count
        baseline = getattr(self, "_rendered_chall_baseline", None)
        if total is None:
            line.append("chưa quét", style=FG_FAINT)
        else:
            new_n = 0
            if baseline is not None:
                new_n = max(0, int(total) - int(baseline))
            self._rendered_chall_baseline = total
            line.append(str(total), style=f"bold {FG_BASE}")
            if new_n > 0:
                line.append(f"  ✨ +{new_n} bài mới tick này",
                            style=ACCENT_HI)
        return line

    def _render_mini_scoreboard(self) -> List[Any]:
        """🏆 Mini scoreboard top-5 — meter ramp amber 3 mốc
        (#6B4300 → #FFB000 → #FFE49A, ``_METER_RAMP_3STOP`` chung)."""
        rows = list(getattr(self, "_mini_sb_rows", None) or [])[:5]
        parts: List[Any] = []
        if not rows:
            parts.append(Text("🏆 Scoreboard: chưa có dữ liệu",
                              style=FG_FAINT))
            return parts
        try:
            top = max(float(r.get("score") or 0) for r in rows)
        except (TypeError, ValueError):
            top = 0.0
        colors = _METER_RAMP_3STOP
        meter_w = 16
        parts.append(Text("🏆 SCOREBOARD TOP-5", style=FG_FAINT))
        for r in rows:
            score = r.get("score")
            try:
                pct = (float(score) / top * 100.0) if top > 0 else 0.0
                score_txt = str(score)
            except (TypeError, ValueError):
                pct, score_txt = 0.0, str(score)
            row = Text()
            pos = r.get("pos", "?")
            name = str(r.get("name", "?"))[:20]
            row.append(f"{pos:>3}. ", style=FG_FAINT)
            row.append(f"{name:<20}", style=FG_BASE)
            row.append(f" {score_txt:>7} ", style=ACCENT_HI)
            row.append(meter(pct, meter_w, colors))
            parts.append(row)
        return parts

    def _render_panel(self, lines: List[str], width: Optional[int] = None):
        """Panel btop-layout: header clock/countdown + notices +
        mini-scoreboard + footer bar. Trả rich renderable cho Live —
        protocol giữ nguyên (caller vẫn nhận object, không phải string).

        Palette PHOSPHOR FIELD KIT: viền ``accent.deep``, tiêu đề bold
        ``accent``, nhãn khu faint UPPERCASE."""
        if Panel is None or Text is None:
            return None
        if width is None:
            try:
                width = shutil.get_terminal_size((100, 24)).columns
            except Exception:   # pragma: no cover — fallback hiếm gặp
                width = 100
        width = max(MIN_PANEL_WIDTH, int(width))

        body: List[Any] = [self._render_header(), Text()]
        body.extend(self._render_notices())
        body.append(Text())
        body.append(self._render_challenges_summary())
        if width >= DEGRADE_WIDTH:
            body.append(Text())
            body.extend(self._render_mini_scoreboard())

        # TUI luôn tương tác → footer chrome không phụ thuộc stdout pipe.
        footer = footer_bar([("q", "thoát"), ("p", "pause"),
                             ("r", "refresh-now")], width - 4, tty=True)
        if footer:
            body.append(Text())
            body.append(Text.from_markup(footer))

        title = Text()
        title.append("👀 ", style=ACCENT)
        title.append(self._event_name(), style=f"bold {ACCENT}")
        return Panel(Group(*body), title=title,
                     border_style=ACCENT_DEEP)

    def _open_live(self) -> None:
        if self.use_live_ui is False or self.once or Live is None \
                or not sys.stdout.isatty():
            self._live = None
            return
        panel = self._render_panel([])
        try:
            self._live = Live(panel, refresh_per_second=2,
                              console=_watch_console)
            self._live.__enter__()
        except Exception:
            self._live = None

    def _refresh_live(self, lines: List[str]) -> None:
        for ln in lines:
            self._feed.append(ln)
            Logger.info(ln) if self._live is None else None
        if len(self._feed) > FEED_MAX_LINES:
            del self._feed[:-FEED_MAX_LINES]   # bound — panel chỉ đọc đuôi
        if self._live is not None and self._render_panel:
            snap = self._panel_snapshot()
            if snap == getattr(self, "_last_panel_snap", None):
                return   # btop: dữ liệu & giây đồng hồ chưa đổi → bỏ redraw
            self._last_panel_snap = snap
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
