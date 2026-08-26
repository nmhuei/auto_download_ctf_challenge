"""Hunt-c17 — watch service: 6 finding từ vòng hunt thứ 17.

  F-1 [HIGH] Wall-clock anchor: suspend qua đêm làm CLOCK_MONOTONIC đứng
       yên → WindowGuard/PollScheduler kẹt pha, poll chết đói. Contract mới:
       deadline event window/poll neo time.time(); monotonic chỉ còn cho
       đo khoảng ngắn (anti-spin floor). Nhảy ngược lớn → cảnh báo, không kẹt.
  F-2 [MED]  Race chiếm lock stale: verify nội dung pid trước unlink.
  F-3 [MED]  Adaptive scoreboard ratchet một chiều → activity phải trả
       interval về mặc định.
  F-4 [LOW]  PID reuse: os.kill(pid,0) sống dù process gốc chết → đối chiếu
       /proc/<pid>/cmdline ('ctf'/'watch').
  F-5 [LOW]  _resolve_window nằm ngoài try → exception thoát không release_lock.
  F-6 [LOW]  Trong grace countdown clamp "0m00s" suốt 5' → hiển thị grace riêng.

Chạy: python3 -m pytest test_hunter_c17.py -q
Toàn bộ thời gian được patch ở nguồn (module time) — KHÔNG sleep dài.
"""
import datetime as _dt
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import ctf_downloader.services.watch_service as wsm
from ctf_downloader.services.watch_service import (
    ADAPTIVE_SCOREBOARD_INTERVAL,
    DEFAULT_INTERVALS,
    PollScheduler,
    WatchService,
    WatchStateStore,
    WindowGuard,
    default_auto_sync_config,
)


