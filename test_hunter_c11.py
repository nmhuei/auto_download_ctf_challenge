"""HUNTER CYCLE 11 — bấm biên các fix mới nhất sau khi land (mock network 100%).

Đối tượng:
  - e798f82 (R1/R2 watch backoff, R3 auth URL-key, R4 export-pack idempotent,
    R6 auto-sync precedence)
  - 314b9f0 (C9-04 khóa per-target .part)
  - 04bca51 (escape markup print_table) — tìm lỗ hổng NGOÀI bảng
  - 36305ad (cli_legacy _prompt_line EOF)

Quy ước: PASS = documentation (hành vi đúng/đã vá). Các bug C11-01..04
đã được vá — test tương ứng khẳng định hợp đồng SAU fix (trước đây từng
dùng ``@unittest.expectedFailure`` để tái hiện bug khi suite còn xanh).

Chạy: python3 test_hunter_c11.py
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rich.console import Console as _RichConsole

# ---------------------------------------------------------------------- #
# helpers dùng chung
# ---------------------------------------------------------------------- #
DET_RNG = lambda lo, hi: (lo + hi) / 2          # noqa: E731 — jitter trung tâm


def _resp(status, headers=None):
    """Fake CTFd response cho platform.session.get."""
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = {"success": True, "data": []}
    return r


def _mk_watch(ws, status=200, headers=None):
    """WatchService với platform mock, scheduler jitter=0 (deterministic)."""
    from ctf_downloader.services.watch_service import (
        PollScheduler, WatchService)

    platform = MagicMock()
    platform.ctf_info.platform_type = "ctfd"
    platform.base_url = "https://ctfd.example.com"
    platform.session.get = MagicMock(return_value=_resp(status, headers))
    svc = WatchService(str(ws), once=True, use_live_ui=False,
                       scheduler=PollScheduler(jitter=0.0, rng=DET_RNG))
    svc.platform = platform
    svc.state = svc.state_store.load()
    svc.scheduler.register("notices", 15)
    return svc


def _auto_cfg():
    from ctf_downloader.services.watch_service import default_auto_sync_config
    return default_auto_sync_config()["auto_sync"]


def _mk_ws(root: Path, challenges=("Alpha", "Beta"), marker="v1",
           with_solver=False):
    """Workspace tối thiểu cho test."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "challenges.json").write_text(json.dumps(
        {"ctf_info": {"title": "WSX", "url": "https://x.example"}}),
        encoding="utf-8")
    for name in challenges:
        d = root / "Web" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "writeup").mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({
            "id": abs(hash(name)) % 10000,
            "name": name,
            "category": "Web",
            "points": 100,
            "status": {
                "schema_version": 2,
                "solve": "solved_by_me",
                "flag": {"value": None, "state": "none"},
                "writeup": "complete",
                "writeup_auto": True,
            },
        }, ensure_ascii=False), encoding="utf-8")
        (d / "writeup" / "README.md").write_text(
            f"# {marker} {name} PTIT{{aaaa_bbbb}}", encoding="utf-8")
        if with_solver:
            solver = d / "solver"
            solver.mkdir(exist_ok=True)
            (solver / "solve.py").write_text("print('pwned')\n",
                                             encoding="utf-8")


