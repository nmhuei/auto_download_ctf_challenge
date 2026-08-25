"""Cleanup deferred c11-minors (watch + downloader):

1. ``WatchService._refresh_live`` được WIRE vào ``_main_loop`` — bằng chứng
   code: ``self._feed`` chỉ được ghi bởi ``_refresh_live`` và panel rich chỉ
   được rebuild ở đó (Live giữ nguyên object Panel dựng 1 lần trong
   ``_open_live``) → không có nguồn dữ liệu nào khác; không wire thì feed
   chết "(chưa có sự kiện)", đồng hồ/countdown đóng băng, và chế độ non-Live
   mất toàn bộ dòng sự kiện (``_run_round`` trả về bị vứt).
2. Feed bounded — wire làm feed sống lại nên phải có trần.
3. BEFORE-state: sleep sàn 1s (đếm ngược granularity giây, Live render ≤2fps)
   thay vì spin 50ms do deadline task overdue chưa được postpone khi pause.
4. ``http_downloader._TARGET_LOCKS`` bound qua WeakValueDictionary — entry
   tự xoá khi thread cuối bỏ strong ref; lock vẫn sống đủ lâu cho mọi thread
   đang giữ/chờ.
"""
from __future__ import annotations

import gc
import json
import os
import tempfile
import threading
import time as _time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import ctf_downloader.services.watch_service as wsm
from ctf_downloader.downloaders import http_downloader as hd
from ctf_downloader.services.watch_service import (
    WindowGuard,
    WatchService,
    default_auto_sync_config,
)


class _TempWsCase(unittest.TestCase):
    """Workspace rỗng đủ cho WatchService ctor (WorkspaceRepo chỉ cần path)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="watch_cleanup_")
        self.ws = self._tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


def _bare_svc(ws, once=True):
    svc = WatchService(str(ws), once=once, use_live_ui=False)
    # Bỏ qua clock-skew tick (cần platform thật) — coi như vừa check xong.
    svc._last_skew_check_mono = _time.monotonic()
    return svc


# ---------------------------------------------------------------------- #
# 1. _refresh_live wired vào _main_loop
# ---------------------------------------------------------------------- #
class TestRefreshLiveWiring(_TempWsCase):

    def test_main_loop_feeds_round_lines_to_refresh_live(self):
        svc = _bare_svc(self.ws)
        svc.scheduler.register("scoreboard", 60)
        svc._tick_scoreboard = lambda window_active=True: ["🩸 rank đổi"]
        refresh = MagicMock()
        svc._refresh_live = refresh

        svc._main_loop(None, default_auto_sync_config()["auto_sync"])

        refresh.assert_called_once()          # once=True → đúng 1 round
        lines = refresh.call_args[0][0]
        self.assertIsInstance(lines, list)
        self.assertIn("🩸 rank đổi", lines)

    def test_non_live_mode_logs_round_lines(self):
        """Non-Live (_live None): dòng sự kiện phải ra Logger.info — trước
        đây bị vứt vì không ai gọi _refresh_live."""
        svc = _bare_svc(self.ws)
        svc.scheduler.register("scoreboard", 60)
        svc._tick_scoreboard = lambda window_active=True: ["🩸 rank đổi"]
        with patch.object(wsm.Logger, "info") as log_info:
            svc._main_loop(None, default_auto_sync_config()["auto_sync"])
        logged = [str(c.args[0]) for c in log_info.call_args_list]
        self.assertTrue(any("🩸 rank đổi" in ln for ln in logged),
                        f"thiếu dòng sự kiện trong log: {logged}")


# ---------------------------------------------------------------------- #
# 2. Feed bounded
# ---------------------------------------------------------------------- #
class TestFeedBounded(_TempWsCase):

    def test_feed_capped_and_keeps_latest(self):
        svc = _bare_svc(self.ws)
        svc._live = None     # non-Live: mỗi dòng cũng đi qua Logger.info
        cap = wsm.FEED_MAX_LINES
        with patch.object(wsm.Logger, "info"):
            for i in range(cap + 300):
                svc._refresh_live([f"line {i}"])
        self.assertEqual(len(svc._feed), cap)
        self.assertIn(f"line {cap + 299}", svc._feed[-1])
        self.assertNotIn("line 0", svc._feed)


# ---------------------------------------------------------------------- #
# 3. BEFORE-state sleep floor 1s (không spin 50ms)
# ---------------------------------------------------------------------- #
class TestBeforeSleepFloor(_TempWsCase):

    def test_before_state_first_sleep_chunk_at_least_half_second(self):
        svc = _bare_svc(self.ws)
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        guard = WindowGuard(start, start + timedelta(hours=2))
        durations = []

        def fake_sleep(secs):
            durations.append(secs)
            svc._stop = True          # dừng sau chunk đầu tiên

        # Deadline scheduler đều quá khứ (mô phỏng pause trước window mở).
        svc.scheduler.register("notices", 15)
        svc.scheduler._tasks["notices"]["deadline"] = 0.0
        with patch.object(wsm.time, "sleep", side_effect=fake_sleep):
            svc._sleep_until_next(guard)
        self.assertTrue(durations)
        self.assertGreaterEqual(durations[0], 0.4,
                                "BEFORE phải ngủ theo nhịp ≥1s, không 50ms")


# ---------------------------------------------------------------------- #
# 4. http_downloader._TARGET_LOCKS bound (WeakValueDictionary)
# ---------------------------------------------------------------------- #
class TestTargetLocksBounded(unittest.TestCase):

    @staticmethod
    def _key(tag):
        return os.path.join(tempfile.mkdtemp(prefix=f"tlock_{tag}_"),
                            f"target_{tag}.bin")

    def test_lock_evicted_after_last_holder_drops_reference(self):
        key = self._key("a")
        lock, waited = hd.HttpDownloader._acquire_target_lock(key)
        self.assertFalse(waited)
        self.assertEqual(len(hd._TARGET_LOCKS), 1)
        lock.release()
        del lock                    # thread cuối bỏ strong ref
        gc.collect()
        self.assertEqual(len(hd._TARGET_LOCKS), 0)

    def test_waiter_shares_same_lock_and_not_stranded(self):
        """Thread chờ cùng đích dùng ĐÚNG lock cũ (không bị eviction protocol
        tạo lock thứ hai) và được đánh thức đúng bởi release của người giữ."""
        key = self._key("b")
        holder, w1 = hd.HttpDownloader._acquire_target_lock(key)
        self.assertFalse(w1)
        result = {}

        def worker():
            lk, waited = hd.HttpDownloader._acquire_target_lock(key)
            result["waited"] = waited
            lk.release()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=0.3)
        self.assertTrue(t.is_alive(), "worker phải đang chờ lock")
        self.assertEqual(len(hd._TARGET_LOCKS), 1)
        self.assertIs(hd._TARGET_LOCKS.get(key), holder)   # đúng lock cũ
        holder.release()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertTrue(result.get("waited"))
        del holder
        gc.collect()
        self.assertEqual(len(hd._TARGET_LOCKS), 0)


if __name__ == "__main__":
    unittest.main()
