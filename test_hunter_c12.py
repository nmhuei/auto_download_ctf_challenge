"""HUNTER cycle 12 — deep-edge tests cho 4 vùng chưa bấm biên:
  1. interactive_menu (option lạ / EOF / nav vòng lại / workspace rỗng)
  2. sniper_service   (window boundary, --start-at quá khứ, multi-target, Ctrl-C)
  3. instance_keepalive state machine 8 state (renew/restart/GIVE_UP)
  4. web_dashboard v2 concurrency (2 POST song song, GET trong lúc POST)

Quy ước: test có comment ``# BUG-DEMO`` là test CHỦ Ý FAIL trên code hiện tại
(chứng minh bug thật). Còn lại phải PASS (= hành vi đúng / documentation).

Không đụng mạng thật ngoài localhost; không mock-file ngoài tmp_path.
"""
import json
import threading
import time
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctf_downloader.services import sniper_service as sn
from ctf_downloader.services.instance_keepalive import (
    InstanceKeepAlive, InstanceTracker,
    ALIVE, DUE_SOON, RENEW_FAILED, DEAD, RESTARTING, RESTART_BACKOFF,
    GIVE_UP, RENEW_MAX_ATTEMPTS, WHALE_MAX_RENEWS,
)
from ctf_downloader.services.web_dashboard import WebDashboard


# ====================================================================== #
# Fakes dùng chung
# ====================================================================== #
class FakeMenuConsole:
    """Rich-console thay thế: print nuốt, input rút từ script; hết -> EOFError."""

    def __init__(self, script):
        self.script = list(script)
        self.printed = []

    def print(self, *a, **k):
        self.printed.append(str(a))

    @property
    def width(self):
        return 80

    def input(self, prompt=""):
        if not self.script:
            raise EOFError("stdin exhausted")
        return str(self.script.pop(0))


def make_menu_app(monkeypatch, script, tmp_home=None, dash_stats_total=3):
    """CTFInteractiveConsole bỏ __init__, console giả, lưu-config bị chặn."""
    import ctf_downloader.interactive_menu as im

    con = FakeMenuConsole(script)
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    saved = {}

    def _fake_update(mutator):
        # Review c18-2: menu ghi global config qua update_global_config
        # (RMW trong khóa) thay vì save_global_config. Mirror đúng ngữ nghĩa
        # — chạy mutator trên state fresh và trả state sau ghi — nhưng
        # NHẬN DIỆN: không bao giờ chạm config.json thật của user.
        state = {}
        result = mutator(state)
        if result is not None:
            saved.update(default_workspace=result.get("default_workspace"))
        return result

    monkeypatch.setattr(im, "update_global_config", _fake_update)

    class StubDash:
        def __init__(self, p):
            self.p = p

        def get_summary_stats(self):
            return {"total_challenges": dash_stats_total, "title": "T",
                    "platform": "generic", "solved_challenges": 1}

    monkeypatch.setattr(im, "CTFDashboard", StubDash)

    if tmp_home is not None:
        monkeypatch.setenv("HOME", str(tmp_home))

    app = object.__new__(im.CTFInteractiveConsole)
    app.config = {}
    app.workspace_path = "/tmp/default-ws"
    app.cookie = None
    app.token = None
    app._load_saved_auth = lambda: None
    app._print_header = lambda: None
    return app, con, saved


