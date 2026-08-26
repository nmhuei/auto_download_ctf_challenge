"""
Event Window + Instance Keep-Alive — unit/integration tests
(spec docs/superpowers/specs/2026-08-24-event-window-design.md §7).

Chạy: python3 -m pytest test_event_window.py -q
Toàn bộ HTTP được mock — KHÔNG gọi mạng thật.
"""
import datetime as _dt
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.platforms.base import EventTimes, normalize_epoch_to_utc
import ctf_downloader.platforms.ctftime_resolver as ctftime_mod
from ctf_downloader.platforms.ctftime_resolver import (
    CTFTIME_USER_AGENT,
    MATCH_THRESHOLD,
    CTFtimeResolver,
)
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.services.watch_service import (
    SOURCE_CONFLICT_SECONDS,
    resolve_event_window,
    warn_source_conflict,
)
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.services import instance_keepalive as ik
from ctf_downloader.services.watch_service import (
    EventWindowConfigStore,
    PollScheduler,
    WatchService,
    WatchStateStore,
    WindowGuard,
    default_auto_sync_config,
    fmt_countdown,
    parse_time_arg,
    run_event_window_wizard,
)
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def make_resp(status_code=200, json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no json")
    r.text = text if text != "" else (
        json.dumps(json_data) if json_data is not None else "")
    r.headers = headers or {}
    return r


def make_mock_session(routes):
    s = MagicMock()

    def get(url, *a, **kw):
        for frag, resp in routes:
            if frag in url:
                return resp
        return make_resp(404)

    def post(url, *a, **kw):
        for frag, resp in routes:
            if frag in url and resp[0] == "post":
                return resp[1]
        return make_resp(404)

    s.get.side_effect = get
    s.post.side_effect = post
    return s


def make_workspace(root: pathlib.Path) -> pathlib.Path:
    d = root / "Web" / "chall_a"
    d.mkdir(parents=True, exist_ok=True)
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "PTIT CTF 2026", "url": "https://ctf.ptit.edu.vn",
                     "platform": "gzctf"},
        "challenges": [{"id": 1, "name": "Chall A", "category": "Web"}],
    }), encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps({
        "id": 1, "name": "Chall A", "category": "Web"}),
        encoding="utf-8")
    return root


class TempWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="event_window_")
        self.ws = make_workspace(pathlib.Path(self._tmp))
        self.repo = WorkspaceRepo(str(self.ws))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# normalize_epoch_to_utc — bẫy đơn vị ms/giây (spec §2)
# ----------------------------------------------------------------------

class TestNormalizeEpoch(unittest.TestCase):
    def test_epoch_seconds(self):
        dt = normalize_epoch_to_utc(1756000000)
        self.assertEqual(int(dt.timestamp()), 1756000000)
        self.assertIsNotNone(dt.tzinfo)

    def test_epoch_milliseconds(self):
        dt = normalize_epoch_to_utc(1756000000123)
        self.assertEqual(int(dt.timestamp() * 1000), 1756000000123)

    def test_string_digits_both_units(self):
        self.assertEqual(normalize_epoch_to_utc("1756000000"),
                         normalize_epoch_to_utc(1756000000))
        self.assertEqual(normalize_epoch_to_utc("1756000000123").year,
                         normalize_epoch_to_utc(1756000000123).year)

    def test_zero_negative_null_unset(self):
        self.assertIsNone(normalize_epoch_to_utc(0))
        self.assertIsNone(normalize_epoch_to_utc(-5000))
        self.assertIsNone(normalize_epoch_to_utc("null"))
        self.assertIsNone(normalize_epoch_to_utc(None))

    def test_year_before_2000_is_unset(self):
        self.assertIsNone(normalize_epoch_to_utc(631152000))  # 1990-01-01

    def test_iso_string(self):
        dt = normalize_epoch_to_utc("2026-08-24T09:00:00+00:00")
        self.assertEqual(dt.hour, 9)

    def test_garbage_never_raises(self):
        self.assertIsNone(normalize_epoch_to_utc("abc"))
        self.assertIsNone(normalize_epoch_to_utc(object()))


# ----------------------------------------------------------------------
# ctftime_resolver (spec §3)
# ----------------------------------------------------------------------

class TestCtftimeMatching(unittest.TestCase):
    def test_normalize_title_stops_words_and_year(self):
        norm = ctftime_mod.normalize_title("PTIT CTF Quals 2026 Open Online")
        self.assertNotIn("ctf", norm.split())
        self.assertNotIn("2026", norm.split())
        self.assertNotIn("quals", norm.split())
        self.assertIn("ptit", norm.split())

    def test_similarity_identical_titles(self):
        self.assertGreaterEqual(
            ctftime_mod.title_similarity("PTIT CTF 2026", "ptit ctf 2026"), 0.99)

    def test_similarity_unrelated_below_threshold(self):
        self.assertLess(
            ctftime_mod.title_similarity("PTIT CTF", "DEF CONquals"), MATCH_THRESHOLD)

    def test_url_domain_match_scores_one(self):
        event = {"title": "Hoà Bình Open", "url": "https://ctf.hoabinh.vn"}
        score = CTFtimeResolver.score_event(event, "totally different",
                                            "https://ctf.hoabinh.vn/game")
        self.assertEqual(score, 1.0)
        # subdomain khác hẳn → KHÔNG tính là trùng khớp tuyệt đối
        score2 = CTFtimeResolver.score_event(event, "totally different",
                                             "https://play.ctf.hoabinh.vn")
        self.assertLess(score2, 1.0)

    def test_domain_normalization_strips_www(self):
        self.assertEqual(ctftime_mod.url_domain("https://www.ctf.example.com/x"),
                         "ctf.example.com")

    def test_rank_candidates_threshold_and_cap(self):
        events = [
            {"id": 1, "title": "PTIT CTF 2026", "url": ""},
            {"id": 2, "title": "PTIT CTF 2026 practice"},
            {"id": 3, "title": "Unrelated Contest"},
        ]
        resolver = CTFtimeResolver(session=MagicMock())
        cands = resolver.rank_candidates("PTIT CTF 2026", events=events)
        self.assertTrue(all(score >= MATCH_THRESHOLD for score, _e in cands))
        self.assertLessEqual(len(cands), 5)

    def test_ambiguous_when_top_gap_small(self):
        cands = [(0.80, {"id": 1}), (0.70, {"id": 2})]
        self.assertTrue(CTFtimeResolver.is_ambiguous(cands))
        self.assertFalse(CTFtimeResolver.is_ambiguous([(0.90, {"id": 1}),
                                                      (0.50, {"id": 2})]))

    def test_resolve_returns_candidates_when_ambiguous(self):
        events = [
            {"id": 1, "title": "Alpha CTF", "start": "2026-08-20T00:00:00+00:00",
             "finish": "2026-08-22T00:00:00+00:00"},
            {"id": 2, "title": "Alpha CTF Finals",
             "start": "2026-08-21T00:00:00+00:00",
             "finish": "2026-08-23T00:00:00+00:00"},
        ]
        resolver = CTFtimeResolver(session=MagicMock())
        times, cands = resolver.resolve_event_times("Alpha CTF", events=events)
        self.assertIsNone(times)          # ambiguous → wizard hỏi user
        self.assertGreaterEqual(len(cands), 2)

    def test_user_agent_has_contact(self):
        # UA mặc định requests/curl bị CTFtime chặn 403 — bắt buộc có contact
        self.assertIn("ctf-downloader/", CTFTIME_USER_AGENT)
        self.assertIn("(+", CTFTIME_USER_AGENT)

    def test_fetch_window_params_and_ua_header(self):
        session = MagicMock()
        session.headers = {}          # dict thật để assert User-Agent
        session.get.return_value = make_resp(200, json_data=[
            {"id": 7, "title": "X CTF"}])
        resolver = CTFtimeResolver(session=session)
        events = resolver.fetch_window(days_back=7, days_ahead=30)
        args, kwargs = session.get.call_args
        self.assertIn("/events/", args[0])
        self.assertEqual(kwargs.get("params", {}).get("limit"), 200)
        self.assertEqual(kwargs["params"]["finish"] - kwargs["params"]["start"],
                         37 * 86400)
        self.assertIn("ctf-downloader/",
                      session.headers["User-Agent"])
        self.assertEqual(len(events), 1)

    def test_fetch_window_network_error_returns_empty(self):
        session = MagicMock()
        session.get.side_effect = OSError("net down")
        resolver = CTFtimeResolver(session=session)
        self.assertEqual(resolver.fetch_window(), [])

    def test_cache_via_get_event(self):
        session = MagicMock()
        session.get.return_value = make_resp(200, json_data={
            "id": 7, "title": "Cached CTF",
            "start": "2026-08-20T00:00:00+00:00",
            "finish": "2026-08-21T00:00:00+00:00"})
        resolver = CTFtimeResolver(session=session)
        times = resolver.event_times_from(resolver.get_event(7))
        self.assertEqual(times.source, "ctftime:7")
        self.assertEqual(times.confidence, "medium")


