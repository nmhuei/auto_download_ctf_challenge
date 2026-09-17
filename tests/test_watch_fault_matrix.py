"""P1/P2 watch fault-injection matrix.

Covers wall-clock suspend, scoreboard 304/no-change, auth expiry, 429
Retry-After, 5xx and transport failures.
"""

import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.services import watch_service as watch_mod
from ctf_downloader.services.watch_service import (
    PollScheduler,
    SCOREBOARD_IDLE_ROUNDS,
    WatchService,
    WindowGuard,
)


class ScriptedScoreboard:
    def __init__(self, payload):
        self.payload = payload
        self.received_etag = None

    def fetch_scoreboard(self, if_none_match=None):
        self.received_etag = if_none_match
        if isinstance(self.payload, Exception):
            raise self.payload
        return dict(self.payload)


def make_watch(payload):
    svc = WatchService.__new__(WatchService)
    svc.state = {"etag_cache": {}}
    svc.scheduler = PollScheduler(jitter=0, rng=lambda lo, hi: lo)
    svc.scheduler.register("scoreboard", 60, due_now=True)
    svc.platform = ScriptedScoreboard(payload)
    svc.repo = MagicMock()
    svc.guard = None
    svc._scoreboard_idle = 0
    svc._scoreboard_base_interval = None
    svc._last_score = ("2", "100")
    svc._prev_score_same = ("2", "100")
    svc._mini_sb_rows = [{"pos": 1, "name": "old", "score": 200}]
    svc._known_chall_count = None
    svc._burst_until_mono = None
    svc.keepalive = None
    svc._before_last_bucket = None
    return svc


class TestScoreboardWatchFaults(unittest.TestCase):
    def test_304_preserves_previous_snapshot_and_uses_etag(self):
        svc = make_watch({
            "_http_status": 304,
            "_not_modified": True,
            "_etag": '"v1"',
        })
        svc.state["etag_cache"]["scoreboard"] = '"v1"'
        old_rows = list(svc._mini_sb_rows)
        old_score = svc._last_score

        out = svc._tick_scoreboard()

        self.assertEqual(out, [])
        self.assertEqual(svc.platform.received_etag, '"v1"')
        self.assertEqual(svc._mini_sb_rows, old_rows)
        self.assertEqual(svc._last_score, old_score)
        self.assertEqual(svc._scoreboard_idle, 1)

    def test_repeated_304_enters_adaptive_idle_without_losing_base_interval(self):
        svc = make_watch({
            "_http_status": 304,
            "_not_modified": True,
            "_etag": '"v1"',
        })
        svc.state["etag_cache"]["scoreboard"] = '"v1"'
        for _ in range(SCOREBOARD_IDLE_ROUNDS):
            svc._tick_scoreboard()
        self.assertEqual(
            svc.scheduler._tasks["scoreboard"]["interval"],
            watch_mod.ADAPTIVE_SCOREBOARD_INTERVAL,
        )
        self.assertEqual(svc._scoreboard_base_interval, 60.0)

    def test_429_retry_after_is_one_shot_penalty(self):
        svc = make_watch({
            "_http_status": 429,
            "_retry_after": "90",
        })
        out = svc._tick_scoreboard()
        self.assertIn("429", out[0])
        self.assertEqual(svc.scheduler._tasks["scoreboard"]["penalty"], 90.0)

        # _run_round rewards the handler but the one-shot penalty must survive
        # until postpone consumes it.
        svc.scheduler._tasks["scoreboard"]["deadline"] = 0
        before = svc.scheduler._clock()
        lines = svc._run_round({})
        self.assertTrue(any("429" in line for line in lines))
        deadline = svc.scheduler._tasks["scoreboard"]["deadline"]
        self.assertGreaterEqual(deadline - before, 90.0)
        self.assertIsNone(svc.scheduler._tasks["scoreboard"]["penalty"])

    def test_429_http_date_retry_after_is_honored(self):
        import time
        from email.utils import formatdate

        retry_at = formatdate(time.time() + 120, usegmt=True)
        svc = make_watch({
            "_http_status": 429,
            "_retry_after": retry_at,
        })
        out = svc._tick_scoreboard()
        self.assertIn("Retry-After", out[0])
        penalty = svc.scheduler._tasks["scoreboard"]["penalty"]
        self.assertIsNotNone(penalty)
        self.assertGreater(penalty, 100.0)
        self.assertLessEqual(penalty, 120.0)

    def test_401_surfaces_auth_expiry_and_slows_poll(self):
        svc = make_watch({"_http_status": 401})
        out = svc._tick_scoreboard()
        self.assertTrue(any("xác thực" in line.lower() for line in out))
        self.assertTrue(svc.state["_scoreboard_auth_expired"])
        self.assertEqual(svc.scheduler._tasks["scoreboard"]["penalty"], 60.0)

    def test_public_scoreboard_can_continue_while_personal_auth_expired(self):
        svc = make_watch({
            "_http_status": 200,
            "_auth_status": 401,
            "my_rank": None,
            "my_score": None,
            "standings": [{"pos": 1, "name": "team", "score": 10}],
            "total_teams": 1,
        })
        out = svc._tick_scoreboard()
        self.assertTrue(any("cookie/token" in line.lower() for line in out))
        self.assertEqual(len(svc._mini_sb_rows), 1)

    def test_500_is_normal_task_failure_with_exponential_backoff(self):
        svc = make_watch({"_http_status": 500})
        lines = svc._run_round({})
        self.assertTrue(any("scoreboard" in line and "backoff" in line for line in lines))
        self.assertEqual(svc.scheduler._tasks["scoreboard"]["mult"], 2.0)

    def test_transport_error_is_normal_task_failure(self):
        svc = make_watch({"_error": "ConnectionError: reset"})
        lines = svc._run_round({})
        self.assertTrue(any("transport" in line for line in lines))
        self.assertEqual(svc.scheduler._tasks["scoreboard"]["mult"], 2.0)


class TestWallClockFaults(unittest.TestCase):
    def test_scheduler_deadline_becomes_due_after_suspend_jump(self):
        clock = [1_000.0]
        with patch.object(watch_mod.time, "time", lambda: clock[0]):
            sched = PollScheduler(jitter=0, rng=lambda lo, hi: lo)
            sched.register("scoreboard", 60, due_now=False)
            self.assertFalse(sched.due("scoreboard"))
            # Laptop sleeps for one hour: wall clock advances while process
            # executes nothing.
            clock[0] += 3600
            self.assertTrue(sched.due("scoreboard"))
            self.assertLessEqual(sched.next_timeout(), 0.05)

    def test_window_guard_backward_clock_step_does_not_freeze(self):
        start = dt.datetime.fromtimestamp(900, tz=dt.timezone.utc)
        end = dt.datetime.fromtimestamp(1200, tz=dt.timezone.utc)
        clock = [1000.0]
        with patch.object(watch_mod.time, "time", lambda: clock[0]):
            guard = WindowGuard(start, end, grace_seconds=0)
            self.assertEqual(guard.state(), WindowGuard.LIVE)
            # NTP/manual adjustment jumps wall time backward > warn threshold.
            clock[0] = 700.0
            first = guard.wall_now()
            # Clock then progresses normally and eventually crosses end.
            clock[0] = 1300.0
            later = guard.wall_now()
            self.assertGreater(later, first)
            self.assertEqual(guard.state(later), WindowGuard.ENDED)


if __name__ == "__main__":
    unittest.main()