class FakeSubmitter:
    """SubmitService giả: script kết quả lần lượt; ghi submit_history như thật."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.submit_history = []
        self.platform = SimpleNamespace(last_verdict=None)

    def submit(self, challenge, flag, force=False):
        self.calls.append((challenge, flag, force))
        item = self.script.pop(0)
        if isinstance(item, BaseException):   # KeyboardInterrupt là BaseException
            raise item
        kind, message = item  # kind: correct|incorrect|ratelimited|unknown
        if kind in ("correct", "incorrect"):
            self.submit_history.append({"flag": flag, "result": kind})
        elif kind == "ratelimited":
            self.platform.last_verdict = "ratelimited"
        return True, message


class FakeRepo:
    def __init__(self, root: Path, challenges=None):
        self.root = Path(root)
        self._ch = challenges or {}

    def read_challenges(self):
        return self._ch

    def read_status(self, meta_path):
        return {"flag": {"value": None, "state": "none"}}


class SniperClock:
    """Thay module time của sniper: time()/sleep()/strftime()/gmtime().
    sleep() KHÔNG ngủ thật — tăng đồng hồ ảo (mode='advance') hoặc raise."""

    def __init__(self, t0):
        import time as real_time
        self._real = real_time
        self.t = float(t0)
        self.mode = "advance"
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(float(s))
        if self.mode == "raise":
            raise KeyboardInterrupt()
        self.t += max(0.0, float(s))

    def strftime(self, fmt, tt):
        return self._real.strftime(fmt, tt)

    def gmtime(self, s):
        return self._real.gmtime(s)


class FakeInstancePlatform:
    """Platform adapter giả cho keep-alive: status/extend/start/stop đều đếm."""

    def __init__(self, time_left=120.0, extend_ok=False, extend_msg="503 upstream",
                 start_ok=False):
        self.time_left = float(time_left)
        self.extends = 0
        self.starts = 0
        self.stops = 0
        self.extend_ok = extend_ok
        self.extend_msg = extend_msg
        self.start_ok = start_ok
        self.api_status = "running"

    def get_instance_status(self, cid):
        if self.api_status == "stopped":
            return {"status": "stopped", "entry": None}
        return {"status": self.api_status, "entry": "127.0.0.1:9999",
                "time_left": self.time_left}

    def extend_instance(self, cid):
        self.extends += 1
        return self.extend_ok, ("" if self.extend_ok else self.extend_msg)

    def start_instance(self, cid):
        self.starts += 1
        return self.start_ok, {"message": ""}

    def stop_instance(self, cid):
        self.stops += 1
        return True, "ok"


class FakeSvc:
    def __init__(self, platform):
        self.platform = platform
        self.repo = None

    def list_containers(self):
        return []


# ====================================================================== #
# VÙNG 1 — interactive_menu
# ====================================================================== #
class TestInteractiveMenu:
    def test_invalid_option_then_back_nav_then_quit(self, monkeypatch):
        """Option lạ ('zzz') chỉ warning; '2'->'0' nav vòng lại sạch; '0' thoát."""
        app, con, _ = make_menu_app(monkeypatch, ["2", "0", "zzz", "0"])
        app.run()  # không được raise, phải thoát sạch
        assert con.script == []

    def test_empty_workspace_switch_custom_missing_path(self, monkeypatch, tmp_path):
        """Workspace rỗng (không có ~/Workspace/CTF): nhập path không tồn tại
        -> quay lại menu, workspace giữ nguyên, không crash."""
        app, con, _ = make_menu_app(monkeypatch, ["2", "/khong-ton-tai/x", "0"],
                                    tmp_home=tmp_path)
        app.run()
        assert app.workspace_path == "/tmp/default-ws"

    def test_eof_on_main_prompt_exits_gracefully(self, monkeypatch):
        """BUG-DEMO (chủ ý FAIL): Ctrl-D/EOF ở prompt chính phải thoát SẠCH
        như chọn '0', hiện tại EOFError nổ traceback ra ngoài run()."""
        app, con, _ = make_menu_app(monkeypatch, [])
        raised = None
        try:
            app.run()
        except EOFError as exc:
            raised = exc
        assert raised is None, (
            f"BUG C12-M1: EOF ở prompt menu làm crash CLI "
            f"(EOFError thoát ra ngoài run()): {raised!r}")

    def test_zero_zero_selects_last_workspace_silently(self, monkeypatch, tmp_path):
        """BUG-DEMO (chủ ý FAIL): nhập '00' (hoặc '-0','-1') rơi vào index ÂM
        -> lặng lẽ chọn workspace CUỐI + ghi vào config, thay vì báo lựa chọn
        không hợp lệ."""
        ctf_dir = tmp_path / "Workspace" / "CTF"
        (ctf_dir / "wsAAA").mkdir(parents=True)
        (ctf_dir / "wsBBB").mkdir(parents=True)
        app, con, saved = make_menu_app(
            monkeypatch, ["2", "00", "0"], tmp_home=tmp_path)
        app.workspace_path = str(ctf_dir / "wsAAA")
        app.run()
        assert app.workspace_path == str(ctf_dir / "wsAAA"), (
            f"BUG C12-M2: '00' chuyển lặng lẽ sang workspace khác "
            f"(workspace_path={app.workspace_path}, saved={saved})")


# ====================================================================== #
# VÙNG 2 — sniper_service
# ====================================================================== #
START_TS = 3000000000.0  # epoch cố định, tương lai xa


class TestSniperService:
    def _svc(self, tmp_path, script, challenges=None):
        repo = FakeRepo(tmp_path, challenges or {})
        sub = FakeSubmitter(script)
        return sn.SniperService(repo, sub), sub

    def test_load_targets_sort_and_bad_entries(self, tmp_path):
        svc, _ = self._svc(tmp_path, [])
        p = tmp_path / "sniper.json"
        p.write_text(json.dumps([
            {"challenge": "late", "flag": "F3", "delay_seconds": 5},
            {"challenge": "first", "flag": "F1"},
            {"bad": 1},
            {"challenge": "  ", "flag": "F"},
            {"challenge": "mid", "flag": "F2", "delay_seconds": 2},
            {"challenge": "zerodelay", "flag": "F0", "delay_seconds": "xx"},
        ]), encoding="utf-8")
        ts = svc.load_targets(p)
        assert [t["challenge"] for t in ts] == [
            "first", "zerodelay", "mid", "late"]
        assert ts[1]["delay_seconds"] == 0.0

    def test_resolve_start_event_window_without_end(self, tmp_path):
        """Window có start nhưng KHÔNG end (và ngược lại thiếu start -> cần
        --start-at): resolve đọc đúng start, không đụng end."""
        svc, _ = self._svc(tmp_path, [], challenges={
            "ctf_info": {"event_window": {"start": str(int(START_TS))}}})
        assert svc.resolve_start() == START_TS
        svc2, _ = self._svc(tmp_path, [], challenges={
            "ctf_info": {"event_window": {"end": "1"}}})
        assert svc2.resolve_start() is None

    def test_fire_exact_boundary_and_multi_target_order(self, tmp_path, monkeypatch):
        """Giờ G ±0: delay0 bắn ĐÚNG tại boundary; delay5 chỉ bắn sau khi đủ
        mốc (một wake duy nhất, không bắn sớm). Window đọc từ repo (không end)."""
        svc, sub = self._svc(tmp_path, [
            ("correct", "ok-a"), ("correct", "ok-b")],
            challenges={"ctf_info": {
                "event_window": {"start": str(int(START_TS))}}})
        (tmp_path / "sniper.json").write_text(json.dumps([
            {"challenge": "b", "flag": "FB", "delay_seconds": 5},
            {"challenge": "a", "flag": "FA"},
        ]), encoding="utf-8")
        clock = SniperClock(START_TS)  # đứng ĐÚNG tại giờ G
        monkeypatch.setattr(sn, "time", clock)
        summary = svc.run(poll_interval=10)
        assert [c for c, _, _ in sub.calls] == ["a", "b"]      # đúng thứ tự delay
        assert clock.t == START_TS + 5.0                        # không bắn sớm
        assert len(summary["solved"]) == 2 and not summary["aborted"]

    def test_start_at_in_the_past_fires_immediately(self, tmp_path, monkeypatch):
        """--start-at quá khứ: không chờ, bắn ngay mọi target due, không crash."""
        svc, sub = self._svc(tmp_path, [("correct", "ok")])
        (tmp_path / "sniper.json").write_text(json.dumps(
            [{"challenge": "a", "flag": "FA"}]), encoding="utf-8")
        clock = SniperClock(START_TS + 9999)
        monkeypatch.setattr(sn, "time", clock)
        summary = svc.run(poll_interval=5, start_at=str(int(START_TS - 5000)))
        assert len(sub.calls) == 1 and len(summary["solved"]) == 1

    def test_ctrl_c_during_prewindow_wait_aborts_cleanly(self, tmp_path, monkeypatch):
        """BUG-DEMO (chủ ý FAIL): docstring hứa 'Ctrl-C dừng sniper sạch' nhưng
        vòng chờ giờ G nằm NGOÀI try — Ctrl-C khi còn canh giờ G nổ traceback."""
        svc, _ = self._svc(tmp_path, [])
        (tmp_path / "sniper.json").write_text(json.dumps(
            [{"challenge": "a", "flag": "FA"}]), encoding="utf-8")
        clock = SniperClock(START_TS - 1000)
        clock.mode = "raise"
        monkeypatch.setattr(sn, "time", clock)
        raised = None
        try:
            svc.run(poll_interval=10, start_at=str(int(START_TS)))
        except KeyboardInterrupt as exc:
            raised = exc
        assert raised is None, (
            f"BUG C12-S1: Ctrl-C lúc chờ giờ G crash thay vì abort sạch "
            f"({raised!r}); vòng chờ pre-window nằm ngoài try ở sniper_service.py")

    def test_ctrl_c_during_fire_loop_caught(self, tmp_path, monkeypatch):
        """Đối chứng: Ctrl-C GIỮA vòng bắn thì bị bắt sạch -> aborted=True."""
        svc, _ = self._svc(tmp_path, [KeyboardInterrupt("sim")])
        (tmp_path / "sniper.json").write_text(json.dumps(
            [{"challenge": "a", "flag": "FA"}]), encoding="utf-8")
        clock = SniperClock(START_TS + 10)
        monkeypatch.setattr(sn, "time", clock)
        summary = svc.run(poll_interval=5, start_at=str(int(START_TS)))
        assert summary["aborted"] is True
        assert len(summary["pending"]) == 1


# ====================================================================== #
# VÙNG 3 — instance_keepalive state machine
# ====================================================================== #
class TestKeepAliveStateMachine:
    def test_renew_success_right_at_deadline_recovers(self):
        """Renew thành công ngay sát mực chết (remaining < SAFETY_MARGIN,
        lần thử đầu) -> ALIVE + neo lifetime mới."""
        plat = FakeInstancePlatform(time_left=45.0)

        def extend(cid):
            plat.extends += 1
            plat.time_left = 3600.0          # gia hạn thành công
            return True, ""

        plat.extend_instance = extend
        ka = InstanceKeepAlive(FakeSvc(plat), repo=None)
        tr = InstanceTracker("c1", "c1", platform_kind="gzctf")
        events = ka.tick_one(tr)
        assert tr.state == ALIVE
        assert tr.est_lifetime == 3600.0
        assert any(lvl == "info" for lvl, _ in events)

    def test_repeated_nonfatal_renew_failure_gives_up_after_max(self):
        """BUG-DEMO (chủ ý FAIL): renew lỗi KHÔNG-fatal lặp lại phải GIVE_UP
        sau RENEW_MAX_ATTEMPTS lần (spec: RENEW_MAX_ATTEMPTS=4, SAFETY_MARGIN
        ngừng retry). Thực tế tick_one ghi đè state=DUE_SOON ngay trước khi
        _try_renew kiểm tra 'state == RENEW_FAILED' => cả 2 nhánh give-up là
        DEAD CODE: gzctf bị hammer extend mỗi ~5s vô hạn + WARNING spam mỗi
        tick (escalation key chứa counter nên không bao giờ suppress)."""
        plat = FakeInstancePlatform(time_left=120.0, extend_ok=False,
                                    extend_msg="503 upstream timeout")
        ka = InstanceKeepAlive(FakeSvc(plat), repo=None)
        tr = InstanceTracker("c1", "c1", platform_kind="gzctf")
        warnings_seen = 0
        for _ in range(10):
            events = ka.tick_one(tr)
            warnings_seen += sum(1 for lvl, _ in events if lvl == "warning")
        assert plat.extends <= RENEW_MAX_ATTEMPTS, (
            f"BUG C12-K1: extend bị gọi {plat.extends} lần (> "
            f"RENEW_MAX_ATTEMPTS={RENEW_MAX_ATTEMPTS}) — nhánh give-up dead-code")
        assert tr.state == GIVE_UP, f"state vẫn {tr.state}, không bao giờ GIVE_UP"
        assert warnings_seen <= 6, (
            f"BUG C12-K1b: {warnings_seen} WARNING/10 tick — escalation "
            f"repeat-suppression bị vô hiệu vì message chứa counter đổi mỗi lần")

    def test_restart_backoff_deadline_then_recover_alive(self):
        """RESTART_BACKOFF hết hạn -> _begin_restart lại -> boot OK -> ALIVE."""
        plat = FakeInstancePlatform(time_left=1800.0, start_ok=False)
        ka = InstanceKeepAlive(FakeSvc(plat), repo=None)
        tr = InstanceTracker("c1", "c1", platform_kind="gzctf")
        plat.api_status = "stopped"
        ka.tick_one(tr)                       # DEAD -> _begin_restart (cooldown)
        assert tr.state == RESTARTING
        tr.phase_deadline = 0.0               # ép hết cooldown
        plat.start_ok = True
        ka.tick_one(tr)                       # cooldown -> boot_wait (POST ok)
        assert tr.restart_phase == "boot_wait"
        tr.phase_deadline = 0.0
        plat.api_status = "running"           # health-check thấy sống
        events = ka.tick_one(tr)
        assert tr.state == ALIVE and any(lvl == "info" for lvl, _ in events)
        ka.tick_one(tr)                       # tick observe thường -> entry cập nhật
        assert tr.entry == "127.0.0.1:9999"

    def test_max_restarts_exhausted_gives_up_terminal(self):
        """3 lần restart vẫn chết -> GIVE_UP; reconcile sau đó không đụng
        platform nữa (thoát sạch vòng loop, không thao tác thêm)."""
        plat = FakeInstancePlatform(time_left=60.0, start_ok=False)
        ka = InstanceKeepAlive(FakeSvc(plat), repo=None)
        tr = InstanceTracker("c1", "c1", platform_kind="gzctf")
        plat.api_status = "stopped"
        for _ in range(20):
            ka.tick_one(tr)
            tr.phase_deadline = 0.0           # ép mọi phase/backoff hết hạn ngay
            tr.backoff_deadline = 0.0
            if tr.state == GIVE_UP:
                break
        assert tr.state == GIVE_UP
        starts, stops = plat.starts, plat.stops
        for _ in range(5):
            ka.tick_one(tr)
            tr.phase_deadline = 0.0
            tr.backoff_deadline = 0.0
        assert (plat.starts, plat.stops) == (starts, stops), (
            "GIVE_UP xong vẫn còn thao tác start/stop trên platform")

    def test_give_up_sticky_while_running_no_more_renew(self):
        """Whale đã hết lượt renew (renew_count == WHALE_MAX_RENEWS) -> tick
        chuyển GIVE_UP mà KHÔNG extend; các tick sau khi vẫn running: không
        extend, không event (circuit breaker sticky trong reconcile loop)."""
        plat = FakeInstancePlatform(time_left=100.0, extend_ok=True)

        def extend(cid):
            plat.extends += 1
            return True, ""

        plat.extend_instance = extend
        ka = InstanceKeepAlive(FakeSvc(plat), repo=None)
        tr = InstanceTracker("w1", "w1", platform_kind="whale")
        tr.renew_count = WHALE_MAX_RENEWS     # đã đốt hết 5 lượt từ trước
        events = ka.tick_one(tr)
        assert tr.state == GIVE_UP and plat.extends == 0
        assert any(lvl == "critical" for lvl, _ in events)
        assert ka.tick_one(tr) == [] and plat.extends == 0


# ====================================================================== #
# VÙNG 4 — web_dashboard v2 concurrency
# ====================================================================== #
STATS = {"title": "T", "platform": "gzctf", "user": "u", "team": None,
         "solved_challenges": 1, "total_challenges": 2, "earned_points": 100,
         "total_points": 200, "completion_rate": 50.0, "categories": {}}


class SlowSubmitter:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()

    def submit(self, challenge, flag, force=False):
        with self.lock:
            self.calls.append((challenge, flag))
        time.sleep(self.delay)
        return True, "correct"


class CannedDashboard(WebDashboard):
    def collect(self):
        return {"stats": STATS, "challenges": [], "window": ""}


class HeaderStub(dict):
    def get(self, k, default=None):  # http.headers-like, case-insensitive đủ dùng
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return default


def _post_payload(challenge="web/1", flag="FLAG{x}"):
    return (json.dumps({"challenge": challenge, "flag": flag})
            .encode("utf-8"))


class TestWebDashboardConcurrency:
    def test_two_parallel_posts_same_challenge_second_is_429(self):
        """2 POST /api/submit SONG SONG cùng challenge: rate-limit (mutex
        check-and-set) phải chặn ĐÚNG con thứ hai -> 1×200 + 1×429,
        SubmitService.submit chỉ chạy tối đa 1 lần."""
        sub = SlowSubmitter(delay=0.08)
        wd = CannedDashboard(SimpleNamespace(root="/tmp"),
                             submit_factory=lambda: sub)
        hdr = HeaderStub({"X-Requested-With": "XMLHttpRequest"})
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            results.append(wd.handle_submit_request(_post_payload(), hdr))

        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        codes = sorted(code for code, _, _ in results)
        assert codes == [200, 429], f"codes={codes}"
        assert len(sub.calls) <= 1

    def test_get_while_post_in_flight_no_deadlock(self):
        """GET / và GET /api/status.json xử lý XONG trong khi POST đang chờ
        submit (0.6s) — ThreadingHTTPServer + không khoá chung -> không deadlock."""
        sub = SlowSubmitter(delay=0.6)
        wd = CannedDashboard(SimpleNamespace(root="/tmp"),
                             submit_factory=lambda: sub)
        httpd = wd.make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            post_done = threading.Event()

            def do_post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/submit",
                    data=_post_payload(), method="POST",
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
                post_done.set()

            pt = threading.Thread(target=do_post)
            pt.start()
            time.sleep(0.15)                    # POST đang treo trong submit()
            assert not post_done.is_set()
            t0 = time.monotonic()
            html_body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5).read()
            api_body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status.json", timeout=5).read()
            get_elapsed = time.monotonic() - t0
            assert html_body and b"CTF Dashboard" in html_body
            assert json.loads(api_body.decode("utf-8"))["total_challenges"] == 2
            assert get_elapsed < 2.0, "GET bị chặn — nghi deadlock với POST"
            assert not post_done.is_set()       # GET xong TRƯỚC khi POST xong
            pt.join(timeout=5)
            assert post_done.is_set()
        finally:
            httpd.shutdown()
            httpd.server_close()