# ----------------------------------------------------------------------
# fetch_event_times từng platform (spec §2)
# ----------------------------------------------------------------------

class TestGZCTFEventTimes(unittest.TestCase):
    def _platform(self, session):
        return GZCTFPlatform("https://gz.example.com/games/6/challenges", session)

    def test_epoch_milliseconds_parsed(self):
        p = self._platform(make_mock_session([
            ("/api/game/6", make_resp(200, json_data={
                "title": "GZ", "start": 1756000000000, "end": 1756100000000})),
        ]))
        times = p.fetch_event_times()
        self.assertIsNotNone(times)
        self.assertEqual(times.source, "gzctf:/api/game/6")
        self.assertEqual(times.confidence, "high")
        self.assertEqual(int(times.start_utc.timestamp()), 1756000000)

    def test_epoch_zero_means_unset(self):
        p = self._platform(make_mock_session([
            ("/api/game/6", make_resp(200, json_data={
                "start": 0, "end": -1})),
        ]))
        self.assertIsNone(p.fetch_event_times())

    def test_http_error_and_bad_json_never_raise(self):
        p = self._platform(make_mock_session([
            ("/api/game/6", make_resp(500, json_data={"err": True})),
        ]))
        self.assertIsNone(p.fetch_event_times())
        session = MagicMock()
        session.get.side_effect = OSError("boom")
        self.assertIsNone(GZCTFPlatform(
            "https://gz.example.com/games/6/challenges", session
        ).fetch_event_times())


class TestCTFdEventTimes(unittest.TestCase):
    WINDOW_HTML = """
    <script>window.init = {'csrfNonce': "abc", 'start': "1756000000",
      'end': null, 'challenge_interval': 100};</script>
    """

    def _platform(self, html=WINDOW_HTML, route_status=200):
        p = CTFdPlatform("https://ctfd.example.com", MagicMock())
        p.session = MagicMock()
        p.session.get.return_value = make_resp(route_status, text=html)
        return p

    def test_unix_seconds_as_string_with_null_end(self):
        p = self._platform()
        times = p.fetch_event_times()
        self.assertIsNotNone(times)
        self.assertEqual(times.source, "ctfd:window.init")
        # GIÂY chứ không phải ms: timestamp đúng giá trị chuỗi
        self.assertEqual(int(times.start_utc.timestamp()), 1756000000)
        self.assertIsNone(times.end_utc)          # 'end': null → chưa đặt
        self.assertEqual(times.confidence, "medium")

    def test_reuses_cached_html_no_second_fetch(self):
        p = self._platform()
        p._last_page_html = self.WINDOW_HTML
        p.fetch_event_times()
        self.assertEqual(p.session.get.call_count, 0)

    def test_no_window_init_returns_none(self):
        p = self._platform(html="<html><body>custom theme</body></html>")
        self.assertIsNone(p.fetch_event_times())

    def test_missing_start_and_end_returns_none(self):
        p = self._platform(html="""
        <script>window.init = {'csrfNonce': "abc", 'start': null, 'end': null};</script>
        """)
        self.assertIsNone(p.fetch_event_times())


class TestRCTFEventTimes(unittest.TestCase):
    def test_client_config_api_epoch_ms(self):
        p = RCTFPlatform("https://rctf.example.com", make_mock_session([
            ("/api/v1/integrations/client/config", make_resp(200, json_data={
                "kind": "goodClientConfig",
                "data": {"startTime": 1756000000000, "endTime": 1756100000000}})),
        ]))
        times = p.fetch_event_times()
        self.assertIsNotNone(times)
        self.assertEqual(times.confidence, "high")
        self.assertEqual(int(times.start_utc.timestamp()), 1756000000)

    def test_meta_tag_fallback_when_config_absent(self):
        html = ('<html><head><meta name="rctf-config" '
                'content="{&quot;startTime&quot;: 1756000000000}">'
                "</head><body></body></html>")
        p = RCTFPlatform("https://rctf.example.com", make_mock_session([
            ("/api/v1/integrations/client/config", make_resp(404)),
            ("https://rctf.example.com", make_resp(200, text=html)),
        ]))
        times = p.fetch_event_times()
        self.assertIsNotNone(times)
        self.assertEqual(times.source, "rctf:meta[rctf-config]")
        self.assertIsNone(times.end_utc)

    def test_all_sources_fail_returns_none(self):
        p = RCTFPlatform("https://rctf.example.com", make_mock_session([
            ("/api/v1/integrations/client/config", make_resp(404)),
            ("https://rctf.example.com", make_resp(200, text="no meta")),
        ]))
        self.assertIsNone(p.fetch_event_times())


class TestRCTFScoreboard(unittest.TestCase):
    """fetch_scoreboard: rCTF schema BẮT BUỘC query params limit & offset
    trên /api/v1/leaderboard/now — thiếu → lỗi validation → standings rỗng."""

    def _leaderboard_resp(self):
        return make_resp(200, json_data={
            "kind": "goodLeaderboard",
            "data": {"total": 42, "leaderboard": [
                {"id": 1, "name": "TeamA", "score": 5000},
                {"id": 2, "name": "TeamB", "score": 4000},
                {"id": 3, "name": "TeamC", "score": 3000},
            ]}})

    def _session_capturing_params(self):
        captured = {}

        def get(url, *a, **kw):
            if "/api/v1/leaderboard/now" in url:
                captured["url"] = url
                captured["params"] = kw.get("params")
                return make_resp(200, json_data={
                    "kind": "goodLeaderboard",
                    "data": {"total": 42, "leaderboard": [
                        {"id": 1, "name": "TeamA", "score": 5000},
                        {"id": 2, "name": "TeamB", "score": 4000},
                        {"id": 3, "name": "TeamC", "score": 3000},
                    ]}})
            return make_resp(404)

        s = MagicMock()
        s.get.side_effect = get
        return s, captured

    def test_leaderboard_request_includes_limit_and_offset(self):
        s, captured = self._session_capturing_params()
        p = RCTFPlatform("https://rctf.example.com", s)
        p.fetch_scoreboard()
        params = captured.get("params") or {}
        # limit & offset bắt buộc — nằm trong params dict hoặc ngay trong URL
        all_qs = dict(params)
        if "?" in (captured.get("url") or ""):
            from urllib.parse import parse_qs, urlparse
            for k, v in parse_qs(urlparse(captured["url"]).query).items():
                all_qs.setdefault(k, v[0])
        self.assertIn("limit", all_qs)
        self.assertIn("offset", all_qs)

    def test_leaderboard_parsed_to_standings(self):
        s, _ = self._session_capturing_params()
        p = RCTFPlatform("https://rctf.example.com", s)
        result = p.fetch_scoreboard()
        self.assertEqual(result["total_teams"], 3)
        self.assertEqual(len(result["standings"]), 3)
        self.assertEqual(result["standings"][0]["pos"], 1)
        self.assertEqual(result["standings"][0]["name"], "TeamA")
        self.assertEqual(result["standings"][0]["score"], 5000)
        self.assertEqual(result["standings"][2]["name"], "TeamC")


# ----------------------------------------------------------------------
# PollScheduler (spec §5): jitter bounds, backoff cap
# ----------------------------------------------------------------------

class TestPollScheduler(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]

        def fake_uniform(lo, hi):
            return (lo + hi) / 2.0

        self.sched = PollScheduler(rng=fake_uniform)
        self.sched.register("scoreboard", 60)

    def test_register_due_now(self):
        self.assertTrue(self.sched.due("scoreboard"))

    def test_postpone_jitter_within_bounds(self):
        lo_seen, hi_seen = float("inf"), float("-inf")
        for _ in range(200):
            deadline = self.sched.postpone("scoreboard", interval=100)
            # deadline neo WALL-CLOCK (hunt-c17 F-1) — so cùng nguồn time.time()
            delay = deadline - time.time()
            lo_seen, hi_seen = min(lo_seen, delay), max(hi_seen, delay)
        self.assertGreaterEqual(lo_seen, 100 * (1 - 0.2) - 1e-6)
        self.assertLessEqual(hi_seen, 100 * (1 + 0.2) + 1e-6)

    def test_penalize_doubles_and_caps_at_600(self):
        for _ in range(10):
            eff = self.sched.penalize("scoreboard")
        self.assertLessEqual(eff, 600)
        self.assertAlmostEqual(eff, 600)   # base 60 ×2^k → cap 600

    def test_reward_resets_multiplier(self):
        self.sched.penalize("scoreboard")
        self.sched.reward("scoreboard")
        self.assertEqual(self.sched._tasks["scoreboard"]["mult"], 1.0)

    def test_next_timeout_monotonic_nonnegative(self):
        self.sched.postpone("scoreboard", interval=30)
        timeout = self.sched.next_timeout()
        self.assertGreaterEqual(timeout, 0.05)
        self.assertLessEqual(timeout, 36.5)

    def test_wake_from_sleep_tick_immediately(self):
        # deadline đã quá sau sleep → next_timeout ≈ 0 → tick ngay
        self.sched._tasks["scoreboard"]["deadline"] -= 10**6
        self.assertLessEqual(self.sched.next_timeout(), 0.05)


