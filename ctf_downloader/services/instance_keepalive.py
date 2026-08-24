"""InstanceKeepAlive — state machine duy trì container instance (spec event-window §9).

8 state: ALIVE → DUE_SOON → RENEWING → ALIVE | RENEW_FAILED | GIVE_UP ;
DEAD → RESTARTING → ALIVE | RESTART_BACKOFF → GIVE_UP. Reconciliation
LEVEL-TRIGGERED kiểu K8s: mỗi tick quyết định lại toàn bộ từ observed mới.

Tham số bảng (spec "State machine & tham số"):
  POLL 30-60s · TCP probe 15s × 3 · RENEW_LEAD ≈60% cửa sổ (clamp ≤600s để
  tránh 400 của GZCTF RenewalWindow ~10') · SAFETY_MARGIN 90s · BOOT_WAIT
  20-30s · RESTART backoff 30→600 cap ±20% · MAX_RESTARTS=3 · whale mọi op
  cách ≥61s (request LỖI cũng reset đồng hồ).

Ràng buộc thực chiến BẮT BUỘC:
  R-A — whale POST/recreate ĐỔI FLAG ⇒ TUYỆT ĐỐI không auto-restart khi
        status.flag.value đã có; auto-mode dừng ở CRITICAL chờ user.
        GZCTF recreate GIỮ flag (FlagContext ghim DB) ⇒ auto-restart OK.
  R-B — 502/503/504 từ entry ≠ dead ⇒ chỉ tin status API chính thức hoặc
        TCP connect-fail ≥3 lần cách ≥15s, cross-check API trước khi kết luận.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from ..storage.workspace_repo import WorkspaceRepo
from ..utils.logger import Logger

# ---------------------------------------------------------------------- #
# Tham số state machine
# ---------------------------------------------------------------------- #
POLL_INTERVAL_MIN = 30          # giây — poll bình thường
POLL_INTERVAL_MAX = 60
POLL_INTERVAL_DUE = 5           # DUE_SOON / RENEW_FAILED poll nhanh hơn
TCP_PROBE_PERIOD = 15           # giây giữa 2 lần probe TCP
TCP_PROBE_THRESHOLD = 3         # số lần fail liên tiếp trước khi nghi DEAD
RENEW_LEAD_FRACTION = 0.6       # RENEW_LEAD ≈ 60% cửa sổ lifetime
RENEW_WINDOW_CAP = 600          # clamp ≤10' — GZCTF chỉ nhận extend trong
                                # RenewalWindow cuối, gọi sớm → 400
SAFETY_MARGIN = 90              # giây — ngừng retry renew khi còn ít hơn thế
RENEW_MAX_ATTEMPTS = 4
EXT_RETRY_MIN = 2               # full-jitter 2..30s cho retry renew
EXT_RETRY_MAX = 30
BOOT_WAIT_MIN = 20              # giây chờ container boot sau POST
BOOT_WAIT_MAX = 30
RESTART_COOLDOWN = 10           # DELETE → cooldown → POST
BOOT_HEALTH_CHECKS = 3          # M-2: tối đa 3 lần health-check sau boot
BOOT_HEALTH_RETRY = 10          # ...cách nhau 10s trước khi tính fail
RESTART_BACKOFF_BASE = 30
RESTART_BACKOFF_CAP = 600
MAX_RESTARTS = 3
WHALE_OP_GAP = 61               # whale frequency limit — request lỗi cũng reset
WHALE_MAX_RENEWS = 5            # docker_max_renew_count mặc định
ESCALATION_REPEAT = 300         # ERROR lặp lại tối đa mỗi 300s

# Cấp escalation — CRITICAL mute các cấp thấp hơn cùng instance
INFO, WARNING, ERROR, CRITICAL = "info", "warning", "error", "critical"

# Trạng thái
ALIVE, DUE_SOON, RENEWING, RENEW_FAILED, DEAD = (
    "ALIVE", "DUE_SOON", "RENEWING", "RENEW_FAILED", "DEAD")
RESTARTING, RESTART_BACKOFF, GIVE_UP = (
    "RESTARTING", "RESTART_BACKOFF", "GIVE_UP")


def _now() -> float:
    return time.monotonic()


def _parse_entry(entry: Any) -> Optional[Tuple[str, int]]:
    """'host:port' → (host, port); bỏ qua URL http(s)."""
    s = str(entry or "").strip()
    if not s or s.startswith("http"):
        return None
    m = re.match(r"^([\w.\-]+):(\d+)$", s)
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2))
    except ValueError:
        return None


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP connect probe. True = port chấp nhận kết nối."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class InstanceTracker:
    """Trạng thái keep-alive của MỘT container."""

    def __init__(self, challenge_id: Any, name: str,
                 meta_path: Optional[Any] = None,
                 platform_kind: str = "unknown"):
        self.challenge_id = challenge_id
        self.name = name
        self.meta_path = meta_path
        self.platform_kind = platform_kind   # gzctf | whale | unknown
        self.state = ALIVE
        self.remaining: Optional[float] = None
        self.entry: Optional[str] = None
        # Lifetime quan sát được lớn nhất (giây) — dùng tính RENEW_LEAD 60%
        self.est_lifetime: Optional[float] = None
        # whale: client-side renew counter (API không trả renew_count)
        self.renew_count = 0
        self.renew_attempts = 0
        self.restart_count = 0
        self.tcp_fail_count = 0
        self.last_tcp_probe_mono: Optional[float] = None
        self.last_op_mono: Optional[float] = None     # whale ≥61s spacing
        self.restart_phase: Optional[str] = None      # cooldown|boot_wait
        self.phase_deadline: Optional[float] = None
        self.health_checks = 0                        # M-2: đếm health-check
        self.backoff_deadline: Optional[float] = None
        self.last_escalation: Dict[str, float] = {}   # key -> mono
        self.critical_muted = False                   # CRITICAL mute cấp thấp
        self.blocked_flag_rotate = False              # R-A đang chờ user

    def next_poll_in(self) -> float:
        if self.state in (DUE_SOON, RENEW_FAILED):
            return POLL_INTERVAL_DUE
        if self.state in (RESTARTING, RESTART_BACKOFF):
            return 2.0
        return random.uniform(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX)

    # ------------------------------------------------------------------ #
    def escalate(self, level: str, message: str) -> Optional[Tuple[str, str]]:
        """Phát escalation với repeat-suppression (ERROR mỗi 300s) và
        CRITICAL mute các cấp thấp hơn. Trả (level, message) hoặc None."""
        if self.critical_muted and level != CRITICAL:
            return None
        now = _now()
        key = f"{level}:{message}"
        last = self.last_escalation.get(key)
        if level != CRITICAL and last is not None and now - last < ESCALATION_REPEAT:
            return None
        self.last_escalation[key] = now
        if level == CRITICAL:
            self.critical_muted = True
        return level, message


class InstanceKeepAlive:
    """Điều phối keep-alive mọi container trong workspace.

    ``svc`` duck-type InstanceService: cần ``platform`` (get_instance_status /
    extend_instance / start_instance / stop_instance), ``repo``
    (WorkspaceRepo) và ``list_containers()``.
    """

    def __init__(self, svc: Any, repo: Optional[WorkspaceRepo] = None,
                 assume_yes: bool = False, practice_mode: bool = False):
        self.svc = svc
        self.repo = repo or getattr(svc, "repo", None)
        self.assume_yes = assume_yes
        self.practice_mode = practice_mode
        self.trackers: Dict[Any, InstanceTracker] = {}

    # ------------------------------------------------------------------ #
    # Tracking registry
    # ------------------------------------------------------------------ #
    def discover_containers(self) -> List[InstanceTracker]:
        """Quét workspace → tracker cho mọi challenge container chưa có."""
        try:
            containers = self.svc.list_containers() or []
        except Exception:
            containers = []
        for meta in containers:
            cid = meta.get("id")
            if cid is None or cid in self.trackers:
                continue
            pname = type(self.svc.platform).__name__.lower()
            kind = ("gzctf" if "gzctf" in pname
                    else "whale" if "ctfd" in pname else "unknown")
            self.trackers[cid] = InstanceTracker(
                challenge_id=cid, name=str(meta.get("name", f"ID {cid}")),
                meta_path=meta.get("_local_path"), platform_kind=kind)
        return list(self.trackers.values())

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _remaining_seconds(status: dict, kind: str) -> Optional[float]:
        """Đếm ngược còn lại (giây) từ observed mới nhất; None nếu không biết.

        - whale: ``time_left`` (remaining_time) tính bằng GIÂY.
        - gzctf: ``close_time`` epoch-ms → trừ now.
        """
        if not isinstance(status, dict):
            return None
        tl = status.get("time_left", status.get("remaining_time"))
        close_ms = status.get("close_time")
        try:
            if tl is not None:
                return max(0.0, float(tl))
        except (TypeError, ValueError):
            pass
        try:
            if close_ms is not None and float(close_ms) > 0:
                return max(0.0, (float(close_ms) -
                                 _dt.datetime.now(_dt.timezone.utc).timestamp() * 1000) / 1000.0)
        except (TypeError, ValueError):
            pass
        return None

    def _flag_status(self, tracker: InstanceTracker) -> dict:
        """status.flag qua repo.read_status (R-A). Thiếu → flag coi như null."""
        if self.repo is None or not tracker.meta_path:
            return {"value": None, "state": "none"}
        try:
            st = self.repo.read_status(tracker.meta_path)
            flag = st.get("flag") or {}
            return {"value": flag.get("value"),
                    "state": flag.get("state", "none")}
        except Exception:
            return {"value": None, "state": "none"}

    @staticmethod
    def renew_threshold(lifetime: Optional[float]) -> float:
        """Ngưỡng vào DUE_SOON: min(60% lifetime quan sát được, 600s cap)."""
        lead = RENEW_WINDOW_CAP
        if lifetime and lifetime > 0:
            lead = min(RENEW_LEAD_FRACTION * lifetime, RENEW_WINDOW_CAP)
        return lead

    def _whale_gap_ok(self, tracker: InstanceTracker) -> bool:
        """Whale: mọi operation cách nhau ≥61s — request LỖI cũng reset đồng hồ
        (last_op được cập nhật ở cả nhánh thành công lẫn thất bại)."""
        if tracker.platform_kind == "gzctf":
            return True
        last = tracker.last_op_mono
        return last is None or (_now() - last) >= WHALE_OP_GAP

    def _mark_whale_op(self, tracker: InstanceTracker) -> None:
        if tracker.platform_kind != "gzctf":
            tracker.last_op_mono = _now()

    # ------------------------------------------------------------------ #
    # Tick chính — level-triggered reconciliation
    # ------------------------------------------------------------------ #
    def tick_all(self, window_active: bool = True) -> List[Tuple[str, str]]:
        """Chạy 1 vòng reconcile cho mọi tracker. Trả danh sách event
        (level, message) để UI/log hiển thị. ``window_active=False`` (hết
        window) → dừng auto-extend trừ khi practice_mode."""
        events: List[Tuple[str, str]] = []
        self.discover_containers()
        for tracker in list(self.trackers.values()):
            try:
                evs = self.tick_one(tracker, window_active=window_active)
                events.extend(evs)
            except Exception as exc:      # một instance lỗi không chết cả loop
                events.append((ERROR, f"keepalive {tracker.name}: {exc}"))
        return events

    def tick_one(self, tracker: InstanceTracker,
                 window_active: bool = True) -> List[Tuple[str, str]]:
        events: List[Tuple[str, str]] = []

        # Hết window: ngừng auto-extend (trừ practice mode)
        if not window_active and not self.practice_mode:
            return events

        # ---- Phases của restart đang dang dở --------------------------- #
        if tracker.state == RESTARTING:
            return self._tick_restarting(tracker)
        if tracker.state == RESTART_BACKOFF:
            if tracker.backoff_deadline is not None and _now() >= tracker.backoff_deadline:
                return self._begin_restart(tracker)
            return events

        # ---- Observe ---------------------------------------------------- #
        status = self._get_status_safe(tracker)
        api_status = str((status or {}).get("status") or "unknown")
        remaining = self._remaining_seconds(status, tracker.platform_kind)
        tracker.remaining = remaining
        entry = (status or {}).get("entry")
        if entry:
            tracker.entry = str(entry)

        # ---- DEAD determination (R-B) ----------------------------------- #
        dead_by_api = api_status in ("stopped", "destroyed", "exited", "removed")
        if dead_by_api or (remaining is not None and remaining <= 0
                           and api_status != "running"):
            self._sync_destroyed_local(tracker)
            tracker.state = DEAD
            events.append((WARNING, f"💀 {tracker.name}: container đã chết "
                                    f"(status={api_status})."))
            return self._maybe_restart(tracker, events)

        if api_status not in ("running",):
            # API lỗi/không rõ (vd 'unknown', HTTP 502/503/504 từ proxy) —
            # KHÔNG kết luận dead. Dùng TCP probe ×3 cách ≥15s + cross-check.
            if self._tcp_failed_enough(tracker):
                cross = self._get_status_safe(tracker)
                cross_status = str((cross or {}).get("status") or "unknown")
                if cross_status not in ("running",):
                    tracker.state = DEAD
                    events.append((WARNING, f"💀 {tracker.name}: TCP fail "
                                            f"×{TCP_PROBE_THRESHOLD} + status={cross_status}."))
                    return self._maybe_restart(tracker, events)
            ev = tracker.escalate(WARNING, f"⚠️ {tracker.name}: trạng thái tạm thời "
                                           f"không rõ ({api_status}) — không kết luận dead.")
            if ev:
                events.append(ev)
            tracker.state = ALIVE if tracker.state not in (GIVE_UP,) else GIVE_UP
            return events

        # ---- Running ----------------------------------------------------- #
        tracker.tcp_fail_count = 0
        # Circuit breaker OPEN (M-1): GIVE_UP sticky — không thao tác renew nữa
        if tracker.state == GIVE_UP:
            return events
        # Ngưỡng DUE_SOON: 60% lifetime quan sát được (neo sau renew/restart),
        # clamp ≤600s — chưa có mốc neo thì dùng cap 10' theo spec §9.
        threshold = self.renew_threshold(tracker.est_lifetime)
        if remaining is not None and remaining <= threshold:
            tracker.state = DUE_SOON
            return self._try_renew(tracker, events, remaining)
        tracker.state = ALIVE
        return events

    # ------------------------------------------------------------------ #
    def _get_status_safe(self, tracker: InstanceTracker) -> dict:
        try:
            return self.svc.platform.get_instance_status(tracker.challenge_id) or {}
        except Exception:
            return {"status": "unknown", "entry": None}

    def _tcp_failed_enough(self, tracker: InstanceTracker) -> bool:
        """TCP connect fail ≥3 lần liên tiếp cách nhau ≥15s."""
        parsed = _parse_entry(tracker.entry)
        now = _now()
        if (tracker.last_tcp_probe_mono is not None
                and now - tracker.last_tcp_probe_mono < TCP_PROBE_PERIOD):
            return tracker.tcp_fail_count >= TCP_PROBE_THRESHOLD
        tracker.last_tcp_probe_mono = now
        if parsed is None:
            # Không có entry để probe — không tăng count (R-B: chỉ tin API).
            return False
        host, port = parsed
        if tcp_probe(host, port):
            tracker.tcp_fail_count = 0
            return False
        tracker.tcp_fail_count += 1
        return tracker.tcp_fail_count >= TCP_PROBE_THRESHOLD

    # ------------------------------------------------------------------ #
    # Renew
    # ------------------------------------------------------------------ #
    def _try_renew(self, tracker: InstanceTracker,
                   events: List[Tuple[str, str]],
                   remaining: Optional[float]) -> List[Tuple[str, str]]:
        # Whale hết lượt renew → không gọi PATCH nữa (circuit breaker OPEN)
        if tracker.platform_kind != "gzctf":
            if tracker.renew_count >= WHALE_MAX_RENEWS:
                tracker.state = GIVE_UP
                # M-1: message key CỐ ĐỊNH (không nhúng remaining) để
                # escalation suppress chống spam mỗi tick
                msg = (f"📢 {tracker.name}: container sắp chết — "
                       f"không extend được nữa (hết {WHALE_MAX_RENEWS} lượt renew).")
                ev = tracker.escalate(CRITICAL, msg)
                if ev:
                    events.append(ev)
                return events
            if tracker.renew_count == WHALE_MAX_RENEWS - 1:
                ev = tracker.escalate(WARNING, f"🔴 {tracker.name}: còn 1 lượt "
                                               f"renew duy nhất.")
                if ev:
                    events.append(ev)

        if tracker.state == RENEW_FAILED and tracker.renew_attempts >= RENEW_MAX_ATTEMPTS:
            tracker.state = GIVE_UP
            ev = tracker.escalate(ERROR, f"❌ {tracker.name}: renew thất bại "
                                         f"{RENEW_MAX_ATTEMPTS} lần — bỏ cuộc.")
            if ev:
                events.append(ev)
            return events

        if remaining is not None and remaining <= SAFETY_MARGIN \
                and tracker.state == RENEW_FAILED:
            # Quá sát mực chết — ngừng retry, báo ERROR
            tracker.state = GIVE_UP
            ev = tracker.escalate(ERROR, f"⏱️ {tracker.name}: còn "
                                         f"{int(remaining)}s — quá muộn để retry renew.")
            if ev:
                events.append(ev)
            return events

        if not self._whale_gap_ok(tracker):
            return events   # postpone — tick sau sẽ thử lại

        tracker.state = RENEWING
        self._mark_whale_op(tracker)
        try:
            success, msg = self.svc.platform.extend_instance(tracker.challenge_id)
        except Exception as exc:
            success, msg = False, str(exc)

        if success:
            tracker.renew_attempts = 0
            tracker.tcp_fail_count = 0
            tracker.state = ALIVE
            if tracker.platform_kind != "gzctf":
                tracker.renew_count += 1
            # Neo lại lifetime sau renew (remaining mới ≈ cửa sổ đầy đủ)
            fresh = self._get_status_safe(tracker)
            fresh_remaining = self._remaining_seconds(fresh, tracker.platform_kind)
            if fresh_remaining:
                tracker.est_lifetime = float(fresh_remaining)
                tracker.remaining = fresh_remaining
            events.append((INFO, f"🔄 Extended {tracker.name}."))
            return events

        # ---- Renew failure ------------------------------------------------
        self._mark_whale_op(tracker)   # request lỗi cũng reset đồng hồ whale
        fatal = self._is_fatal_renew_error(msg)
        if fatal:
            if tracker.platform_kind != "gzctf":
                tracker.renew_count = WHALE_MAX_RENEWS   # circuit breaker OPEN
            tracker.state = GIVE_UP
            ev = tracker.escalate(CRITICAL, f"📢 {tracker.name}: renew bị từ chối "
                                            f"cố định ({msg}) — dừng auto-renew.")
            if ev:
                events.append(ev)
            return events

        tracker.renew_attempts += 1
        tracker.state = RENEW_FAILED
        delay = random.uniform(EXT_RETRY_MIN, EXT_RETRY_MAX)   # full-jitter
        ev = tracker.escalate(WARNING, f"⚠️ {tracker.name}: renew lỗi "
                                       f"({str(msg)[:80]}) — retry sau {delay:.0f}s "
                                       f"({tracker.renew_attempts}/{RENEW_MAX_ATTEMPTS}).")
        if ev:
            events.append(ev)
        return events

    @staticmethod
    def _is_fatal_renew_error(msg: Any) -> bool:
        s = str(msg or "").lower()
        return ("403" in s or "forbidden" in s
                or "not created" in s or "notcreated" in s)

    # ------------------------------------------------------------------ #
    # Restart (phương sách cuối — phải qua R-A nếu đổi flag)
    # ------------------------------------------------------------------ #
    def restart_rotates_flag(self, tracker: InstanceTracker) -> bool:
        """True nếu recreate container có thể ĐỔI FLAG (R-A).

        - gzctf: FlagContext ghim trong DB → recreate GIỮ flag → False.
        - whale/platform khác: POST tạo row mới → ĐỔI flag → True (an toàn).
        """
        return tracker.platform_kind != "gzctf"

    def _maybe_restart(self, tracker: InstanceTracker,
                       events: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        if not self.restart_rotates_flag(tracker):
            # GZCTF: recreate giữ flag — auto-restart an toàn
            return self._begin_restart(tracker)

        # R-A: whale / platform không rõ — cấm auto-restart khi đã có flag
        flag = self._flag_status(tracker)
        if flag.get("value") or flag.get("state", "none") != "none":
            tracker.blocked_flag_rotate = True
            tracker.state = DEAD
            ev = tracker.escalate(CRITICAL,
                                  f"📢 CRITICAL — {tracker.name}: container đã chết nhưng "
                                  f"bạn đang GIỮ flag (state={flag.get('state')}). "
                                  f"Restart sẽ ĐỔI FLAG — chạy lại lệnh start thủ công "
                                  f"để xác nhận.")
            if ev:
                events.append(ev)
            return events
        return self._begin_restart(tracker)

    def manual_restart_approved(self, tracker: InstanceTracker) -> Tuple[bool, str]:
        """Restart do user chủ động (--yes / chọn trong wizard) khi vi phạm R-A.

        Sau restart: flag → found_unverified + xoá value + note rotate.
        """
        flag = self._flag_status(tracker)
        had_flag = bool(flag.get("value")) or flag.get("state", "none") != "none"
        success, info = self.svc.platform.start_instance(tracker.challenge_id)
        if success and had_flag and self.repo is not None and tracker.meta_path:

            def _mut(st):
                st["flag"]["value"] = None
                st["flag"]["state"] = "found_unverified"
                st["notes"] = ((st.get("notes") or "") +
                               " 🔄 flag đã rotate do restart.").strip()
                return st

            try:
                self.repo.update_status(tracker.meta_path, _mut)
            except Exception:
                pass
        return bool(success), str((info or {}).get("message", ""))

    def interactive_restart(self, tracker: InstanceTracker,
                            assume_yes: bool = False) -> Tuple[bool, str]:
        """R-A nửa user-consent (F-2): cảnh báo restart sẽ ĐỔI FLAG, hỏi xác
        nhận (bỏ qua khi ``--yes``), rồi mới restart + rotate bookkeeping.

        Trả ``(success, message)``; message 'cancelled' nếu user từ chối.
        """
        flag = self._flag_status(tracker)
        if not assume_yes:
            held = flag.get("value") or f"(state={flag.get('state')})"
            try:
                print(f"⚠️  Restart sẽ ĐỔI FLAG của bài '{tracker.name}' — "
                      f"flag bạn đang giữ ({held}) sẽ hết hiệu lực.")
                answer = input("Tiếp tục restart? [y/N] ").strip().lower()
            except (EOFError, OSError):
                return False, "no-tty"
            if answer not in ("y", "yes"):
                return False, "cancelled"
        return self.manual_restart_approved(tracker)

    def _begin_restart(self, tracker: InstanceTracker) -> List[Tuple[str, str]]:
        if tracker.restart_count >= MAX_RESTARTS:
            tracker.state = GIVE_UP
            ev = tracker.escalate(CRITICAL, f"📢 CRITICAL — {tracker.name}: restart "
                                            f"{MAX_RESTARTS} lần vẫn chết. Chờ user can thiệp.")
            return ([ev] if ev else [])
        tracker.state = RESTARTING
        tracker.health_checks = 0   # M-2: reset bộ đếm health-check mỗi vòng
        tracker.restart_phase = "cooldown"
        tracker.phase_deadline = _now() + RESTART_COOLDOWN
        try:
            self.svc.platform.stop_instance(tracker.challenge_id)
        except Exception:
            pass
        return [(INFO, f"♻️ {tracker.name}: recreating container "
                       f"(restart {tracker.restart_count}/{MAX_RESTARTS})...")]

    def _tick_restarting(self, tracker: InstanceTracker) -> List[Tuple[str, str]]:
        now = _now()
        if tracker.phase_deadline is not None and now < tracker.phase_deadline:
            return []
        if tracker.restart_phase == "cooldown":
            # DELETE xong → POST create lại
            tracker.restart_phase = "boot_wait"
            tracker.phase_deadline = now + random.uniform(BOOT_WAIT_MIN, BOOT_WAIT_MAX)
            try:
                success, info = self.svc.platform.start_instance(tracker.challenge_id)
            except Exception:
                success = False
            if not success:
                return self._restart_failed(tracker)
            return []
        if tracker.restart_phase == "boot_wait":
            # Health check sau boot (M-2: cho phép tối đa 3 lần cách 10s
            # trước khi tính là fail — pod cần thời gian lên)
            status = self._get_status_safe(tracker)
            api_status = str((status or {}).get("status") or "unknown")
            entry = (status or {}).get("entry")
            if api_status == "running" and entry:
                tracker.state = ALIVE
                tracker.restart_phase = None
                tracker.tcp_fail_count = 0
                tracker.health_checks = 0
                fresh_remaining = self._remaining_seconds(status,
                                                          tracker.platform_kind)
                if fresh_remaining:
                    tracker.est_lifetime = float(fresh_remaining)   # neo lifetime
                    tracker.remaining = fresh_remaining
                self._sync_restart_local(tracker, entry,
                                         (status or {}).get("time_left"))
                return [(INFO, f"✅ {tracker.name}: container sống lại — "
                               f"entry mới [bold green]{entry}[/bold green].")]
            tracker.health_checks += 1
            if tracker.health_checks < BOOT_HEALTH_CHECKS:
                tracker.phase_deadline = _now() + BOOT_HEALTH_RETRY
                return []
            return self._restart_failed(tracker)
        return []

    def _restart_failed(self, tracker: InstanceTracker) -> List[Tuple[str, str]]:
        tracker.restart_count += 1
        tracker.restart_phase = None
        if tracker.restart_count >= MAX_RESTARTS:
            tracker.state = GIVE_UP
            ev = tracker.escalate(CRITICAL, f"📢 CRITICAL — {tracker.name}: restart "
                                            f"{MAX_RESTARTS} lần vẫn chết. Chờ user can thiệp.")
            return ([ev] if ev else [])
        delay = min(RESTART_BACKOFF_BASE * (2 ** (tracker.restart_count - 1)),
                    RESTART_BACKOFF_CAP) * random.uniform(0.8, 1.2)   # ±20%
        tracker.state = RESTART_BACKOFF
        tracker.backoff_deadline = _now() + delay
        ev = tracker.escalate(WARNING, f"⚠️ {tracker.name}: restart chưa lên — "
                                       f"backoff {delay:.0f}s.")
        return ([ev] if ev else [])

    # ------------------------------------------------------------------ #
    # Sync metadata local
    # ------------------------------------------------------------------ #
    def _sync_destroyed_local(self, tracker: InstanceTracker) -> None:
        """Container chết bất ngờ → cập nhật metadata.instance_info.status."""
        if self.svc is not None and hasattr(self.svc, "_update_local_instance_info"):
            try:
                self.svc._update_local_instance_info(
                    tracker.challenge_id, entry=None, time_left=0, status="stopped")
                return
            except Exception:
                pass
        self._sync_via_repo(tracker, status="stopped")

    def _sync_restart_local(self, tracker: InstanceTracker, entry: str,
                            time_left: Any) -> None:
        if self.svc is not None and hasattr(self.svc, "_update_local_instance_info"):
            try:
                self.svc._update_local_instance_info(
                    tracker.challenge_id, entry=entry,
                    time_left=time_left, status="running")
                return
            except Exception:
                pass
        self._sync_via_repo(tracker, status="running", entry=entry)

    def _sync_via_repo(self, tracker: InstanceTracker, status: str,
                       entry: Optional[str] = None) -> None:
        if self.repo is None:
            return
        now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _mutate(meta: dict) -> dict:
            meta = dict(meta) if isinstance(meta, dict) else {}
            if str(meta.get("id")) != str(tracker.challenge_id):
                # Không phải challenge đích: giữ nguyên (no-op).
                return meta
            inst = meta.get("instance_info")
            if not isinstance(inst, dict):
                inst = {}
            inst["status"] = status
            inst["last_updated"] = now_str
            if entry:
                inst["active_instance"] = entry
                inst["remaining_time"] = tracker.remaining
            elif status == "stopped":
                inst["active_instance"] = None
                inst["remaining_time"] = 0
            meta["instance_info"] = inst
            return meta

        for meta_path in self.repo.iter_challenges():
            try:
                # update_metadata: read-mutate-write dưới cùng lockfile flock
                # với update_status (tránh lost update đa tiến trình).
                updated = self.repo.update_metadata(meta_path, _mutate)
            except Exception:
                continue
            if str(updated.get("id")) == str(tracker.challenge_id):
                break


# Alias theo tên trong spec §9
WatchKeepAlive = InstanceKeepAlive