class _FakeStdin:
    """stdin giả: hàng đợi dòng rồi EOF; đếm số lần readline."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.calls = 0

    def readline(self):
        self.calls += 1
        if self._lines:
            return self._lines.pop(0)
        return ""                      # EOF vĩnh viễn


# ====================================================================== #
# CASE 1 — Watch backoff (R1/R2): reward không xoá penalty; Retry-After
# one-shot; chuỗi lỗi→lỗi→OK→lỗi
# ====================================================================== #
class TestC11Case1WatchBackoff(unittest.TestCase):
    def test_1a_error_streak_ok_then_error(self):
        """lỗi→lỗi→OK→lỗi: backoff ×2×4, OK trả về base, lỗi mới bắt đầu
        lại từ ×2 (thiết kế: tick thành công reset streak lỗi thường)."""
        from ctf_downloader.services.watch_service import PollScheduler
        s = PollScheduler(jitter=0.0, rng=DET_RNG)
        s.register("t", 15)
        self.assertEqual(s.penalize("t"), 30.0)     # lỗi 1 → ×2
        s.postpone("t")
        self.assertEqual(s.penalize("t"), 60.0)     # lỗi 2 → ×4
        s.reward("t")                               # tick OK
        s.postpone("t")
        self.assertEqual(s._effective_interval("t"), 15.0)   # về base
        self.assertEqual(s.penalize("t"), 30.0)     # lỗi mới → ×2 (không kế thừa)

    def test_1b_penalty_survives_reward_consumed_once(self):
        """R1/R2 lõi: reward KHÔNG xoá penalty; postpone tiêu đúng 1 lần."""
        from ctf_downloader.services.watch_service import PollScheduler
        s = PollScheduler(jitter=0.0, rng=DET_RNG)
        s.register("notices", 15)
        s.set_penalty("notices", 90)
        s.reward("notices")                         # reward phải giữ penalty
        now = time.monotonic()
        dl = s.postpone("notices", now=now)
        self.assertAlmostEqual(dl - now, 90.0, delta=0.5)
        now2 = time.monotonic()                     # đã tiêu → về base
        dl2 = s.postpone("notices", now=now2)
        self.assertAlmostEqual(dl2 - now2, 15.0, delta=0.5)

    def test_1c_retry_after_returns_to_base_next_round(self):
        """429+Retry-After=90 → kỳ đó lùi 90s, base 15 bất biến; kỳ sau
        (server hết rate-limit, 200) quay về đúng nhịp ~15s."""
        ws = tempfile.mkdtemp()
        try:
            svc = _mk_watch(ws, 429, {"Retry-After": "90"})
            cfg = _auto_cfg()
            before = time.time()
            svc._run_round(cfg)
            t = svc.scheduler._tasks["notices"]
            self.assertEqual(t["interval"], 15)     # base bất biến (R2)
            self.assertAlmostEqual(t["deadline"] - before, 90.0, delta=1.0)
            # Server hồi phục → tick bình thường
            svc.platform.session.get.return_value = _resp(200)
            svc.scheduler._tasks["notices"]["deadline"] = 0.0
            before2 = time.time()
            svc._run_round(cfg)
            t2 = svc.scheduler._tasks["notices"]
            self.assertAlmostEqual(t2["deadline"] - before2, 15.0, delta=1.0,
                                   msg="hết Retry-After phải về đúng base 15s")
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_1d_bug_304_does_not_clear_rate_limit_streak(self):
        """BUG C11-01 (L): tick 304 thoát sớm TRƯỚC clear_rate_limit
        (watch_service.py:958-959) nên streak rl_mult không bao giờ được
        xoá khi endpoint đứng im (304 mãi mãi). 429 kế tiếp (dù rất sau)
        bị tính backoff luỹ tích cũ: nhảy thẳng ×8=120s thay vì ×2=30s.
        Hợp đồng docstring clear_rate_limit: 'tick bình thường → xoá'."""
        ws = tempfile.mkdtemp()
        try:
            svc = _mk_watch(ws, 429, {})
            cfg = _auto_cfg()
            svc._run_round(cfg)                     # 429 #1 → rl_mult=2
            svc.scheduler._tasks["notices"]["deadline"] = 0.0
            svc._run_round(cfg)                     # 429 #2 → rl_mult=4
            # Endpoint hồi phục nhưng không đổi → 304 (tick bình thường)
            svc.platform.session.get.return_value = _resp(304)
            svc.scheduler._tasks["notices"]["deadline"] = 0.0
            svc._run_round(cfg)
            self.assertEqual(svc.scheduler._tasks["notices"]["rl_mult"], 1.0,
                             "304 là tick bình thường — phải xoá streak")
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_1e_bug_checkpoint_failure_penalizes_successful_tick(self):
        """BUG C11-02 (M): checkpoint_type NẰM SAU reward/postpone trong
        _run_round (watch_service.py:874-876); nó raise thì rơi vào nhánh
        except của TASK → penalize một tick VỪA thành công + postpone lần
        thứ hai. .ctf không ghi được (vd read-only) → MỌI task backoff
        luỹ tiến tới cap 600s dù mạng hoàn toàn khoẻ, kèm dòng lỗi oan."""
        ws = tempfile.mkdtemp()
        try:
            svc = _mk_watch(ws, 200)
            cfg = _auto_cfg()
            svc.state_store.checkpoint_type = MagicMock(
                side_effect=OSError("read-only fs"))
            before = time.time()
            lines = svc._run_round(cfg)
            t = svc.scheduler._tasks["notices"]
            self.assertEqual(t["mult"], 1.0,
                             "tick thành công không được bị penalize")
            self.assertAlmostEqual(t["deadline"] - before, 15.0, delta=1.0,
                                   msg="tick OK phải giữ nhịp base")
            self.assertFalse(any("lỗi" in ln for ln in lines),
                             "checkpoint fail không phải lỗi của task")
        finally:
            shutil.rmtree(ws, ignore_errors=True)


# ====================================================================== #
# CASE 2 — Auth URL-key (R3): exact lookup, host match, trailing slash
# ====================================================================== #
class TestC11Case2AuthUrlKey(unittest.TestCase):
    def _patch_cfg(self, auth):
        from ctf_downloader.services import auth_service
        p = patch.object(auth_service, "load_global_config",
                         lambda: {"auth": auth})
        return p

    def _ws_with_url(self, url):
        ws = tempfile.mkdtemp()
        Path(ws, "challenges.json").write_text(json.dumps(
            {"ctf_info": {"url": url}}), encoding="utf-8")
        return ws

    def test_2a_host_mismatch_never_leaks(self):
        """Cookie platform A KHÔNG BAO GIỜ rơi sang workspace host B —
        cả khi workspace là dir thật lẫn URL string."""
        ws = self._ws_with_url("https://plat-b.example.net")
        try:
            with self._patch_cfg(
                    {"https://plat-a.example.com": {"cookie": "COOKIE_A"}}):
                from ctf_downloader.services.auth_service import AuthService
                cookie, _ = AuthService.resolve(ws)
                self.assertIsNone(cookie, "leak cookie chéo platform!")
                cookie2, _ = AuthService.resolve(
                    "https://plat-b.example.net/login")
                self.assertIsNone(cookie2)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_2b_trailing_slash_normalization_both_ways(self):
        """URL có/không '/' cuối phải khớp exact theo cả hai chiều lưu key."""
        for stored_key, ws_url in (
                ("https://plat.example.com/", "https://plat.example.com"),
                ("https://plat.example.com", "https://plat.example.com/")):
            ws = self._ws_with_url(ws_url)
            try:
                with self._patch_cfg({stored_key: {"cookie": "CK"}}):
                    from ctf_downloader.services.auth_service import AuthService
                    cookie, _ = AuthService.resolve(ws)
                    self.assertEqual(cookie, "CK",
                                     f"key={stored_key!r} url={ws_url!r}")
            finally:
                shutil.rmtree(ws, ignore_errors=True)

    def test_2c_same_pattern_different_platform_no_cross(self):
        """Hai platform cùng 'pattern' URL (khác host) — exact tra A không
        bao giờ trả cookie cho workspace B."""
        ws = self._ws_with_url("https://beta.events.example.com/e1")
        try:
            with self._patch_cfg({
                    "https://alpha.events.example.com/e1":
                        {"cookie": "ALPHA_CK"}}):
                from ctf_downloader.services.auth_service import AuthService
                cookie, _ = AuthService.resolve(ws)
                self.assertIsNone(cookie)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_2d_unique_host_fallback_scheme_variant(self):
        """Fallback DUY NHẤT cùng host (khác scheme) vẫn resolve — chủ đích
        R3; hai entry cùng host thì từ chối đoán (trả None)."""
        ws = self._ws_with_url("https://plat.example.com")
        try:
            with self._patch_cfg(
                    {"http://plat.example.com": {"cookie": "HTTP_CK"}}):
                from ctf_downloader.services.auth_service import AuthService
                cookie, _ = AuthService.resolve(ws)
                self.assertEqual(cookie, "HTTP_CK")
            # Hai entry cùng host → không đoán mò
            with self._patch_cfg({
                    "http://plat.example.com/e1": {"cookie": "C1"},
                    "http://plat.example.com/e2": {"cookie": "C2"}}):
                from ctf_downloader.services.auth_service import AuthService
                cookie2, _ = AuthService.resolve(ws)
                self.assertIsNone(cookie2)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_2e_abspath_key_still_wins_first(self):
        """Key abspath workspace (đăng ký truyền thống) ưu tiên trước mọi
        heuristic URL."""
        ws = self._ws_with_url("https://unrelated.example.com")
        try:
            auth = {os.path.abspath(ws): {"cookie": "LOCAL_CK"},
                    "https://unrelated.example.com": {"cookie": "URL_CK"}}
            with self._patch_cfg(auth):
                from ctf_downloader.services.auth_service import AuthService
                cookie, _ = AuthService.resolve(ws)
                self.assertEqual(cookie, "LOCAL_CK")
        finally:
            shutil.rmtree(ws, ignore_errors=True)


# ====================================================================== #
# CASE 3 — Export-pack re-run ×3 cùng ngày (R4 idempotent)
# ====================================================================== #
# CASE 4 — Auto-sync precedence hai tầng (R6) + giá trị rác
# ====================================================================== #
class TestC11Case4AutoSyncPrecedence(unittest.TestCase):
    def _resolve(self, ws_cfg, glob_cfg):
        from ctf_downloader.services.watch_service import (
            resolve_auto_sync_enabled)
        return resolve_auto_sync_enabled(ws_cfg, glob_cfg)

    def test_4a_matrix(self):
        self.assertTrue(self._resolve({"auto_sync": {"enabled": True}},
                                      {"auto_sync": {"enabled": False}}))
        self.assertFalse(self._resolve({"auto_sync": {"enabled": False}},
                                       {"auto_sync": {"enabled": True}}))
        self.assertTrue(self._resolve(None, None))          # mặc định BẬT
        self.assertTrue(self._resolve({}, {}))
        self.assertFalse(self._resolve({"auto_sync": {}},
                                       {"auto_sync": {"enabled": False}}))

    def test_4b_garbage_values_documented(self):
        """Giá trị rác: bool-mới-đúng (JSON true/false) mới được tính;
        'yes'/1/0/None/auto_sync-không-phải-dict đều bị coi là 'unset'
        và rơi xuống tầng kế (workspace → global → mặc định BẬT)."""
        self.assertFalse(self._resolve({"auto_sync": {"enabled": "yes"}},
                                       {"auto_sync": {"enabled": False}}))
        self.assertTrue(self._resolve({"auto_sync": {"enabled": 1}}, {}),
                                        msg="int 1 không phải bool → unset")
        self.assertTrue(self._resolve({"auto_sync": {"enabled": None}}, {}))
        self.assertTrue(self._resolve({"auto_sync": "junk"}, {}))
        self.assertTrue(self._resolve({"auto_sync": {"enabled": "yes"}}, {}))

    def test_4c_run_gate_two_tiers(self):
        """run(): global off + workspace on → gate MỞ (platform được khởi
        tạo); global on + workspace off → gate ĐÓNG, không đụng platform,
        không chiếm lock."""
        from ctf_downloader.services.watch_service import WatchService
        from ctf_downloader.storage import global_config as gc

        base = tempfile.mkdtemp()
        try:
            cases = [
                # (global_enabled, ws_enabled, expect_gate_open)
                (False, True, True),
                (True, False, False),
            ]
            for g_on, w_on, expect_open in cases:
                with patch.object(gc, "load_global_config",
                                  lambda: {"auto_sync":
                                           {"enabled": g_on}}):
                    svc = WatchService(base, once=True, use_live_ui=False)
                    svc.cfg_store.save(
                        {"version": 1, "auto_sync": {"enabled": w_on}})
                    lock_path = Path(base, ".ctf", "watch_state.json.lock")
                    if lock_path.exists():
                        lock_path.unlink()
                    with patch.object(WatchService, "_setup_platform") as sp, \
                            patch.object(WatchService, "_main_loop"):
                        rc = svc.run()
                    self.assertEqual(rc, 0)
                    self.assertEqual(sp.called, expect_open,
                                     f"global={g_on} ws={w_on}")
                    self.assertFalse(lock_path.exists(),
                                     "gate đóng không được chiếm lock")
        finally:
            shutil.rmtree(base, ignore_errors=True)


# ====================================================================== #
# CASE 5 — Khóa per-target .part (C9-04)
# ====================================================================== #
CHUNK = 65536


class _FakeResp:
    def __init__(self, n_chunks, delay=0.0, explode_after=None, status=200,
                 headers=None):
        self.status_code = status
        self.headers = headers if headers is not None else {
            "Content-Type": "application/octet-stream"}
        self._n = n_chunks
        self._delay = delay
        self._explode = explode_after

    def iter_content(self, chunk_size=CHUNK):
        for i in range(self._n):
            if self._delay:
                time.sleep(self._delay)
            if self._explode is not None and i >= self._explode:
                raise RuntimeError("boom mid-stream")
            yield b"x" * chunk_size

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_session_factory(resp, hits):
    class _Sess:
        def get(self, url, **kw):
            hits.append(url)
            return resp

        head = get
    return _Sess()


class TestC11Case5PartLock(unittest.TestCase):
    def test_5a_different_targets_run_parallel(self):
        """Lock per-target: 2 thread KHÁC đích chạy song song thật
        (wall ≈ max thay vì tổng)."""
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        dest = tempfile.mkdtemp()
        try:
            hits = []
            sess = _fake_session_factory(_FakeResp(2, delay=0.25), hits)
            t0 = time.monotonic()
            threads = [
                threading.Thread(target=HttpDownloader.download_file,
                                 args=(f"https://s/{n}", dest, sess),
                                 kwargs={"preferred_filename": n},
                                 daemon=True)
                for n in ("a.bin", "b.bin")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            wall = time.monotonic() - t0
            self.assertFalse(any(t.is_alive() for t in threads),
                             "thread khác đích bị khoá chéo (lock toàn cục?)")
            self.assertLess(wall, 0.9,
                            f"2 đích khác nhau chạy tuần tự ({wall:.2f}s)")
            for n in ("a.bin", "b.bin"):
                self.assertTrue(os.path.exists(os.path.join(dest, n)))
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def test_5b_same_target_serializes_and_skips(self):
        """Trùng đích: thread sau CHỜ lock, thấy file hoàn tất → skip
        không tải lại (đúng 1 GET trên server), nội dung không bị đè."""
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        dest = tempfile.mkdtemp()
        try:
            hits = []
            sess = _fake_session_factory(_FakeResp(2, delay=0.25), hits)
            results = {}

            def worker(tag):
                results[tag] = HttpDownloader.download_file(
                    "https://s/x.bin", dest, sess,
                    preferred_filename="x.bin")

            t1 = threading.Thread(target=worker, args=("first",), daemon=True)
            t1.start()
            time.sleep(0.05)               # chắc chắn first giữ lock trước
            t2 = threading.Thread(target=worker, args=("second",), daemon=True)
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)
            self.assertFalse(t1.is_alive() or t2.is_alive(), "treo lock?")
            self.assertEqual(len(hits), 1,
                             f"skip-fail: server bị gọi {len(hits)} lần")
            target = os.path.join(dest, "x.bin")
            self.assertTrue(os.path.exists(target))
            self.assertEqual(results["first"], target)
            self.assertEqual(results["second"], target)
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def test_5c_exception_midstream_releases_lock_cleanly(self):
        """Exception giữa chừng: trả None, .part được dọn, lock nhả SẠCH —
        lần tải kế tiếp cùng đích không chết chờ, hoàn thành bình thường."""
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        dest = tempfile.mkdtemp()
        try:
            boom_hits, ok_hits = [], []
            boom_sess = _fake_session_factory(
                _FakeResp(4, explode_after=1), boom_hits)
            out = HttpDownloader.download_file(
                "https://s/c.bin", dest, boom_sess,
                preferred_filename="c.bin")
            self.assertIsNone(out)
            self.assertFalse(os.path.exists(os.path.join(dest, "c.bin.part")),
                             ".part rác còn sót sau exception")
            ok_sess = _fake_session_factory(
                _FakeResp(2, delay=0.0), ok_hits)
            got = HttpDownloader.download_file(
                "https://s/c.bin", dest, ok_sess,
                preferred_filename="c.bin")
            self.assertIsNotNone(got, "lock không được nhả sau exception")
            self.assertTrue(os.path.exists(got))
        finally:
            shutil.rmtree(dest, ignore_errors=True)


# ====================================================================== #
# CASE 6 — escape markup NGOÀI bảng (Logger.info/success/warning/error)
# ====================================================================== #
class TestC11Case6EscapeBeyondTable(unittest.TestCase):
    def setUp(self):
        from ctf_downloader.ui import theme as ui_theme
        from ctf_downloader.utils import logger as logger_mod
        self.buf = io.StringIO()
        self._cm = patch.object(
            logger_mod, "console",
            _RichConsole(file=self.buf, width=200, force_terminal=True,
                         color_system="truecolor", highlight=False,
                         theme=ui_theme.load_theme(None)))
        self._cm.start()
        self.addCleanup(self._cm.stop)

    def _plain(self):
        return re.sub(r"\x1b\[[0-9;]*m", "",
                      self.buf.getvalue().replace("\x1b]\\", "]"))

    def test_6a_stray_close_tag_printed_verbatim(self):
        """FIXED C11-04 (M): Logger.info escape msg — tên challenge/notice/
        server-data chứa '[/]' lạc loài in NGUYÊN VĂN, không còn văng
        MarkupError CRASH cả lệnh (call-site thật: pull_service.py:907 nối
        tên challenge server vào Logger.info; watch_service notice/hint)."""
        from ctf_downloader.utils.logger import Logger
        Logger.info("🆕 Challenge mới: team[/]name")   # trước fix: MarkupError
        self.assertIn("team[/]name", self._plain())

    def test_6b_link_tag_injects_hyperlink(self):
        """BUG C11-04b (M): '[link=…]' qua Logger.info sinh OSC-8 hyperlink
        thật ra terminal (style-injection) — print_table đã escape còn
        đường info thì chưa."""
        from ctf_downloader.utils.logger import Logger
        Logger.info("[link=https://evil.example]click[/link]")
        self.assertNotIn("\x1b]8;", self.buf.getvalue(),
                         "hyperlink OSC-8 bị inject qua Logger.info")

    def test_6c_unknown_tag_silently_drops_content(self):
        """BUG C11-04c (L): tag lạ '[foo]' KHÔNG crash nhưng bị rich NUỐT —
        dữ liệu server mất nội dung lặng lẽ ('name [foo] tail' in ra
        'name  tail')."""
        from ctf_downloader.utils.logger import Logger
        Logger.info("name [foo] tail")
        plain = self._plain()
        self.assertIn("[foo]", plain,
                      f"dữ liệu bị nuốt mất: {plain!r}")

    def test_6d_zerowidth_chars_safe_everywhere(self):
        """Zero-width unicode: bảng đã escape không crash và giữ nguyên
        văn; Logger.info cũng không crash (chỉ lệch measure width — chấp
        nhận được, không mất dữ liệu)."""
        from ctf_downloader.utils.logger import Logger
        Logger.print_table("T", ["N"], [["​A​[/]​"]])
        Logger.info("​x​y")
        plain = self._plain()
        self.assertIn("​A​[/]​", plain)

    def test_6e_watch_notice_line_end_to_end(self):
        """Đường dây thật sau fix C11-04: notice title/body từ server chứa
        markup → feed watch vẫn mang text gốc; render qua Logger.info
        (như _refresh_live non-Live) KHÔNG crash và '[link=…]' không sinh
        OSC-8 hyperlink ra terminal."""
        ws = tempfile.mkdtemp()
        try:
            svc = _mk_watch(ws, 200)
            svc.platform.session.get.return_value.json.return_value = {
                "success": True,
                "data": [{"id": 1, "title": "[/]",
                          "body": "[link=//evil]x[/link]"}]}
            lines = svc._tick_notices()
            self.assertTrue(any("[/]" in ln for ln in lines))
            from ctf_downloader.utils.logger import Logger
            for ln in lines:
                Logger.info(ln)          # như _refresh_live non-Live
            plain = self._plain()
            self.assertIn("[/]", plain)
            self.assertNotIn("\x1b]8;", self.buf.getvalue(),
                             "hyperlink OSC-8 leak qua đường notice")
        finally:
            shutil.rmtree(ws, ignore_errors=True)


# ====================================================================== #
# CASE 7 — _prompt_line: EOF giữa menu loop
# ====================================================================== #
class TestC11Case7PromptLineEof(unittest.TestCase):
    def test_7a_eof_at_prompt_raises_eoferror(self):
        from ctf_downloader.cli_legacy import _prompt_line
        fake = _FakeStdin([])
        with patch("sys.stdin", fake):
            with self.assertRaises(EOFError):
                _prompt_line("Select option: ")

    def test_7b_menu_loop_terminates_on_immediate_eof(self):
        """stdin đóng ngay tại menu: EOFError thoát RA KHỎI vòng lặp
        (không vô hạn) — số lần đọc stdin hữu hạn."""
        from ctf_downloader import cli_legacy

        class _Dash:
            local_challenges = [{"id": 1, "name": "A", "_folder": "/tmp"}]

        fake = _FakeStdin([])
        with patch("sys.stdin", fake), \
                redirect_stdout(io.StringIO()), \
                self.assertRaises(EOFError):
            cli_legacy._manage_interactive_mode(_Dash(), "/tmp/ws",
                                                None, None)
        self.assertLessEqual(fake.calls, 3,
                             "menu loop đọc stdin quá nhiều lần → khả nghi")

    def test_7c_submenu_eof_swallowed_once_then_terminates(self):
        """EOF tại prompt CON (option 3): EOFError là subclass Exception →
        bị except Exception nuốt 1 lần thành 'Instance error:' rồi vòng
        lặp quay lên prompt chính, gặp EOF lần nữa → THOÁT (2 lần đọc).
        Không treo, nhưng có 1 iteration thừa + traceback ở tầng trên."""
        from ctf_downloader import cli_legacy

        class _Dash:
            local_challenges = [{"id": 1, "name": "A", "_folder": "/tmp"}]

        class _Mgr:
            def __init__(self, *a, **k):
                pass

            def find_challenge(self, challenge_id=None, challenge_name=None):
                return {"id": 1, "name": "A"}

        fake = _FakeStdin(["3\n"])
        err_buf = io.StringIO()
        with patch("sys.stdin", fake), \
                redirect_stdout(io.StringIO()), \
                patch.object(cli_legacy, "InstanceManager", _Mgr), \
                patch.object(cli_legacy.Logger, "error"), \
                self.assertRaises(EOFError):
            cli_legacy._manage_interactive_mode(_Dash(), "/tmp/ws",
                                                None, None)
        self.assertEqual(fake.calls, 3,
                         f"số lần đọc stdin bất thường: {fake.calls}")

    def test_7d_line_content_stripped_of_newline_only(self):
        from ctf_downloader.cli_legacy import _prompt_line
        fake = _FakeStdin(["  hello \n"])
        with patch("sys.stdin", fake):
            # _prompt_line chỉ bỏ '\n' cuối; caller giữ quyền .strip()
            self.assertEqual(_prompt_line("Q: "), "  hello ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