# ----------------------------------------------------------------------
# WindowGuard (spec §5): pause-before-start, grace, clock-skew
# ----------------------------------------------------------------------

class TestWindowGuard(unittest.TestCase):
    def _guard(self, start_offset_s, end_offset_s, grace=300):
        now = _dt.datetime.now(_dt.timezone.utc)
        return WindowGuard(now + _dt.timedelta(seconds=start_offset_s),
                           now + _dt.timedelta(seconds=end_offset_s),
                           grace_seconds=grace)

    def test_before_live_ended_states(self):
        g = self._guard(+3600, +7200)
        self.assertEqual(g.state(), WindowGuard.BEFORE)
        g = self._guard(-60, +3600)
        self.assertEqual(g.state(), WindowGuard.LIVE)
        g = self._guard(-7200, -1800)           # kết thúc 30' trước, grace 5'
        self.assertEqual(g.state(), WindowGuard.ENDED)

    def test_grace_extends_live_past_end(self):
        g = self._guard(-7200, -120, grace=300)  # hết 2' trước, grace 5'
        self.assertEqual(g.state(), WindowGuard.LIVE)
        g2 = self._guard(-7200, -400, grace=300)
        self.assertEqual(g2.state(), WindowGuard.ENDED)

    def test_countdown_helpers(self):
        g = self._guard(+125, +3600)
        self.assertAlmostEqual(g.seconds_to_start(), 125, delta=5)
        self.assertIsNone(WindowGuard(None, None).seconds_to_end())

    def test_date_header_skew_detection(self):
        from email.utils import formatdate
        server_ts = time.time() + 250       # server nhanh 250s
        offset = WindowGuard.date_header_offset(formatdate(server_ts, usegmt=True))
        self.assertGreater(abs(offset), 120)   # > CLOCK_SKEW_WARN_SECONDS
        self.assertIsNone(WindowGuard.date_header_offset(None))

    def test_wall_now_immune_to_system_jump(self):
        g = self._guard(+100, +200)
        w1 = g.wall_now()
        # monotonic vẫn trôi đều dù wall-clock bị nhảy — chỉ sanity check
        self.assertGreaterEqual(g.wall_now(), w1)


# ----------------------------------------------------------------------
# State/config stores + wizard (spec §4)
# ----------------------------------------------------------------------

class TestStoresAndWizard(TempWorkspaceCase):
    def test_state_roundtrip_atomic(self):
        store = WatchStateStore(str(self.ws))
        state = store.load()
        state["window"] = {"start": "2026-08-24T00:00:00+00:00"}
        store.save(state)
        loaded = store.load()
        self.assertEqual(loaded["window"]["start"],
                         "2026-08-24T00:00:00+00:00")

    def test_checkpoint_per_type(self):
        store = WatchStateStore(str(self.ws))
        state = store.load()
        store.checkpoint_type(state, "notices")
        self.assertIn("notices", state["last_synced_at"])
        self.assertTrue(os.path.exists(store.path))

    def test_lockfile_acquire_release(self):
        store = WatchStateStore(str(self.ws))
        self.assertTrue(store.acquire_lock())
        with open(store.lock_path) as f:
            self.assertEqual(int(f.read()), os.getpid())
        store.release_lock()
        self.assertFalse(os.path.exists(store.lock_path))

    def test_lockfile_live_pid_rejected_stale_reclaimed(self):
        store = WatchStateStore(str(self.ws))
        store.acquire_lock()
        other = WatchStateStore(str(self.ws))
        with patch.object(WatchStateStore, "_pid_alive", return_value=True):
            # pid khác đang sống → từ chối (watch đang chạy)
            with open(other.lock_path, "w") as f:
                f.write("123456")
            self.assertFalse(other.acquire_lock())
            # stale-pid → chiếm lại
            with patch.object(WatchStateStore, "_pid_alive",
                              return_value=False):
                self.assertTrue(other.acquire_lock())
        store.release_lock()

    def test_default_config_shape_matches_spec(self):
        cfg = default_auto_sync_config(mode="window")["auto_sync"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "window")
        self.assertEqual(cfg["intervals_sec"]["notices"], 15)
        self.assertEqual(cfg["intervals_sec"]["scoreboard"], 60)
        self.assertEqual(cfg["intervals_sec"]["challenges"], 120)
        self.assertEqual(cfg["grace_seconds"], 300)
        self.assertTrue(cfg["auto_exit_on_end"])

    def test_wizard_answers_write_config_once(self):
        store = EventWindowConfigStore(str(self.ws))
        self.assertFalse(store.exists())
        # Q1: Y · Q3 (notices): n · Q4 (scoreboard): n
        confirm_answers = iter([True, False, False])

        def fake_confirm(msg, default=None):
            return next(confirm_answers)

        def fake_prompt(msg, default=None):
            return "2"                              # Q2: mode always

        with patch.object(sys.modules["ctf_downloader.services.watch_service"],
                          "Confirm") as mc, \
                patch.object(sys.modules["ctf_downloader.services.watch_service"],
                             "Prompt") as mp:
            mc.ask.side_effect = fake_confirm
            mp.ask.side_effect = fake_prompt
            cfg = run_event_window_wizard(str(self.ws), force_prompt=True)
        self.assertIsNotNone(cfg)
        auto = cfg["auto_sync"]
        self.assertEqual(auto["mode"], "always")
        self.assertFalse(auto["policy"]["notices"])
        self.assertFalse(auto["policy"]["scoreboard"])
        self.assertTrue(store.exists())
        # Lần 2 không hỏi lại (config đã có)
        again = run_event_window_wizard(str(self.ws), force_prompt=True)
        self.assertIsNone(again)

    def test_wizard_scoreboard_independent_of_notices(self):
        # EW M-4: scoreboard là khóa policy ĐỘC LẬP — bật notices không được
        # âm thầm bật kèm theo dõi bảng điểm (tránh bật ngoài ý muốn).
        EventWindowConfigStore(str(self.ws))
        confirm_answers = iter([True, True, False])   # Q1 Y · notices Y · scoreboard N

        def fake_confirm(msg, default=None):
            return next(confirm_answers)

        def fake_prompt(msg, default=None):
            return "1"                              # mode window

        with patch.object(sys.modules["ctf_downloader.services.watch_service"],
                          "Confirm") as mc, \
                patch.object(sys.modules["ctf_downloader.services.watch_service"],
                             "Prompt") as mp:
            mc.ask.side_effect = fake_confirm
            mp.ask.side_effect = fake_prompt
            cfg = run_event_window_wizard(str(self.ws), force_prompt=True)
        self.assertIsNotNone(cfg)
        policy = cfg["auto_sync"]["policy"]
        self.assertTrue(policy["notices"])
        self.assertFalse(policy["scoreboard"])

    def test_parse_time_arg(self):
        iso = parse_time_arg("2026-08-24T09:00:00")
        self.assertEqual(iso.hour, 9)
        self.assertIsNotNone(iso.tzinfo)
        self.assertEqual(parse_time_arg("1756000000").timestamp(), 1756000000)
        self.assertEqual(parse_time_arg("1756000000000").timestamp(),
                         1756000000)
        self.assertIsNone(parse_time_arg("garbage"))
        self.assertIsNone(parse_time_arg(None))


# ----------------------------------------------------------------------
# Instance Keep-Alive (spec §9 + R-A/R-B)
# ----------------------------------------------------------------------

class FakeGzctfPlatform:
    """Class name chứa 'gzctf' → kind='gzctf' (recreate giữ flag)."""

    def __init__(self):
        self.extends = 0
        self.starts = 0
        self.stops = 0
        self.status = {}

    def get_instance_status(self, cid):
        return dict(self.status)

    def extend_instance(self, cid):
        self.extends += 1
        return True, "extended"

    def start_instance(self, cid):
        self.starts += 1
        return True, {"entry": "1.2.3.4:9999"}

    def stop_instance(self, cid):
        self.stops += 1
        return True, "stopped"


class FakeCTFdPlatform(FakeGzctfPlatform):
    """Class name chứa 'ctfd' → kind='whale' (recreate ĐỔI flag → R-A)."""
    pass


