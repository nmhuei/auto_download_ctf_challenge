"""
P2-6 Sniper first-blood — unit tests (unittest + mock, không network thật).

Mock toàn bộ thời gian qua FakeClock (patch ``ctf_downloader.services.sniper_service.time``)
và submit qua FakeSubmitter (bắt chước đúng hợp đồng của SubmitService: ghi
submit_history cho verdict correct/incorrect, KHÔNG ghi khi ratelimited).

Chạy: python3 -m unittest test_sniper.py -v
"""
import json
import os
import shutil
import tempfile
import time as _real_time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.services import sniper_service as sn_mod
from ctf_downloader.services.sniper_service import (
    MAX_ATTEMPTS_PER_TARGET,
    MAX_CONSECUTIVE_RATELIMITS,
    SniperService,
)


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class FakeClock:
    """Đồng hồ giả: time() trả giờ giả, sleep() tăng giờ giả."""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(max(0.0, float(seconds)))
        self.now += max(0.0, float(seconds))

    # run() dùng strftime/gmtime để format started_at — dùng bản thật
    strftime = staticmethod(_real_time.strftime)
    gmtime = staticmethod(_real_time.gmtime)


class FakeSubmitter:
    """Bắt chước SubmitService: script list các (verdict, message).

    Hợp đồng mô phỏng từ SubmitService.submit:
    - correct/incorrect  -> ghi entry vào submit_history.
    - ratelimited        -> KHÔNG ghi lịch sử (chỉ đặt platform.last_verdict).
    """

    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock
        self.calls = []          # [(challenge, flag, force)]
        self.call_times = []     # clock.time() tại lúc gọi
        self.platform = SimpleNamespace(last_verdict=None)
        self.submit_history = []

    def submit(self, challenge, flag, force=False):
        if not self.script:
            raise AssertionError("FakeSubmitter hết script — sniper bắn nhiều hơn kỳ vọng")
        verdict, message = self.script.pop(0)
        self.platform.last_verdict = verdict
        self.calls.append((challenge, flag, force))
        self.call_times.append(self.clock.time())
        if verdict in ("correct", "incorrect"):
            self.submit_history[:] = [
                e for e in self.submit_history if e.get("flag") != flag
            ]
            self.submit_history.append({"flag": flag, "result": verdict})
        return verdict in ("correct", "ratelimited"), message


# ----------------------------------------------------------------------
# Case chung
# ----------------------------------------------------------------------

class SniperBase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="sniper_ws_")
        self.clock = FakeClock()
        # Mặc định: window start sau 100s (tương lai so với đồng hồ giả)
        self.start_epoch = self.clock.now + 100

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_challenges(self, event_start=None):
        ctf_info = {"url": "http://ctf.test"}
        if event_start is not None:
            ctf_info["event_window"] = {"start": event_start}
        with open(os.path.join(self.ws, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "platform_url": "http://ctf.test",
                "ctf_info": ctf_info,
                "challenges": [
                    {"id": 1, "name": "Warmup", "category": "Misc"},
                    {"id": 2, "name": "Pwn Baby", "category": "Pwn"},
                ],
            }, f)

    def write_sniper(self, targets):
        with open(os.path.join(self.ws, "sniper.json"), "w", encoding="utf-8") as f:
            json.dump(targets, f)

    def make_service(self, script):
        self.submitter = FakeSubmitter(script, self.clock)
        return SniperService(WorkspaceRepo(self.ws), self.submitter)

    def run_sniper(self, service, **kwargs):
        with patch.object(sn_mod, "time", self.clock):
            return service.run(**kwargs)


# ----------------------------------------------------------------------
# load_targets
# ----------------------------------------------------------------------