class _TempWsCase(unittest.TestCase):
    """Workspace rỗng đủ cho WatchService ctor (repo chỉ cần path)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunt_c17_")
        self.ws = self._tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


def _bare_svc(ws, once=True):
    return WatchService(str(ws), once=once, use_live_ui=False)


class _ScorePlatform:
    """fetch_scoreboard tối giản — đủ cho _tick_scoreboard."""

    def __init__(self):
        self.score = {"my_rank": "12th", "my_score": 900,
                      "total_teams": 100}

    def fetch_scoreboard(self):
        return dict(self.score)


def _render_plain(renderable, width=100):
    from rich.console import Console
    buf = io.StringIO()
    console = Console(width=width, file=buf, force_terminal=False,
                      legacy_windows=False)
    console.print(renderable)
    return buf.getvalue()


# ----------------------------------------------------------------------
# F-1 [HIGH] — wall-clock anchor qua suspend
# ----------------------------------------------------------------------

class TestC17WallClockAnchor(unittest.TestCase):
    """Suspend: monotonic đứng yên, wall vẫn trôi. Deadline window/poll là
    WALL-TIME nên guard/scheduler phải chuyển pha đúng sau "thức dậy"."""

    # Suspend trung thực: đóng băng monotonic NGAY tại giá trị hiện có,
    # chỉ wall trôi — không phải nhảy monotonic tới một mốc tuỳ ý.
    def test_guard_live_to_ended_after_suspend(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        g = WindowGuard(now - _dt.timedelta(minutes=5),   # đang LIVE
                        now + _dt.timedelta(hours=1))
        self.assertEqual(g.state(), WindowGuard.LIVE)
        mono0 = wsm.time.monotonic()
        real_wall = time.time()
        with patch.object(wsm.time, "monotonic", return_value=mono0), \
                patch.object(wsm.time, "time",
                             return_value=real_wall + 7 * 3600):
            # 7h trôi trên wall trong khi monotonic đứng nguyên (suspend)
            self.assertEqual(g.state(), WindowGuard.ENDED)
            self.assertLess(g.seconds_to_end(), -6 * 3600)

    def test_guard_before_to_live_after_suspend(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        g = WindowGuard(now + _dt.timedelta(minutes=30),
                        now + _dt.timedelta(hours=4))
        self.assertEqual(g.state(), WindowGuard.BEFORE)
        mono0 = wsm.time.monotonic()
        real_wall = time.time()
        with patch.object(wsm.time, "monotonic", return_value=mono0), \
                patch.object(wsm.time, "time",
                             return_value=real_wall + 3600):
            self.assertEqual(g.state(), WindowGuard.LIVE)
            # start còn 30' mà wall đã +1h → đã qua start 30'
            self.assertAlmostEqual(g.seconds_to_start(), -1800, delta=120)

    def test_scheduler_deadlines_survive_suspend(self):
        real_wall = time.time()
        sched = PollScheduler(rng=lambda lo, hi: (lo + hi) / 2)
        with patch.object(wsm.time, "time", return_value=real_wall), \
                patch.object(wsm.time, "monotonic", return_value=5000.0):
            sched.register("keepalive", 60)
            sched.postpone("keepalive")
            self.assertFalse(sched.due("keepalive"))
            # suspend 7h: monotonic đứng yên, wall tăng 7h → phải đến kỳ
            with patch.object(wsm.time, "time",
                              return_value=real_wall + 7 * 3600):
                self.assertTrue(sched.due("keepalive"))
                self.assertLessEqual(sched.next_timeout(), 0.05)

    def test_sleep_until_next_exits_when_wall_passes_deadline(self):
        """_sleep_until_next neo wall: wall vượt deadline giữa giấc ngủ
        (suspend) → vòng lập tức thoát thay vì ngủ tiếp theo monotonic."""
        ws = tempfile.mkdtemp(prefix="hunt_c17_sleep_")
        try:
            svc = _bare_svc(ws)
            svc.scheduler.register("scoreboard", 3600)
            clock = {"t": time.time()}
            slept = []

            def fake_time():
                return clock["t"]

            def fake_sleep(_secs):
                slept.append(1)
                if len(slept) > 5:
                    raise AssertionError(
                        "_sleep_until_next kẹt neo monotonic — wall đã vượt "
                        "deadline mà vòng vẫn tiếp tục ngủ")
                clock["t"] += 7 * 3600     # suspend ngay trong giấc ngủ đầu

            with patch.object(wsm.time, "time", side_effect=fake_time), \
                    patch.object(wsm.time, "sleep", side_effect=fake_sleep):
                svc._sleep_until_next(None)
            self.assertLessEqual(len(slept), 1)
        finally:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)

    def test_wall_backward_jump_warns_once_but_tracks_reality(self):
        """Nhảy ngược bất thường (user đổi giờ/NTP step): wall_now phải theo
        thực tế (không kẹt vĩnh viễn) và cảnh báo đúng MỘT lần."""
        now = _dt.datetime.now(_dt.timezone.utc)
        g = WindowGuard(now - _dt.timedelta(minutes=1),
                        now + _dt.timedelta(hours=2))
        clock = {"t": time.time()}
        with patch.object(wsm.time, "time",
                          side_effect=lambda: clock["t"]), \
                patch.object(wsm.Logger, "warning") as mwarn:
            g.wall_now()
            g.wall_now()
            clock["t"] -= 900                  # lùi 15 phút bất thường
            w = g.wall_now()
            self.assertAlmostEqual(w, clock["t"], delta=1,
                                   msg="wall_now phải theo wall thực, không "
                                       "kẹt giá trị cũ")
            self.assertEqual(mwarn.call_count, 1,
                             "nhảy ngược lớn phải cảnh báo (đúng 1 lần)")
            g.wall_now()
            self.assertEqual(mwarn.call_count, 1,
                             "không spam cảnh báo mỗi lần gọi")


# ----------------------------------------------------------------------
# F-2 [MED] — race chiếm lock stale: verify pid trước unlink
# ----------------------------------------------------------------------

class TestC17LockStealRace(_TempWsCase):
    def test_stale_steal_verifies_content_does_not_unlink_fresh_lock(self):
        store = WatchStateStore(str(self.ws))
        os.makedirs(store.dir, exist_ok=True)
        stale_pid = 4194300
        with open(store.lock_path, "w") as f:
            f.write(str(stale_pid))

        orig_read = WatchStateStore._read_lock_pid
        seen = {"n": 0}

        def spy_read(self_):
            val = orig_read(self_)
            seen["n"] += 1
            if seen["n"] == 1:
                # Giữa lúc ta đọc stale-pid và lúc ta unlink, "process B"
                # đã kịp unlink/create/ghi lock TƯƠI của nó.
                with open(store.lock_path, "w") as f:
                    f.write("987654")
            return val

        with patch.object(WatchStateStore, "_pid_alive",
                          return_value=False), \
                patch.object(WatchStateStore, "_read_lock_pid", spy_read):
            ok = store.acquire_lock()
        self.assertFalse(ok, "đã xoá lock TƯƠI của process khác rồi tự "
                             "chiếm — cả 2 cùng 'thành công'")
        with open(store.lock_path) as f:
            self.assertEqual(int(f.read()), 987654,
                             "lock tươi của process khác phải nguyên vẹn")


# ----------------------------------------------------------------------
# F-3 [MED] — adaptive scoreboard ratchet hai chiều
# ----------------------------------------------------------------------

class TestC17AdaptiveRatchet(_TempWsCase):
    def _svc(self):
        svc = _bare_svc(self.ws)
        svc.platform = _ScorePlatform()
        svc.state = svc.state_store.load()
        svc.scheduler.register("scoreboard",
                               DEFAULT_INTERVALS["scoreboard"])
        return svc, svc.platform

    def test_idle_widens_then_activity_restores_default(self):
        svc, plat = self._svc()
        svc._tick_scoreboard()                     # baseline
        for _ in range(3):
            svc._tick_scoreboard()                 # 3 kỳ không đổi → nới
        self.assertEqual(
            svc.scheduler._tasks["scoreboard"]["interval"],
            ADAPTIVE_SCOREBOARD_INTERVAL)
        plat.score["my_score"] = 12345             # activity quay lại!
        lines = svc._tick_scoreboard()
        self.assertEqual(
            svc.scheduler._tasks["scoreboard"]["interval"],
            DEFAULT_INTERVALS["scoreboard"],
            "activity quay lại mà interval không trả về mặc định — "
            "ratchet một chiều kẹt 120s mãi")
        self.assertTrue(any("🩸" in ln for ln in lines))
        # 1 kỳ yên tĩnh sau activity KHÔNG được nới lại vội
        svc._tick_scoreboard()
        self.assertEqual(
            svc.scheduler._tasks["scoreboard"]["interval"],
            DEFAULT_INTERVALS["scoreboard"])


# ----------------------------------------------------------------------
# F-4 [LOW] — PID reuse: đối chiếu cmdline
# ----------------------------------------------------------------------

def _wait_cmdline(pid: int) -> bool:
    for _ in range(100):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def _store_with_lock(ws: str, pid) -> WatchStateStore:
    store = WatchStateStore(ws)
    os.makedirs(store.dir, exist_ok=True)
    with open(store.lock_path, "w") as f:
        f.write(str(pid))
    return store


@unittest.skipIf(not os.path.isdir("/proc"), "cần /proc (Linux)")
class TestC17PidReuse(_TempWsCase):
    def test_unrelated_live_pid_is_treated_as_stale(self):
        p = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(15)"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(_wait_cmdline(p.pid))
            store = _store_with_lock(self.ws, p.pid)
            self.assertTrue(
                store.acquire_lock(),
                "pid sống nhưng cmdline không phải ctf/watch — phải coi là "
                "PID reuse và cho chiếm lock")
        finally:
            p.kill()
            p.wait()

    def test_watch_like_cmdline_pid_still_rejected(self):
        p = subprocess.Popen([sys.executable, "-c",
                              "# holder cho lock — ctf watch\n"
                              "import time; time.sleep(15)"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(_wait_cmdline(p.pid))
            store = _store_with_lock(self.ws, p.pid)
            self.assertFalse(
                store.acquire_lock(),
                "cmdline có dấu hiệu ctf/watch → vẫn là watch đang chạy")
        finally:
            p.kill()
            p.wait()


# ----------------------------------------------------------------------
# F-5 [LOW] — _resolve_window raise phải release_lock + exit 1
# ----------------------------------------------------------------------

class TestC17ResolveWindowFailure(_TempWsCase):
    def test_resolve_window_raise_releases_lock_and_returns_1(self):
        svc = _bare_svc(self.ws)
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(svc, "_effective_auto_sync_enabled",
                              return_value=True), \
                    patch.object(svc, "_setup_platform"), \
                    patch.object(svc, "_resolve_window",
                                 side_effect=RuntimeError("platform nổ")):
                rc = svc.run()
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        self.assertEqual(rc, 1)
        self.assertFalse(
            os.path.exists(svc.state_store.lock_path),
            "exception ở _resolve_window phải được bắt — lock không được "
            "bỏ rơi trên đĩa")


# ----------------------------------------------------------------------
# F-6 [LOW] — hiển thị grace riêng thay vì "kết thúc sau 0m00s"
# ----------------------------------------------------------------------

class TestC17GraceDisplay(_TempWsCase):
    def _svc_in_grace(self):
        svc = _bare_svc(self.ws)
        svc.platform = _ScorePlatform()
        svc.state = svc.state_store.load()
        now = _dt.datetime.now(_dt.timezone.utc)
        # end 2' trước, grace 300s → LIVE nhưng đang trong grace
        svc.guard = WindowGuard(now - _dt.timedelta(hours=2),
                                now - _dt.timedelta(minutes=2),
                                grace_seconds=300)
        return svc

    def test_run_round_feed_shows_grace_status_not_zero_countdown(self):
        svc = self._svc_in_grace()
        svc.scheduler.register("scoreboard", 60)
        lines = svc._run_round(default_auto_sync_config()["auto_sync"])
        joined = "\n".join(lines)
        self.assertNotIn("kết thúc sau 0m00s", joined)
        self.assertIn("grace", joined.lower())

    def test_header_render_grace_label_not_zero_countdown(self):
        svc = self._svc_in_grace()
        text = _render_plain(svc._render_panel([]))
        self.assertNotIn("kết thúc sau 0m00s", text)
        self.assertIn("grace", text.lower())


if __name__ == "__main__":
    unittest.main()