class FakeInstanceService:
    def __init__(self, platform, containers):
        self.platform = platform
        self.containers = containers
        self.repo = None

    def list_containers(self):
        return self.containers

    def _update_local_instance_info(self, *a, **kw):
        pass

    # Cho test đường CLI R-A consent (F-2)
    def start_instance(self, cid):
        return self.platform.start_instance(cid)

    def get_status(self, cid):
        return self.platform.get_instance_status(cid)


def _close_time_in(seconds):
    now_ms = _dt.datetime.now(_dt.timezone.utc).timestamp() * 1000
    return now_ms + seconds * 1000


class TestKeepAlive(TempWorkspaceCase):
    def _make(self, platform_cls=FakeGzctfPlatform, status=None):
        plat = platform_cls()
        if status is not None:
            plat.status = status
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        svc = FakeInstanceService(
            plat, [{"id": 1, "name": "flask-jail", "_local_path": str(meta_path)}])
        ka = ik.InstanceKeepAlive(svc, repo=self.repo)
        trackers = ka.discover_containers()
        return ka, trackers[0], plat

    # ---- GZCTF: chỉ extend trong RenewalWindow ~10' cuối -------------- #
    def test_gzctf_extend_only_near_deadline(self):
        # còn 2 giờ → KHÔNG extend (tránh 400 gọi sớm)
        ka, tr, plat = self._make(status={
            "status": "running", "entry": "1.2.3.4:7777",
            "close_time": _close_time_in(7200)})
        evs = ka.tick_one(tr)
        self.assertEqual(plat.extends, 0)
        self.assertEqual(tr.state, ik.ALIVE)
        # còn 5 phút (≤ cap 600s) → extend đúng lúc
        plat.status["close_time"] = _close_time_in(300)
        evs = ka.tick_one(tr)
        self.assertEqual(plat.extends, 1)
        self.assertEqual(tr.state, ik.ALIVE)
        self.assertTrue(any("🔄" in m for _l, m in evs))

    def test_gzctf_dead_auto_restart_keeps_flag_allowed(self):
        ka, tr, plat = self._make(status={"status": "stopped", "entry": None})
        # GZCTF recreate giữ flag → auto-restart được phép kể cả khi có flag
        self.repo.update_status(
            self.ws / "Web" / "chall_a" / "metadata.json",
            lambda st: {**st, "flag": {"value": "FLAG{x}", "state": "hoarded"}})
        evs = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.RESTARTING)
        self.assertEqual(plat.stops, 1)
        self.assertFalse(getattr(tr, "blocked_flag_rotate", False))

    def test_restart_sequence_cooldown_boot_health(self):
        ka, tr, plat = self._make(status={"status": "stopped", "entry": None})
        ka.tick_one(tr)                       # DEAD → begin restart (cooldown)
        self.assertEqual(tr.state, ik.RESTARTING)
        tr.phase_deadline = time.monotonic() - 1
        ka.tick_one(tr)                       # cooldown hết → POST + boot_wait
        self.assertEqual(plat.starts, 1)
        self.assertEqual(tr.restart_phase, "boot_wait")
        plat.status = {"status": "running", "entry": "5.6.7.8:1234"}
        tr.phase_deadline = time.monotonic() - 1
        evs = ka.tick_one(tr)                 # health OK → ALIVE
        self.assertEqual(tr.state, ik.ALIVE)
        self.assertTrue(any("5.6.7.8:1234" in m for _l, m in evs))

    def test_max_restarts_then_give_up_critical(self):
        ka, tr, plat = self._make(status={"status": "stopped", "entry": None})
        tr.restart_count = ik.MAX_RESTARTS    # đã restart đủ số lần
        evs = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.GIVE_UP)
        self.assertTrue(any(lv == ik.CRITICAL for lv, _m in evs))

    # ---- Whale: PATCH renew, đếm lượt, ≥61s gap ------------------------ #
    def test_whale_renew_counts_and_warns_last_chance(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 300})
        tr.renew_count = ik.WHALE_MAX_RENEWS - 1
        evs = ka.tick_one(tr)
        self.assertEqual(plat.extends, 1)
        self.assertEqual(tr.renew_count, ik.WHALE_MAX_RENEWS)
        self.assertTrue(any("🔴" in m for _l, m in evs))

    def test_whale_exhausted_never_patches_again(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 120})
        tr.renew_count = ik.WHALE_MAX_RENEWS
        evs = ka.tick_one(tr)
        self.assertEqual(plat.extends, 0)
        self.assertEqual(tr.state, ik.GIVE_UP)
        self.assertTrue(any(lv == ik.CRITICAL and "không extend được nữa" in m
                            for lv, m in evs))

    def test_whale_op_gap_61s_even_after_failed_request(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 300})
        # op vừa xảy ra 10s trước (kể cả request lỗi cũng reset đồng hồ)
        tr.last_op_mono = time.monotonic() - 10
        ka.tick_one(tr)
        self.assertEqual(plat.extends, 0)     # chưa đủ 61s → postpone
        tr.last_op_mono = time.monotonic() - 61
        ka.tick_one(tr)
        self.assertEqual(plat.extends, 1)

    def test_whale_fatal_403_circuit_breaker(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 300})

        def forbidden(cid):
            return False, "HTTP 403: Forbidden"

        plat.extend_instance = forbidden
        evs = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.GIVE_UP)

    # ---- R-A: whale restart đổi flag ----------------------------------- #
    def test_ra_whale_blocked_when_flag_held(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "stopped", "entry": None})
        self.repo.update_status(
            self.ws / "Web" / "chall_a" / "metadata.json",
            lambda st: {**st, "flag": {"value": "FLAG{keep}", "state": "hoarded"}})
        evs = ka.tick_one(tr)
        # auto-mode KHÔNG restart khi có flag — dừng ở critical chờ user
        self.assertEqual(plat.stops + plat.starts, 0)
        self.assertTrue(any(lv == ik.CRITICAL and "ĐỔI FLAG" in m
                            for lv, m in evs))
        self.assertTrue(tr.blocked_flag_rotate)

    def test_ra_manual_approved_rotates_flag_state(self):
        ka, tr, plat = self._make(platform_cls=FakeCTFdPlatform,
                                  status={"status": "stopped", "entry": None})
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        self.repo.update_status(
            meta_path,
            lambda st: {**st, "flag": {"value": "FLAG{old}", "state": "hoarded"}})
        plat.status = {"status": "running", "entry": "9.9.9.9:1"}
        ok, _msg = ka.manual_restart_approved(tr)
        self.assertTrue(ok)
        st = self.repo.read_status(meta_path)
        # Sau restart user đồng ý: xoá value + state found_unverified + note rotate
        self.assertIsNone(st["flag"]["value"])
        self.assertEqual(st["flag"]["state"], "found_unverified")
        self.assertIn("rotate", st["notes"])

    # ---- R-B: 502/proxy ≠ dead ----------------------------------------- #
    def test_rb_unknown_status_not_dead_without_tcp_evidence(self):
        ka, tr, plat = self._make(status={"status": "unknown", "entry": None})
        evs = ka.tick_one(tr)
        self.assertNotEqual(tr.state, ik.DEAD)
        self.assertEqual(plat.stops + plat.starts, 0)   # không restart bừa

    def test_rb_tcp_fail_x3_cross_check_then_dead(self):
        ka, tr, plat = self._make(status={"status": "unknown",
                                          "entry": "10.255.255.1:1"})
        tr.entry = "10.255.255.1:1"
        with patch.object(ik, "TCP_PROBE_PERIOD", 0), \
                patch.object(ik, "tcp_probe", return_value=False):
            for _ in range(3):
                ka.tick_one(tr)
        self.assertEqual(tr.tcp_fail_count, 3)
        # cross-check status API vẫn không rõ → DEAD → gzctf auto-restart
        self.assertIn(tr.state, (ik.DEAD, ik.RESTARTING))
        self.assertGreaterEqual(plat.stops, 1)

    def test_rb_running_despite_flaky_entry_is_alive(self):
        # API vẫn báo Running + còn hạn → chỉ ⚠️, tuyệt đối không restart
        ka, tr, plat = self._make(status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 3600})
        evs = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.ALIVE)
        self.assertEqual(plat.stops + plat.starts, 0)

    # ---- Hết window: ngừng auto-extend trừ practice_mode ---------------- #
    def test_window_inactive_stops_autoextend_unless_practice(self):
        ka, tr, plat = self._make(status={"status": "running",
                                          "entry": "1.2.3.4:5555",
                                          "time_left": 60})
        evs = ka.tick_one(tr, window_active=False)
        self.assertEqual(plat.extends, 0)
        ka.practice_mode = True
        ka.tick_one(tr, window_active=False)
        self.assertEqual(plat.extends, 1)

    def test_renew_threshold_fraction_and_cap(self):
        self.assertEqual(ik.InstanceKeepAlive.renew_threshold(600), 360)
        self.assertEqual(ik.InstanceKeepAlive.renew_threshold(7200), 600)
        self.assertEqual(ik.InstanceKeepAlive.renew_threshold(None), 600)

    def test_escalation_repeat_suppression_and_critical_mute(self):
        tr = ik.InstanceTracker(1, "x")
        first = tr.escalate(ik.WARNING, "msg")
        second = tr.escalate(ik.WARNING, "msg")
        self.assertIsNotNone(first)
        self.assertIsNone(second)                 # repeat < 300s bị nuốt
        crit = tr.escalate(ik.CRITICAL, "down")
        self.assertIsNotNone(crit)
        self.assertIsNone(tr.escalate(ik.WARNING, "other msg"))  # muted