class TestLoadTargets(SniperBase):
    def test_parse_sort_and_skip_invalid(self):
        self.write_challenges()
        self.write_sniper([
            {"challenge": "Pwn Baby", "flag": "FLAG{b}", "delay_seconds": 30},
            {"challenge": 1, "flag": "FLAG{a}"},                       # delay default 0
            {"flag": "FLAG{no-chall}"},                                 # thiếu challenge -> bỏ
            {"challenge": 2},                                           # thiếu flag -> bỏ
            "garbage",                                                  # sai kiểu -> bỏ
        ])
        svc = SniperService(WorkspaceRepo(self.ws), MagicMock())
        targets = svc.load_targets()
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]["challenge"], 1)      # delay 0 lên trước
        self.assertEqual(targets[0]["delay_seconds"], 0.0)
        self.assertEqual(targets[1]["delay_seconds"], 30.0)

    def test_dict_wrapper_and_missing_file_and_corrupt(self):
        self.write_challenges()
        svc = SniperService(WorkspaceRepo(self.ws), MagicMock())
        self.assertEqual(svc.load_targets(), [])          # thiếu file -> rỗng, không raise
        self.write_sniper({"targets": [{"challenge": 1, "flag": "FLAG{x}"}]})
        self.assertEqual(len(svc.load_targets()), 1)      # wrapper {'targets': [...]}
        with open(os.path.join(self.ws, "sniper.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(svc.load_targets(), [])          # JSON hỏng -> rỗng, không raise


# ----------------------------------------------------------------------
# resolve_start
# ----------------------------------------------------------------------

class TestResolveStart(SniperBase):
    def test_from_event_window(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = SniperService(WorkspaceRepo(self.ws), MagicMock())
        self.assertAlmostEqual(svc.resolve_start(), self.start_epoch, places=3)

    def test_missing_window_returns_none_and_override_raises_on_garbage(self):
        self.write_challenges(event_start=None)
        svc = SniperService(WorkspaceRepo(self.ws), MagicMock())
        self.assertIsNone(svc.resolve_start())
        with self.assertRaises(ValueError):
            svc.resolve_start("not-a-date")

    def test_iso_string_and_epoch_ms_accepted(self):
        self.write_challenges(event_start=None)
        svc = SniperService(WorkspaceRepo(self.ws), MagicMock())
        iso = _real_time.strftime("%Y-%m-%dT%H:%M:%S+00:00", _real_time.gmtime(self.start_epoch))
        self.assertAlmostEqual(svc.resolve_start(iso), self.start_epoch, places=3)
        self.assertAlmostEqual(
            svc.resolve_start(str(int(self.start_epoch * 1000))),  # epoch-ms
            self.start_epoch, places=2,
        )


# ----------------------------------------------------------------------
# run()
# ----------------------------------------------------------------------

class TestRunWaitForStart(SniperBase):
    def test_no_submit_before_window_opens(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = self.make_service([("correct", "ok")])
        self.write_sniper([{"challenge": 1, "flag": "FLAG{a}"}])
        summary = self.run_sniper(svc, poll_interval=10)
        # Có giai đoạn chờ (sleep > 0) trước phát bắn đầu tiên
        self.assertTrue(any(s > 0 for s in self.clock.sleeps))
        self.assertGreaterEqual(self.submitter.call_times[0], self.start_epoch)
        self.assertEqual(len(self.submitter.calls), 1)
        self.assertEqual(len(summary["solved"]), 1)
        self.assertEqual(summary["pending"], [])

    def test_missing_start_blocks_run(self):
        self.write_challenges(event_start=None)
        self.submitter = FakeSubmitter([], self.clock)
        svc = SniperService(WorkspaceRepo(self.ws), self.submitter)
        self.write_sniper([{"challenge": 1, "flag": "FLAG{a}"}])
        summary = self.run_sniper(svc, poll_interval=10)
        self.assertEqual(self.submitter.calls, [])
        self.assertEqual(summary["solved"], [])
        self.assertEqual(len(summary["pending"]), 1)   # target được trả lại nguyên vẹn
        self.assertEqual(self.clock.sleeps, [])        # chưa bắn gì cả

    def test_start_at_override_used_when_window_missing(self):
        self.write_challenges(event_start=None)
        svc = self.make_service([("correct", "ok")])
        self.write_sniper([{"challenge": 1, "flag": "FLAG{a}"}])
        summary = self.run_sniper(
            svc, poll_interval=10,
            start_at=_real_time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _real_time.gmtime(self.start_epoch)),
        )
        self.assertEqual(len(summary["solved"]), 1)
        self.assertGreaterEqual(self.submitter.call_times[0], self.start_epoch)


class TestFirstBloodPath(SniperBase):
    def test_first_blood_order_and_log(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = self.make_service([("correct", "accepted"), ("correct", "accepted")])
        self.write_sniper([
            {"challenge": "Pwn Baby", "flag": "FLAG{b}", "delay_seconds": 7},
            {"challenge": 1, "flag": "FLAG{a}", "delay_seconds": 0},
        ])
        with patch.object(sn_mod.Logger, "success") as mock_success:
            summary = self.run_sniper(svc, poll_interval=10)
        # Bắn theo thứ tự delay: FLAG{a} (0s) trước FLAG{b} (7s)
        self.assertEqual(
            [c[1] for c in self.submitter.calls],
            ["FLAG{a}", "FLAG{b}"],
        )
        self.assertEqual(len(summary["solved"]), 2)
        self.assertEqual(summary["pending"], [])
        first_bloods = [c.args[0] for c in mock_success.call_args_list
                        if "FIRST BLOOD" in str(c.args[0])]
        self.assertEqual(len(first_bloods), 2)


class TestIncorrectBlacklist(SniperBase):
    def test_wrong_flag_blacklisted_and_not_retried_by_default(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = self.make_service([("incorrect", "Incorrect")])
        self.write_sniper([{"challenge": 1, "flag": "FLAG{dead}"}])
        summary = self.run_sniper(svc, poll_interval=10)
        # Chỉ bắn ĐÚNG 1 lần — không retry khi không bật --retry-wrong
        self.assertEqual(len(self.submitter.calls), 1)
        self.assertFalse(self.submitter.calls[0][2])   # force=False
        # SubmitService (fake) đã ghi blacklist như thật
        self.assertEqual(self.submitter.submit_history,
                         [{"flag": "FLAG{dead}", "result": "incorrect"}])
        self.assertEqual(len(summary["failed"]), 1)
        self.assertEqual(summary["pending"], [])

    def test_retry_wrong_respects_attempt_cap_and_forces(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        script = [("incorrect", "Incorrect")] * MAX_ATTEMPTS_PER_TARGET
        svc = self.make_service(script)
        self.write_sniper([{"challenge": 1, "flag": "FLAG{dead}"}])
        summary = self.run_sniper(svc, poll_interval=10, retry_wrong=True)
        self.assertEqual(len(self.submitter.calls), MAX_ATTEMPTS_PER_TARGET)
        # Lần thử lại phải đi kèm force=True (vì flag đã bị blacklist)
        self.assertFalse(self.submitter.calls[0][2])
        self.assertTrue(all(c[2] for c in self.submitter.calls[1:]))
        self.assertEqual(len(summary["failed"]), 1)


class TestRateLimitedBackoff(SniperBase):
    def test_ratelimited_backs_off_then_succeeds_without_consuming_attempt(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = self.make_service([("ratelimited", "Slow down"), ("correct", "accepted")])
        self.write_sniper([{"challenge": 1, "flag": "FLAG{a}"}])
        summary = self.run_sniper(svc, poll_interval=10)
        self.assertEqual(len(self.submitter.calls), 2)
        # Backoff ≥ BACKOFF_BASE_SECONDS giữa hai phát bắn
        gap = self.submitter.call_times[1] - self.submitter.call_times[0]
        self.assertGreaterEqual(gap, sn_mod.BACKOFF_BASE_SECONDS)
        # Rate-limit không ghi lịch sử (không blacklist)
        self.assertEqual(self.submitter.submit_history,
                         [{"flag": "FLAG{a}", "result": "correct"}])
        self.assertEqual(len(summary["solved"]), 1)

    def test_persistent_ratelimit_eventually_stops(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))
        svc = self.make_service(
            [("ratelimited", "Slow down")] * (MAX_CONSECUTIVE_RATELIMITS + 5))
        self.write_sniper([{"challenge": 1, "flag": "FLAG{a}"}])
        summary = self.run_sniper(svc, poll_interval=10)
        self.assertEqual(len(self.submitter.calls), MAX_CONSECUTIVE_RATELIMITS)
        self.assertEqual(len(summary["pending"]), 1)   # không tiêu lượt thử
        self.assertEqual(summary["solved"], [])


class TestKeyboardInterrupt(SniperBase):
    def test_ctrl_c_midway_prints_remaining_state(self):
        self.write_challenges(event_start=str(int(self.start_epoch)))

        class InterruptingSubmitter(FakeSubmitter):
            def submit(self, challenge, flag, force=False):
                raise KeyboardInterrupt

        self.submitter = InterruptingSubmitter([("correct", "ok")], self.clock)
        svc = SniperService(WorkspaceRepo(self.ws), self.submitter)
        self.write_sniper([
            {"challenge": 1, "flag": "FLAG{a}"},
            {"challenge": "Pwn Baby", "flag": "FLAG{b}"},
        ])
        with patch.object(sn_mod.Logger, "print_table") as mock_table, \
             patch.object(sn_mod.Logger, "info"):
            summary = self.run_sniper(svc, poll_interval=10)
        self.assertTrue(summary["aborted"])
        self.assertEqual(len(summary["pending"]), 2)   # cả hai còn nguyên
        self.assertEqual(summary["solved"], [])
        # Trạng thái còn lại được in ra bảng
        self.assertTrue(mock_table.called)
        rows = str(mock_table.call_args.kwargs.get("rows"))
        self.assertIn("Pwn Baby", rows)


if __name__ == "__main__":
    unittest.main()