# ----------------------------------------------------------------------
# WatchService integration — vòng lặp mock platform (spec §7)
# ----------------------------------------------------------------------

class FakeWatchPlatform:
    platform_type = "gzctf"

    def __init__(self):
        self.ctf_info = MagicMock()
        self.ctf_info.platform_type = "gzctf"
        self.base_url = "https://gz.example.com"
        self.session = MagicMock()
        self.challs = [MagicMock(id=str(i), name=f"c{i}", hints=[])
                       for i in range(3)]
        self.score = {"my_rank": "12th", "my_score": 900, "total_teams": 100}

    def fetch_challenges(self):
        return list(self.challs)

    def fetch_scoreboard(self):
        return dict(self.score)

    def fetch_event_times(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        return EventTimes(start_utc=now - _dt.timedelta(hours=1),
                          end_utc=now + _dt.timedelta(hours=5),
                          confidence="high", source="gzctf:/api/game/6")


class TestWatchServiceRound(TempWorkspaceCase):
    def _svc(self, platform):
        svc = WatchService(str(self.ws), once=True, use_live_ui=False)
        svc.platform = platform
        svc.state = svc.state_store.load()
        return svc

    @staticmethod
    def _force_due(svc, *tasks):
        """Đặt deadline về quá khứ — mô phỏng đã đến kỳ poll tiếp theo."""
        for t in tasks:
            if t in svc.scheduler._tasks:
                svc.scheduler._tasks[t]["deadline"] = 0.0

    def test_run_round_ticks_order_and_checkpoints(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        auto_cfg = default_auto_sync_config()["auto_sync"]
        for task in ("scoreboard", "challenges", "keepalive"):
            svc.scheduler.register(task, 60)
        lines = svc._run_round(auto_cfg)
        self.state_store_check(svc)
        # checkpoint per-type đã ghi last_synced_at
        self.assertIn("scoreboard", svc.state["last_synced_at"])
        self.assertIn("challenges", svc.state["last_synced_at"])

    def state_store_check(self, svc):
        persisted = WatchStateStore(str(self.ws)).load()
        self.assertTrue(persisted.get("last_synced_at"))

    def test_first_challenges_tick_sets_baseline_without_spurious_notice(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        svc.scheduler.register("challenges", 120)
        svc.scheduler.register("scoreboard", 120)
        lines = svc._run_round(default_auto_sync_config()["auto_sync"])
        self.assertFalse(any("✨" in ln for ln in lines))   # baseline không báo
        # Thêm challenge → tick sau báo ✨ + burst
        platform.challs.append(MagicMock(id="99", name="new-one", hints=[]))
        self._force_due(svc, "challenges", "scoreboard")
        lines = svc._run_round(default_auto_sync_config()["auto_sync"])
        self.assertTrue(any("✨" in ln for ln in lines))
        self.assertEqual(svc.scheduler._tasks["challenges"]["interval"], 25)

    def test_hint_new_detected(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        svc.scheduler.register("challenges", 120)
        svc.scheduler.register("scoreboard", 120)
        svc._run_round(default_auto_sync_config()["auto_sync"])
        platform.challs[0].hints = ["hint1"]
        self._force_due(svc, "challenges", "scoreboard")
        lines = svc._run_round(default_auto_sync_config()["auto_sync"])
        self.assertTrue(any("💡" in ln for ln in lines))

    def test_scoreboard_change_logged_and_idle_adapts(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        svc.scheduler.register("scoreboard", 60)
        svc.scheduler.register("notices", 15)
        svc._run_round(default_auto_sync_config()["auto_sync"])   # baseline
        platform.score["my_score"] = 1000
        self._force_due(svc, "scoreboard")
        lines = svc._run_round(default_auto_sync_config()["auto_sync"])
        self.assertTrue(any("🩸" in ln for ln in lines))
        # 3 kỳ không đổi → adaptive 120s
        for _ in range(3):
            svc._tick_scoreboard()
            svc.scheduler.reward("scoreboard")
            svc.scheduler.postpone("scoreboard")
        self.assertEqual(
            svc.scheduler._tasks["scoreboard"]["interval"], 120)

    def test_keyboard_interrupt_mid_tick_propagates_and_shutdown_flushes(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        svc.scheduler.register("challenges", 120)
        svc.scheduler.register("scoreboard", 120)
        svc._run_round(default_auto_sync_config()["auto_sync"])

        def boom(window_active=True):
            raise KeyboardInterrupt()

        svc._tick_notices = boom
        svc.scheduler.register("notices", 1)
        with self.assertRaises(KeyboardInterrupt):
            svc._run_round(default_auto_sync_config()["auto_sync"])
        # Ctrl-C giữa tick → _shutdown vẫn flush state atomic
        svc._shutdown()
        persisted = WatchStateStore(str(self.ws)).load()
        self.assertTrue(persisted.get("last_synced_at"))
        self.assertFalse(os.path.exists(svc.state_store.lock_path))

    def test_window_guard_integrated_from_platform_times(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        guard = svc._resolve_window(default_auto_sync_config()["auto_sync"])
        self.assertIsNotNone(guard)
        self.assertEqual(guard.state(), WindowGuard.LIVE)
        self.assertEqual(svc.state["window"]["source"], "gzctf:/api/game/6")

    def test_signal_handlers_stop_cleanly(self):
        platform = FakeWatchPlatform()
        svc = self._svc(platform)
        svc._install_signal_handlers()
        self.assertFalse(svc._stop)
        # Mô phỏng handler được signal gọi: SIGINT → stop + exit 130
        with patch.object(signal, "signal") as msig:
            svc._install_signal_handlers()
            sigint_handler = msig.call_args_list[0][0][1]
            sigint_handler(signal.SIGINT, None)
        self.assertTrue(svc._stop)
        self.assertEqual(svc._exit_code, 130)


# ----------------------------------------------------------------------
# Review fixes: F-1..F-5 + M-1/M-2
# ----------------------------------------------------------------------

class CountingWatchPlatform(FakeWatchPlatform):
    def __init__(self):
        super().__init__()
        self.chall_fetches = 0
        self.score_fetches = 0

    def fetch_challenges(self):
        self.chall_fetches += 1
        return super().fetch_challenges()

    def fetch_scoreboard(self):
        self.score_fetches += 1
        return super().fetch_scoreboard()


class TestF1AutoExitOnEnd(TempWorkspaceCase):
    """F-1 [Critical]: guard ENDED → loop thoát, final sync đúng 1 lần,
    không tick data ngoài window nữa."""

    def _ended_svc(self, platform, auto_exit=True):
        svc = WatchService(str(self.ws), once=False, use_live_ui=False)
        svc.platform = platform
        svc.state = svc.state_store.load()
        now = _dt.datetime.now(_dt.timezone.utc)
        guard = WindowGuard(now - _dt.timedelta(hours=2),
                            now - _dt.timedelta(minutes=10),
                            grace_seconds=300)          # ENDED rõ ràng
        cfg = default_auto_sync_config()["auto_sync"]
        cfg["auto_exit_on_end"] = auto_exit
        for task in ("scoreboard", "challenges", "keepalive"):
            svc.scheduler.register(task, 60)
        return svc, guard, cfg

    def test_ended_final_syncs_once_then_exits_no_more_ticks(self):
        import ctf_downloader.services.watch_service as ws_mod
        platform = CountingWatchPlatform()
        svc, guard, cfg = self._ended_svc(platform)
        final_calls = []
        orig_final = svc._final_sync

        def counting_final():
            final_calls.append(1)
            orig_final()

        svc._final_sync = counting_final
        svc._main_loop(guard, cfg)
        self.assertEqual(len(final_calls), 1)      # final sync ĐÚNG 1 lần
        self.assertEqual(platform.chall_fetches, 0)  # KHÔNG tick data nữa
        self.assertEqual(platform.score_fetches, 1)  # chỉ scoreboard cuối

    def test_ended_idle_when_auto_exit_false_until_signal(self):
        import ctf_downloader.services.watch_service as ws_mod
        platform = CountingWatchPlatform()
        svc, guard, cfg = self._ended_svc(platform, auto_exit=False)
        final_calls = []
        orig_final = svc._final_sync

        def counting_final():
            final_calls.append(1)
            orig_final()

        svc._final_sync = counting_final

        real_sleep = ws_mod.time.sleep

        def fake_sleep(_secs):
            svc._stop = True          # mô phỏng Ctrl-C trong idle
        with patch.object(ws_mod.time, "sleep", side_effect=fake_sleep):
            svc._main_loop(guard, cfg)               # phải tự thoát
        ws_mod.time.sleep = real_sleep
        self.assertEqual(len(final_calls), 1)      # vẫn chỉ final sync 1 lần
        self.assertEqual(platform.chall_fetches, 0)


class TestF2RAConsent(TempWorkspaceCase):
    """F-2: R-A nửa user-consent — --yes / interactive_restart được wire."""

    def _make_whale_with_flag(self):
        plat = FakeCTFdPlatform()
        plat.status = {"status": "stopped", "entry": None}
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        self.repo.update_status(
            meta_path,
            lambda st: {**st, "flag": {"value": "FLAG{old}", "state": "hoarded"}})
        svc = FakeInstanceService(
            plat, [{"id": 1, "name": "flask-jail", "_local_path": str(meta_path)}])
        ka = ik.InstanceKeepAlive(svc, repo=self.repo)
        tr = ka.discover_containers()[0]
        return ka, tr, plat

    def test_interactive_restart_assume_yes_rotates(self):
        ka, tr, plat = self._make_whale_with_flag()
        ok, _msg = ka.interactive_restart(tr, assume_yes=True)   # như `--yes`
        self.assertTrue(ok)
        self.assertEqual(plat.starts, 1)
        st = self.repo.read_status(
            pathlib.Path(tr.meta_path))
        self.assertIsNone(st["flag"]["value"])
        self.assertEqual(st["flag"]["state"], "found_unverified")
        self.assertIn("rotate", st["notes"])

    def test_interactive_restart_declined_does_not_start(self):
        ka, tr, plat = self._make_whale_with_flag()
        with patch("builtins.input", return_value="n"):
            ok, msg = ka.interactive_restart(tr)
        self.assertFalse(ok)
        self.assertEqual(msg, "cancelled")
        self.assertEqual(plat.starts, 0)

    def test_cli_start_consent_wires_rotate_bookkeeping(self):
        from ctf_downloader.cli_commands import _start_instance_with_ra_consent
        ka_holder = {}
        ka, tr, plat = self._make_whale_with_flag()

        class SvcWithRepo(FakeInstanceService):
            pass

        svc = SvcWithRepo(plat, [{"id": 1, "name": "flask-jail",
                                  "_local_path": tr.meta_path}])
        svc.repo = self.repo
        # user giữ flag + whale đổi flag + --yes → rotate bookkeeping chạy
        _start_instance_with_ra_consent(svc, 1, assume_yes=True)
        self.assertEqual(plat.starts, 1)
        st = self.repo.read_status(pathlib.Path(tr.meta_path))
        self.assertIsNone(st["flag"]["value"])
        self.assertEqual(st["flag"]["state"], "found_unverified")

    def test_cli_start_gzctf_holding_flag_starts_plainly(self):
        # GZCTF recreate giữ flag → KHÔNG cần consent, flag nguyên vẹn
        from ctf_downloader.cli_commands import _start_instance_with_ra_consent
        plat = FakeGzctfPlatform()
        plat.status = {"status": "stopped", "entry": None}
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        self.repo.update_status(
            meta_path,
            lambda st: {**st, "flag": {"value": "FLAG{keep}", "state": "hoarded"}})
        svc = FakeInstanceService(
            plat, [{"id": 1, "name": "flask-jail", "_local_path": str(meta_path)}])
        svc.repo = self.repo
        _start_instance_with_ra_consent(svc, 1, assume_yes=True)
        self.assertEqual(plat.starts, 1)
        st = self.repo.read_status(meta_path)
        self.assertEqual(st["flag"]["value"], "FLAG{keep}")   # không xoá

    def test_cli_start_declined_aborts(self):
        from ctf_downloader.cli_commands import _start_instance_with_ra_consent
        ka, tr, plat = self._make_whale_with_flag()
        svc = FakeInstanceService(plat, [{"id": 1, "name": "flask-jail",
                                          "_local_path": tr.meta_path}])
        svc.repo = self.repo
        with patch("builtins.input", return_value="n"):
            _start_instance_with_ra_consent(svc, 1, assume_yes=False)
        self.assertEqual(plat.starts, 0)
        st = self.repo.read_status(pathlib.Path(tr.meta_path))
        self.assertEqual(st["flag"]["value"], "FLAG{old}")   # giữ nguyên


class TestF3ClockSkewActive(TempWorkspaceCase):
    """F-3: Date header server được hỏi định kỳ; lệch >120s → cảnh báo +
    hiệu chỉnh wall_now của WindowGuard."""

    def test_skew_checked_applied_and_throttled(self):
        from email.utils import formatdate
        platform = FakeWatchPlatform()
        server_ts = time.time() + 250       # server nhanh hơn 250s
        platform.session.get.return_value = make_resp(
            200, json_data={}, headers={"Date": formatdate(server_ts,
                                                           usegmt=True)})
        svc = WatchService(str(self.ws), once=True, use_live_ui=False)
        svc.platform = platform
        now = _dt.datetime.now(_dt.timezone.utc)
        svc.guard = WindowGuard(now - _dt.timedelta(hours=1),
                                now + _dt.timedelta(hours=5))

        offset = svc._clock_skew_tick()
        self.assertIsNotNone(offset)
        self.assertGreater(abs(offset), 120)
        # wall_now đã được hiệu chỉnh theo server
        self.assertAlmostEqual(svc.guard.wall_now() - time.time(), 250, delta=5)
        # throttle ~5 phút: gọi ngay lần nữa → bỏ qua
        self.assertIsNone(svc._clock_skew_tick())
        # lệch nhỏ → không cảnh báo nhưng vẫn apply
        svc._last_skew_check_mono = -10**9
        platform.session.get.return_value = make_resp(
            200, json_data={},
            headers={"Date": formatdate(time.time() + 10, usegmt=True)})
        offset2 = svc._clock_skew_tick()
        self.assertIsNotNone(offset2)
        self.assertLessEqual(abs(offset2), 120)


class TestF4SourceConflict(TempWorkspaceCase):
    """F-4 (spec §2): platform vs CTFtime lệch >5 phút → cảnh báo cả hai."""

    def test_warn_source_conflict_over_five_minutes(self):
        base = _dt.datetime.now(_dt.timezone.utc)
        ptimes = EventTimes(start_utc=base, confidence="high",
                           source="gzctf:/api/game/6")
        ctimes = EventTimes(start_utc=base + _dt.timedelta(minutes=40),
                            confidence="medium", source="ctftime:9")
        msgs = warn_source_conflict(ptimes, ctimes)
        self.assertEqual(len(msgs), 1)
        self.assertIn("gzctf:/api/game/6", msgs[0])
        self.assertIn("ctftime:9", msgs[0])
        self.assertIn("ưu tiên gzctf:/api/game/6", msgs[0])

    def test_no_warning_within_five_minutes_or_missing_field(self):
        base = _dt.datetime.now(_dt.timezone.utc)
        ptimes = EventTimes(start_utc=base, source="gzctf:/api/game/6")
        ctimes = EventTimes(start_utc=base + _dt.timedelta(minutes=3),
                            source="ctftime:9")
        self.assertEqual(warn_source_conflict(ptimes, ctimes), [])
        self.assertEqual(warn_source_conflict(ptimes, None), [])

    def test_resolve_event_window_platform_wins_on_conflict(self):
        platform = FakeWatchPlatform()   # start = now-1h, end = now+5h
        resolver = MagicMock()
        resolver.resolve_event_times.return_value = (
            EventTimes(start_utc=_dt.datetime.now(_dt.timezone.utc)
                       + _dt.timedelta(hours=3),
                       end_utc=None, confidence="medium", source="ctftime:42"),
            [])
        times, cands = resolve_event_window(platform, self.repo,
                                            title_hint="PTIT CTF 2026",
                                            resolver=resolver)
        self.assertIsNotNone(times)
        self.assertEqual(times.source, "gzctf:/api/game/6")   # nguồn cao thắng
        # mirror event_window vẫn ghi nguồn platform
        data = self.repo.read_challenges()
        self.assertEqual((data.get("ctf_info") or {}).get("event_window",
                                                          {}).get("source"),
                         "gzctf:/api/game/6")


class TestF5LockAtomic(TempWorkspaceCase):
    """F-5: acquire_lock nguyên tử qua O_CREAT|O_EXCL — N process cùng lúc
    thì đúng 1 cái thắng."""

    def test_two_processes_only_one_acquires(self):
        race_ws = tempfile.mkdtemp(prefix="lockrace_")
        child_code = (
            "import sys, time;"
            "from ctf_downloader.services.watch_service import WatchStateStore;"
            "s = WatchStateStore(sys.argv[1]);"
            "ok = s.acquire_lock();"
            "time.sleep(1.5);"          # giữ lock đủ lâu để các rival thấy live-pid
            "print(ok)"
        )
        procs = [subprocess.Popen([sys.executable, "-c", child_code, race_ws],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
                 for _ in range(4)]
        outs = []
        for p in procs:
            out, _err = p.communicate(timeout=60)
            outs.append(out.decode().strip())
        self.assertEqual(outs.count("True"), 1)    # đúng 1 process chiếm được
        self.assertEqual(outs.count("False"), 3)


class TestMinorsKeepAlive(TempWorkspaceCase):
    """M-1: GIVE_UP sticky + CRITICAL không spam; M-2: boot-wait health ×3."""

    def test_m1_give_up_sticky_and_critical_suppressed(self):
        plat = FakeCTFdPlatform()
        plat.status = {"status": "running", "entry": "1.2.3.4:5555",
                       "time_left": 120}
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        svc = FakeInstanceService(
            plat, [{"id": 1, "name": "w", "_local_path": str(meta_path)}])
        ka = ik.InstanceKeepAlive(svc, repo=self.repo)
        tr = ka.discover_containers()[0]
        tr.renew_count = ik.WHALE_MAX_RENEWS
        evs1 = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.GIVE_UP)
        self.assertTrue(any(lv == ik.CRITICAL for lv, _m in evs1))
        # tick tiếp: sticky — không PATCH lại, không lặp CRITICAL (<300s)
        evs2 = ka.tick_one(tr)
        self.assertEqual(plat.extends, 0)
        self.assertEqual(tr.state, ik.GIVE_UP)
        self.assertFalse(any(lv == ik.CRITICAL for lv, _m in evs2))

    def test_m2_boot_wait_allows_three_health_checks(self):
        plat = FakeGzctfPlatform()
        plat.status = {"status": "stopped", "entry": None}
        meta_path = self.ws / "Web" / "chall_a" / "metadata.json"
        svc = FakeInstanceService(
            plat, [{"id": 1, "name": "g", "_local_path": str(meta_path)}])
        ka = ik.InstanceKeepAlive(svc, repo=self.repo)
        tr = ka.discover_containers()[0]
        ka.tick_one(tr)                          # DEAD → begin restart
        tr.phase_deadline = time.monotonic() - 1
        ka.tick_one(tr)                          # POST → boot_wait
        self.assertEqual(tr.restart_phase, "boot_wait")
        # health fail lần 1 và 2 → retry sau 10s, CHƯA tính restart fail
        for expected in (1, 2):
            tr.phase_deadline = time.monotonic() - 1
            ka.tick_one(tr)
            self.assertEqual(tr.health_checks, expected)
            self.assertEqual(tr.restart_count, 0)
            self.assertEqual(tr.state, ik.RESTARTING)
        # fail lần thứ 3 → mới chuyển RESTART_BACKOFF (+restart_count)
        tr.phase_deadline = time.monotonic() - 1
        ka.tick_one(tr)
        self.assertEqual(tr.health_checks, 3)
        self.assertEqual(tr.restart_count, 1)
        self.assertEqual(tr.state, ik.RESTART_BACKOFF)
        # container lên ở lần check sau → ALIVE, bộ đếm reset
        plat.status = {"status": "running", "entry": "7.7.7.7:1234"}
        tr.backoff_deadline = time.monotonic() - 1
        ka.tick_one(tr)                          # backoff hết → begin restart lại
        tr.restart_phase = "boot_wait"           # giả lập đã POST xong
        tr.phase_deadline = time.monotonic() - 1
        evs = ka.tick_one(tr)
        self.assertEqual(tr.state, ik.ALIVE)
        self.assertEqual(tr.health_checks, 0)


# ----------------------------------------------------------------------
# rCTF fetch_scoreboard phân trang khi data.total > 100 (deferred item)
# ----------------------------------------------------------------------

class TestRCTFPagination(unittest.TestCase):
    """fetch_scoreboard: data.total > len(leaderboard) → loop GET với
    offset += limit (limit=100), chặn tối đa 10 trang; ghép standings.
    Giữ hành vi cũ khi total <= 100."""

    def _entry(self, i):
        return {"id": i + 1, "name": f"Team{i:04d}", "score": 10000 - i}

    def _make_session(self, total, per_page=100):
        """Server trả đúng per_page dòng mỗi request theo offset; đếm số call."""
        served = {"count": 0}
        offsets = []

        def get(url, *a, **kw):
            if "/api/v1/leaderboard/now" not in url:
                return make_resp(404)
            params = kw.get("params") or {}
            offset = int(params.get("offset", 0))
            served["count"] += 1
            offsets.append(offset)
            # Server thật chỉ trả số dòng còn lại theo total
            n = min(per_page, max(0, total - offset))
            rows = [self._entry(offset + j) for j in range(n)]
            return make_resp(200, json_data={
                "kind": "goodLeaderboard",
                "data": {"total": total, "leaderboard": rows}})

        s = MagicMock()
        s.get.side_effect = get
        s.served_count = served
        s.offsets_seen = offsets
        return s

    def test_total_gt_100_merges_two_pages(self):
        # total=150 → trang 1: 100 dòng (offset=0), trang 2: 50 dòng (offset=100)
        s = self._make_session(total=150, per_page=100)
        p = RCTFPlatform("https://rctf.example.com", s)
        result = p.fetch_scoreboard()
        self.assertEqual(s.served_count["count"], 2)
        self.assertEqual(s.offsets_seen, [0, 100])
        self.assertEqual(len(result["standings"]), 150)
        self.assertEqual(result["total_teams"], 150)
        self.assertEqual(result["standings"][0]["pos"], 1)
        self.assertEqual(result["standings"][99]["pos"], 100)
        self.assertEqual(result["standings"][149]["pos"], 150)
        self.assertEqual(result["standings"][149]["name"], "Team0149")

    def test_total_le_100_keeps_single_request(self):
        # Hành vi cũ: total <= 100 → chỉ 1 request, không loop offset
        s = self._make_session(total=80, per_page=80)
        p = RCTFPlatform("https://rctf.example.com", s)
        result = p.fetch_scoreboard()
        self.assertEqual(s.served_count["count"], 1)
        self.assertEqual(len(result["standings"]), 80)
        self.assertEqual(result["total_teams"], 80)

    def test_hard_cap_ten_pages(self):
        # Server luôn báo còn nữa → dừng ở tối đa 10 trang (10 x 100 = 1000 đội)
        s = self._make_session(total=5000, per_page=100)
        p = RCTFPlatform("https://rctf.example.com", s)
        result = p.fetch_scoreboard()
        self.assertEqual(s.served_count["count"], 10)
        self.assertEqual(len(result["standings"]), 1000)
        self.assertEqual(result["total_teams"], 1000)

    def test_my_team_rank_found_on_second_page(self):
        # Team của mình nằm ở đầu trang 2 (index 100) → pos 101 vẫn được dò ra
        s = self._make_session(total=150, per_page=100)
        p = RCTFPlatform("https://rctf.example.com", s)
        p.ctf_info.team_name = "Team0100"
        result = p.fetch_scoreboard()
        self.assertEqual(result["my_rank"], "101th")
        self.assertEqual(result["my_score"], 10000 - 100)


# ----------------------------------------------------------------------
# Watch panel render — btop layout (header clock + notices +
# mini-scoreboard + footer). Toàn bộ render mock, không gọi mạng.
# ----------------------------------------------------------------------

def _render_plain(renderable, width=100):
    """Render rich renderable ra text thuần (không ANSI) để assert."""
    from rich.console import Console
    buf = io.StringIO()
    console = Console(width=width, file=buf, force_terminal=False,
                      legacy_windows=False)
    console.print(renderable)
    return buf.getvalue()


class TestRenderPanelBtop(TempWorkspaceCase):
    def _svc(self):
        svc = WatchService(str(self.ws), once=True, use_live_ui=False)
        svc.platform = FakeWatchPlatform()
        now = _dt.datetime.now(_dt.timezone.utc)
        svc.guard = WindowGuard(now - _dt.timedelta(hours=1),
                                now + _dt.timedelta(hours=4))
        return svc

    # -- countdown format + trạng thái window --------------------------
    def test_render_contains_countdown_and_status(self):
        svc = self._svc()
        panel = svc._render_panel([])
        self.assertIsNotNone(panel)
        text = _render_plain(panel)
        # fmt_countdown: "3h59m59s" hoặc "59m59s"
        self.assertIsNotNone(
            re.search(r"\d+h\d{2}m\d{2}s|\d{1,2}m\d{2}s", text),
            f"không thấy countdown trong:\n{text}")
        self.assertIn("🔴 LIVE", text)
        self.assertIn("kết thúc sau", text)
        self.assertIn("PTIT CTF 2026", text)   # tên giải từ challenges.json

    def test_render_before_state_counts_to_start(self):
        svc = self._svc()
        now = _dt.datetime.now(_dt.timezone.utc)
        svc.guard = WindowGuard(now + _dt.timedelta(minutes=30),
                                now + _dt.timedelta(hours=4))
        text = _render_plain(svc._render_panel([]))
        self.assertIn("⏳", text)
        self.assertIn("bắt đầu sau", text)

    def test_render_clock_skew_icon_when_offset_present(self):
        svc = self._svc()
        self.assertNotIn("🕐", _render_plain(svc._render_panel([])))
        svc.guard.apply_server_offset(150.0)
        self.assertIn("🕐", _render_plain(svc._render_panel([])))

    def test_render_ended_state_dimmed(self):
        svc = self._svc()
        now = _dt.datetime.now(_dt.timezone.utc)
        svc.guard = WindowGuard(now - _dt.timedelta(hours=5),
                                now - _dt.timedelta(hours=2))
        text = _render_plain(svc._render_panel([]))
        self.assertIn("✅ ended", text)
        self.assertNotIn("🔴 LIVE", text)

    # -- footer bindings ------------------------------------------------
    def test_footer_bindings_present(self):
        svc = self._svc()
        text = _render_plain(svc._render_panel([], width=100))
        for binding in ("q thoát", "p pause", "r refresh-now"):
            self.assertIn(binding, text,
                          f"thiếu binding '{binding}' trong footer")

    # -- mini-scoreboard: top-5 + meter gradient chuẩn hoá theo #1 ------
    def test_mini_scoreboard_top5_and_full_meter_for_first(self):
        svc = self._svc()
        svc._mini_sb_rows = [{"pos": i + 1, "name": f"Team{i}",
                              "score": 500 - 100 * i} for i in range(6)]
        text = _render_plain(svc._render_panel([], width=100))
        for i in range(5):
            self.assertIn(f"Team{i}", text)
        self.assertNotIn("Team5", text)          # chỉ top-5
        meter_w = 16
        self.assertIn("▰" * meter_w, text)       # #1 → meter đầy (100%)

    def test_mini_scoreboard_empty_placeholder_when_no_data(self):
        svc = self._svc()
        text = _render_plain(svc._render_panel([], width=100))
        self.assertIn("chưa có dữ liệu", text)

    # -- challenges summary ----------------------------------------------
    def test_challenges_summary_new_count_this_tick(self):
        svc = self._svc()
        svc._known_chall_count = 3
        first = _render_plain(svc._render_panel([]))   # baseline
        self.assertNotIn("✨ +", first)
        svc._known_chall_count = 5                     # tick mới +2 bài
        second = _render_plain(svc._render_panel([]))
        self.assertIn("✨ +2", second)

    # -- skip-refresh theo snapshot --------------------------------------
    def test_snapshot_skip_refresh_when_data_unchanged(self):
        import ctf_downloader.services.watch_service as wsm
        svc = self._svc()
        fake_live = MagicMock()
        fake_live.update.reset_mock()
        svc._live = fake_live
        with patch.object(wsm.time, "time", return_value=1000.0):
            svc._refresh_live(["📢 thông báo"])   # lần đầu → update
            svc._refresh_live([])                  # cùng giây + cùng dữ liệu → skip
            self.assertEqual(fake_live.update.call_count, 1)
        with patch.object(wsm.time, "time", return_value=1005.0):
            svc._refresh_live([])                  # giây mới (đồng hồ tick) → update
            self.assertEqual(fake_live.update.call_count, 2)

    def test_snapshot_skip_redraws_on_data_change_same_second(self):
        import ctf_downloader.services.watch_service as wsm
        svc = self._svc()
        fake_live = MagicMock()
        svc._live = fake_live
        with patch.object(wsm.time, "time", return_value=1000.0):
            svc._refresh_live(["📢 a"])
            svc._refresh_live(["📢 b"])            # feed đổi dù cùng giây
            self.assertEqual(fake_live.update.call_count, 2)

    # -- degrade: width < 80 bỏ scoreboard -------------------------------
    def test_degrade_narrow_drops_scoreboard_keeps_notices_countdown(self):
        svc = self._svc()
        svc._mini_sb_rows = [{"pos": i + 1, "name": f"T{i}", "score": 100 * i}
                             for i in range(5)]
        svc._feed.append("📢 hello")
        wide = _render_plain(svc._render_panel([], width=100), width=100)
        narrow = _render_plain(svc._render_panel([], width=70), width=70)
        self.assertIn("🏆", wide)
        self.assertIn("▰", wide)
        self.assertNotIn("🏆", narrow)
        self.assertNotIn("▰", narrow)
        # notices + countdown vẫn còn ở bản degrade
        self.assertIn("📢 hello", narrow)
        self.assertIn("🔴 LIVE", narrow)


# ----------------------------------------------------------------------
# Spec-gap §4 (challenge-status-model): tick watch đồng bộ solve attribution
# qua CÙNG đường pull (PullService.sync_solve_attribution)
# ----------------------------------------------------------------------

class AttributedWatchPlatform(FakeWatchPlatform):
    """FakeWatchPlatform + fetch_solve_attribution dữ liệu server giả."""

    def __init__(self, attr=None, fail=False):
        super().__init__()
        self.attr = dict(attr or {})
        self.fail = fail
        self.attr_calls = []

    def fetch_solve_attribution(self, challenge_ids):
        self.attr_calls.append(list(challenge_ids))
        if self.fail:
            raise RuntimeError("server 500")
        return dict(self.attr)


class TestAttributionWatchTick(TempWorkspaceCase):
    def _svc(self, platform):
        svc = WatchService(str(self.ws), once=True, use_live_ui=False)
        svc.platform = platform
        svc.state = svc.state_store.load()
        svc.scheduler.register("challenges", 120)
        return svc

    def _meta_path(self):
        for p in self.repo.iter_challenges():
            return p
        return None

    def test_one_tick_fetches_attribution_and_raises_by_team(self):
        # Key = ĐÚNG kiểu id truyền vào (hợp đồng platform: trả nguyên `orig`)
        platform = AttributedWatchPlatform({1: {"by_me": False,
                                                "by_team": True}})
        svc = self._svc(platform)
        lines = svc._tick_challenges()
        # 1 tick → đúng 1 lần gọi, với đúng danh sách id local
        self.assertEqual(len(platform.attr_calls), 1)
        self.assertIn(1, platform.attr_calls[0])
        # by_team cập nhật qua CÙNG helper pull (update_status + synced_at)
        st = self.repo.read_status(self._meta_path())
        self.assertEqual(st["solve"], "solved_by_team")
        self.assertTrue(st.get("synced_at"))
        self.assertTrue(any("attribution" in ln.lower() for ln in lines))

    def test_never_downgrades_local_higher_state(self):
        meta = self._meta_path()
        self.repo.update_status(
            meta, lambda st: {**st, "solve": "solved_by_me"})
        platform = AttributedWatchPlatform({1: {"by_me": False,
                                                "by_team": True}})
        svc = self._svc(platform)
        svc._tick_challenges()
        st = self.repo.read_status(meta)
        self.assertEqual(st["solve"], "solved_by_me")   # nguyên tắc chỉ-nâng
        # Review finding: data không đổi (không nâng gì) → KHÔNG stamp
        # synced_at giả "tươi", không rewrite status.json mỗi tick.
        self.assertIsNone(st.get("synced_at"))

    def test_fetch_raise_never_crashes_and_logs_warning(self):
        import ctf_downloader.services.watch_service as wsm
        platform = AttributedWatchPlatform(fail=True)
        svc = self._svc(platform)
        with patch.object(wsm.Logger, "warning") as mwarn:
            lines = svc._tick_challenges()   # never-raise: không crash
        self.assertEqual(len(platform.attr_calls), 1)
        mwarn.assert_called()                # warning tiếng Việt đã log
        self.assertIsInstance(lines, list)

    def test_platform_without_support_skips_silently(self):
        platform = FakeWatchPlatform()       # không có fetch_solve_attribution
        svc = self._svc(platform)
        lines = svc._tick_challenges()
        self.assertFalse(any("attribution" in ln.lower() for ln in lines))

    def test_not_window_active_no_extra_call(self):
        platform = AttributedWatchPlatform({"1": {"by_team": True}})
        svc = self._svc(platform)
        svc._tick_challenges(window_active=False)   # giải đã kết thúc
        self.assertEqual(platform.attr_calls, [])


if __name__ == "__main__":
    unittest.main()
