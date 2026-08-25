"""HUNTER cycle 13 — xác nhận biên cho loạt fix vừa land (4f7cb00, 003fb66):

  1. utils/logger.py       — escape semantics (_safe_body) sau C11-04
  2. instance_keepalive.py — was_renew_failed / GIVE_UP sticky / escalate key (C12-K1, M-1)
  3. writeup_exporter.py   — prune pack_dir về đúng tập subdir lần chạy (C11-03)
  4. interactive_menu.py   — index guard workspace + container (C12-M2)
  5. sniper_service.py     — Ctrl-C lúc ĐANG bắn (không chỉ lúc canh giờ — C12-S1 mở rộng)
  6. watch_service.py      — checkpoint isolate khỏi try của task (C11-02)

Quy ước như các cycle trước: test có comment ``# BUG-DEMO`` là test CHỦ Ý
FAIL trên code hiện tại (chứng minh bug thật); còn lại phải PASS
(= hành vi đúng / documentation hợp đồng).

Không đụng mạng thật; mock mọi platform/scheduler; file tạm trong tmp_path.
"""
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.errors import MarkupError

from ctf_downloader.ui import theme as ui_theme
from ctf_downloader.utils import logger as logger_mod
from ctf_downloader.services import instance_keepalive as ik
from ctf_downloader.services.instance_keepalive import (
    InstanceKeepAlive, InstanceTracker,
    ALIVE, DUE_SOON, RENEW_FAILED, RESTARTING, GIVE_UP,
    RENEW_MAX_ATTEMPTS, WHALE_MAX_RENEWS,
)
from ctf_downloader.services import sniper_service as sn
from ctf_downloader.services.writeup_exporter import WriteupExporter


# ====================================================================== #
# VÙNG 1 — Logger escape semantics (C11-04)
# ====================================================================== #
def _capture(fn, *args, **kwargs):
    """Chạy một Logger.* qua console thay thế (theme thật, width lớn để
    không wrap làm hỏng assert chuỗi dài). Trả (raw, plain-no-ansi)."""
    buf = __import__("io").StringIO()
    con = Console(file=buf, width=40000, force_terminal=False,
                  highlight=False, theme=ui_theme.load_theme(None))
    old = logger_mod.console
    logger_mod.console = con
    try:
        fn(*args, **kwargs)
    finally:
        logger_mod.console = old
    return buf.getvalue(), buf.getvalue()


class TestLoggerEscapeSemantics:
    """Hợp đồng: msg là DỮ LIỆU → mặc định escape, không bao giờ crash;
    markup=True là đường chủ ý → hành vi rich chuẩn."""

    def test_percent_signs_are_format_safe(self):
        # % không thuộc markup rich và print không %-format → phải nguyên văn.
        msg = "100% [accent]giả-tag[/accent] %s %d {{braces}}"
        raw, _ = _capture(logger_mod.Logger.info, msg)
        assert msg in raw            # hiện NGUYÊN VĂN, không nuốt chữ
        assert "giả-tag" in raw

    def test_none_and_bytes_as_msg_degrade_not_crash(self):
        _, p1 = _capture(logger_mod.Logger.info, None)
        assert "None" in p1          # str(None) — degrade đẹp
        _, p2 = _capture(logger_mod.Logger.warning, b"\x00abc")
        assert "b'" in p2            # str(bytes) — không raise TypeError

    def test_very_long_msg_10kb_no_crash_tail_intact(self):
        marker = "TAIL_" + "z" * 10000
        _, plain = _capture(logger_mod.Logger.error, "head " + marker)
        assert marker in plain       # 10KB đi trọn, width 40000 không wrap

    def test_nested_bracket_quote_verbatim(self):
        _, plain = _capture(logger_mod.Logger.info, "nested [a[b]] c]")
        assert "nested [a[b]] c]" in plain   # không MarkupError, không mất chữ

    def test_markup_true_unclosed_open_tag_renders_like_rich(self):
        # Đường CHỦ Ý: tag mở không đóng — rich tự đóng cuối dòng, KHÔNG raise
        # (documentation: giống rich thường, không thêm gì đặc biệt).
        _, plain = _capture(
            logger_mod.Logger.info, "hello [bold]world", markup=True)
        assert "hello world" in plain
        assert "[bold]" not in plain  # đã được parse thành style

    def test_markup_true_stray_close_raises_by_contract(self):
        # Hợp đồng markup=True: tag sai vẫn nổ MarkupError như rich thường —
        # call-site chủ ý PHẢI tự đảm bảo tag đúng. Đây là hành vi thiết kế.
        with pytest.raises(MarkupError):
            _capture(logger_mod.Logger.error, "team[/]name", markup=True)


# ====================================================================== #
# VÙNG 2 — Keepalive state machine sau C12-K1/M-1
# ====================================================================== #
class FakeClock:
    def __init__(self, t0=1000.0):
        self.t = float(t0)

    def advance(self, s):
        self.t += float(s)


class ScriptedPlatform:
    """Status/extend/start/stop điều khiển được từng tick."""

    def __init__(self, time_left=400.0, extend_ok=False, extend_msg="503"):
        self.extends = 0
        self.starts = 0
        self.stops = 0
        self.extend_ok = extend_ok
        self.extend_msg = extend_msg
        self.status_now = {"status": "running",
                           "entry": "127.0.0.1:9999",
                           "time_left": float(time_left)}

    def get_instance_status(self, cid):
        return dict(self.status_now)

    def extend_instance(self, cid):
        self.extends += 1
        return self.extend_ok, ("" if self.extend_ok else self.extend_msg)

    def start_instance(self, cid):
        self.starts += 1
        return True, {"message": ""}

    def stop_instance(self, cid):
        self.stops += 1
        return True, "ok"


def make_ka(platform, clock):
    """InstanceKeepAlive với _now neo vào FakeClock; svc tối giản."""
    svc = SimpleNamespace(platform=platform, repo=None,
                          list_containers=lambda: [])
    ka = InstanceKeepAlive(svc, repo=None)
    monkey_now = lambda: clock.t
    return ka, monkey_now


def tick(ka, tracker, clock, monkey_now, monkeypatch, advance=60.0):
    clock.advance(advance)
    monkeypatch.setattr(ik, "_now", monkey_now)
    return ka.tick_one(tracker)


class TestKeepaliveStateMachine:
    def test_give_up_sticky_while_container_alive(self, monkeypatch):
        """M-1/C12-K1: whale hết lượt renew → GIVE_UP phải STICKY khi
        container còn running: không extend nữa, không hồi phục nhầm."""
        clock = FakeClock()
        pf = ScriptedPlatform(time_left=50.0, extend_ok=True)
        ka, now = make_ka(pf, clock)
        tr = InstanceTracker(1, "w", platform_kind="whale")
        tr.renew_count = WHALE_MAX_RENEWS      # circuit breaker OPEN
        tr.state = GIVE_UP
        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == GIVE_UP             # không hồi phục từ GIVE_UP
        assert pf.extends == 0                 # không gọi PATCH renew
        assert evs == []
        # api 'unknown' (proxy 502) cũng không được revive GIVE_UP
        pf.status_now = {"status": "unknown"}  # không entry → không TCP probe
        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == GIVE_UP
        assert pf.extends == 0

    def test_renew_fail_then_success_next_tick_recovers_clean(self, monkeypatch):
        """C12-K1: fail → RENEW_FAILED; tick sau success → ALIVE +
        renew_attempts reset ĐÚNG NGHĨA (counter của chu kỳ thất bại)."""
        clock = FakeClock()
        pf = ScriptedPlatform(time_left=400.0, extend_ok=False)
        ka, now = make_ka(pf, clock)
        tr = InstanceTracker(1, "g", platform_kind="gzctf")

        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == RENEW_FAILED and tr.renew_attempts == 1

        pf.extend_ok = True                    # platform hồi phục
        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == ALIVE
        assert tr.renew_attempts == 0          # counter reset đúng lúc recover
        assert any(lv == "info" and "Extended" in m for lv, m in evs)
        # lifetime được neo lại từ observed mới sau renew
        assert tr.est_lifetime == 400.0

        # Chu kỳ sự cố MỘT LẦN NỮA độc lập: hạ remaining dưới ngưỡng neo mới
        # (60% × 400 = 240) để rơi vào DUE_SOON rồi fail — counter phải đếm
        # lại từ 1, không kế thừa chu kỳ trước.
        pf.extend_ok = False
        pf.status_now["time_left"] = 200.0
        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == RENEW_FAILED and tr.renew_attempts == 1

    def test_escalate_stable_key_suppresses_new_incident_within_window(self, monkeypatch):
        """Documentation M-1: key ổn định chống spam, nhưng recovery KHÔNG
        xoá dấu vết suppress → sự cố MỚI trong cùng 300s bị nuốt warning
        đầu tiên (tự hết hạn sau 300s). Rủi ro thấp nhưng cần biết."""
        clock = FakeClock()
        pf = ScriptedPlatform(time_left=400.0, extend_ok=False)
        ka, now = make_ka(pf, clock)
        tr = InstanceTracker(1, "g", platform_kind="gzctf")

        evs = tick(ka, tr, clock, now, monkeypatch)          # sự cố A: warn
        assert any(lv == "warning" for lv, m in evs)
        pf.extend_ok = True
        tick(ka, tr, clock, now, monkeypatch)                # recover
        pf.extend_ok = False
        pf.status_now["time_left"] = 200.0     # dưới ngưỡng neo mới (240)
        evs = tick(ka, tr, clock, now, monkeypatch, advance=60)
        assert tr.state == RENEW_FAILED
        assert not any(lv == "warning" for lv, m in evs), (
            "sự cố mới bị suppress vì key chưa hết hạn — hành vi hiện tại")
        evs = tick(ka, tr, clock, now, monkeypatch, advance=301)
        assert any(lv == "warning" for lv, m in evs), "suppress phải tự hết hạn"

    def test_bug_gzctf_stale_renew_attempts_survive_restart(self, monkeypatch):
        # BUG-DEMO (chủ ý FAIL): GIVE_UP (renew_attempts >= 4) → container
        # chết → gzctf auto-restart THÀNH CÔNG (container MỚI, flag giữ
        # nguyên) → chỉ cần 1 lần renew lỗi của CUỘC SỐNG MỚI là rơi thẳng
        # GIVE_UP, dù lifecycle mới phải được đủ 4 lần thất bại.
        clock = FakeClock()
        pf = ScriptedPlatform(time_left=400.0, extend_ok=False)
        ka, now = make_ka(pf, clock)
        tr = InstanceTracker(1, "g", platform_kind="gzctf")

        # Chạy tự nhiên tới GIVE_UP: 4 lần fail + tick thứ 5 kết luận
        for _ in range(5):
            tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == GIVE_UP and tr.renew_attempts == 4

        # Container chết thật → restart cycle hoàn chỉnh (gzctf giữ flag
        # nên được auto-restart)
        pf.status_now = {"status": "stopped", "entry": None}
        tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == RESTARTING
        tick(ka, tr, clock, now, monkeypatch, advance=15)    # cooldown → POST
        assert pf.starts == 1
        pf.status_now = {"status": "running", "entry": "127.0.0.1:7777",
                         "time_left": 3600.0}
        tick(ka, tr, clock, now, monkeypatch, advance=35)    # boot + health OK
        assert tr.state == ALIVE

        # Cuộc sống mới: hạ remaining dưới ngưỡng neo mới (60%×3600 clamp
        # 600) rồi renew lỗi ĐẦU TIÊN...
        pf.status_now["time_left"] = 400.0
        evs = tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == RENEW_FAILED
        # ...tick kế tiếp KHÔNG được phép GIVE_UP (mới fail 1/4 của
        # lifecycle mới) — hiện tại rơi thẳng GIVE_UP vì attempts cũ (=4).
        tick(ka, tr, clock, now, monkeypatch)
        assert tr.state != GIVE_UP, (
            f"BUG C13-K1: renew_attempts stale ({tr.renew_attempts}) sống sót "
            f"qua restart → lifecycle mới chỉ chịu 0 lần lỗi thay vì "
            f"{RENEW_MAX_ATTEMPTS}. Fix: reset renew_attempts=0 (và "
            f"renew_count=0 với whale) ở nhánh thành công _tick_restarting.")

    def test_bug_whale_fresh_container_instantly_give_up_after_revival(self, monkeypatch):
        # BUG-DEMO (chủ ý FAIL): whale renew_count chạm WHALE_MAX_RENEWS →
        # GIVE_UP; container chết rồi được restart THÀNH CÔNG (không giữ
        # flag) → container MỚI có 5 lượt renew mới phía server, nhưng
        # renew_count client không reset → tick DUE_SOON đầu tiên tuyên
        # bố "hết 5 lượt renew" mà KHÔNG thử phát nào (extends == 0).
        clock = FakeClock()
        pf = ScriptedPlatform(time_left=3600.0, extend_ok=True)
        ka, now = make_ka(pf, clock)
        tr = InstanceTracker(1, "w", platform_kind="whale")
        tr.renew_count = WHALE_MAX_RENEWS
        tr.state = GIVE_UP

        # Container chết, không giữ flag → được phép auto-restart
        pf.status_now = {"status": "stopped", "entry": None}
        tick(ka, tr, clock, now, monkeypatch)
        assert tr.state == RESTARTING
        tick(ka, tr, clock, now, monkeypatch, advance=15)
        pf.status_now = {"status": "running", "entry": "127.0.0.1:8888",
                         "time_left": 3600.0}
        tick(ka, tr, clock, now, monkeypatch, advance=35)
        assert tr.state == ALIVE

        # Container mới sắp đến hạn renew lần ĐẦU TIÊN
        pf.status_now = {"status": "running", "entry": "127.0.0.1:8888",
                         "time_left": 500.0}
        tick(ka, tr, clock, now, monkeypatch)
        assert tr.state != GIVE_UP, (
            f"BUG C13-K2: container mới tái sinh nhưng renew_count="
            f"{tr.renew_count} không reset → GIVE_UP tức thì, extends="
            f"{pf.extends}. Fix: reset renew_count=0 khi restart thành công "
            f"(server cấp lại đủ {WHALE_MAX_RENEWS} lượt cho container mới).")
        assert pf.extends >= 1   # ít nhất phải THỬ renew


# ====================================================================== #
# VÙNG 3 — Export-pack prune (C11-03)
# ====================================================================== #
def make_ws(root: Path, chals: dict):
    for key, meta in chals.items():
        d = root / "chals" / key
        (d / "writeup").mkdir(parents=True, exist_ok=True)
        full = {"id": key, "name": key, "category": "Web", "points": 100}
        full.update(meta)
        full["status"] = {"solve": "solved_by_me", "writeup": "draft"}
        (d / "metadata.json").write_text(json.dumps(full), encoding="utf-8")
        (d / "writeup" / "README.md").write_text(
            "## Solve\nFLAG{h4rd3ned_flag_" + key.lower() + "}\n",
            encoding="utf-8")


def build(exp_cls, ws, out):
    ex = exp_cls(ws)
    return ex.build_pack(out_dir=str(out))


@pytest.fixture(autouse=True)
def _silence_export_console(monkeypatch):
    monkeypatch.setattr(WriteupExporter, "_print_warnings",
                        staticmethod(lambda w: None))
    monkeypatch.setattr(WriteupExporter, "_print_summary",
                        staticmethod(lambda n, p: None))


class TestExportPackPrune:
    def test_rename_between_runs_old_subdir_pruned_from_dir_and_zip(self, tmp_path):
        ws, out = tmp_path / "ws", tmp_path / "out"
        make_ws(ws, {"alpha": {"name": "Alpha"}})
        pack1 = build(WriteupExporter, ws, out)
        subs1 = [p.name for p in pack1.iterdir() if p.is_dir()]
        assert subs1 == ["Web_Alpha"]
        old_sub = subs1[0]

        make_ws(ws, {"alpha": {"name": "Beta"}})   # đổi TÊN giữa 2 lần chạy
        pack2 = build(WriteupExporter, ws, out)
        assert pack2 == pack1                      # cùng ngày → cùng pack
        names = {p.name for p in pack2.iterdir()}
        assert names == {"INDEX.md", "Web_Beta"}, \
            f"pack sau rename còn: {names}"
        assert not any("alpha" in n.lower() for n in names), \
            f"subdir cũ còn sót: {names}"

        zl = zipfile.ZipFile(str(pack2) + ".zip").namelist()
        assert any("Web_Beta" in n for n in zl)
        assert not any("Web_Alpha" in n for n in zl), \
            f"zip còn chứa subdir cũ: {[n for n in zl if 'Alpha' in n]}"
        index = (pack2 / "INDEX.md").read_text(encoding="utf-8")
        assert "Beta" in index and "Alpha" not in index

    def test_collision_pair_shrinks_suffix_dir_pruned(self, tmp_path):
        ws, out = tmp_path / "ws", tmp_path / "out"
        # "Pwn Me" và "Pwn_Me" cùng sanitize → Web_Pwn_Me vs Web_Pwn_Me_2
        make_ws(ws, {"c1": {"name": "Pwn Me"},
                     "c2": {"name": "Pwn_Me"}})
        pack1 = build(WriteupExporter, ws, out)
        dirs1 = sorted(p.name for p in pack1.iterdir() if p.is_dir())
        assert dirs1 == ["Web_Pwn_Me", "Web_Pwn_Me_2"], dirs1

        import shutil as _sh
        _sh.rmtree(ws / "chals" / "c2")            # lần này chỉ còn 1 entry
        pack2 = build(WriteupExporter, ws, out)
        dirs2 = sorted(p.name for p in pack2.iterdir() if p.is_dir())
        assert dirs2 == ["Web_Pwn_Me"], \
            f"hậu tố collision của lần trước không bị prune: {dirs2}"
        zl = zipfile.ZipFile(str(pack2) + ".zip").namelist()
        assert not any("Web_Pwn_Me_2" in n for n in zl), \
            f"zip còn chứa hậu tố stale: {[n for n in zl if 'Web_Pwn_Me_2' in n]}"


# ====================================================================== #
# VÙNG 4 — Menu index guard (C12-M2)
# ====================================================================== #
class FakeMenuCon:
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


def make_menu(monkeypatch, script, tmp_home):
    import ctf_downloader.interactive_menu as im
    con = FakeMenuCon(script)
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    saved = {}
    monkeypatch.setattr(im, "save_global_config",
                        lambda cfg: saved.update(ws=cfg.get("default_workspace")))

    class StubDash:
        def __init__(self, p):
            pass

        def get_summary_stats(self):
            return {"total_challenges": 3}

    monkeypatch.setattr(im, "CTFDashboard", StubDash)
    monkeypatch.setenv("HOME", str(tmp_home))
    app = object.__new__(im.CTFInteractiveConsole)
    app.config = {}
    app.workspace_path = "/baseline/ws"
    app.cookie = None
    app.token = None
    return im, app, con, saved


@pytest.fixture()
def ws_env(tmp_path):
    ctf = tmp_path / "Workspace" / "CTF"
    (ctf / "wsAAA").mkdir(parents=True)
    (ctf / "wsBBB").mkdir(parents=True)
    return ctf


class TestMenuIndexGuard:
    def test_switch_out_of_range_and_negatives_rejected_clean(self, monkeypatch, tmp_path, ws_env):
        """'99' / '-1' / '00' / '-0' phải bị từ chối SẠCH: không đổi
        workspace, không ghi config, không crash."""
        for bad in ("99", "-1", "00", "-0", "999999"):
            im, app, con, saved = make_menu(monkeypatch, [bad], tmp_path)
            app._menu_switch_workspace()
            assert app.workspace_path == "/baseline/ws", f"'{bad}' đổi ws!"
            assert saved == {}, f"'{bad}' ghi config!"

    def test_switch_lenient_int_forms_select_exact_index(self, monkeypatch, tmp_path, ws_env):
        """Documentation lệch brief: '01'/'+1'/' 1 '/'١' KHÔNG bị từ chối —
        int() parse khoan dung chọn đúng đúng-1-workspace (wsAAA), có log
        success + ghi config. Không crash, không chọn NHẦM mục khác →
        đánh giá: khoan dung vô hại (false-alarm với brief 'phải từ chối')."""
        for form, expect_ws in (("01", "wsAAA"), ("+1", "wsAAA"),
                                (" 1 ", "wsAAA"), ("١", "wsAAA")):
            im, app, con, saved = make_menu(monkeypatch, [form], tmp_path)
            app._menu_switch_workspace()
            assert app.workspace_path == str(ws_env / expect_ws), \
                f"'{form}' → {app.workspace_path}"
            assert saved.get("ws") == app.workspace_path

    def test_switch_superscript_digit_rejected_clean(self, monkeypatch, tmp_path, ws_env):
        """Đối chứng: '²' (isdigit True, int() nổ) trong switch_workspace
        nằm trong try → bắt sạch thành 'Lựa chọn không hợp lệ'."""
        im, app, con, saved = make_menu(monkeypatch, ["²"], tmp_path)
        app._menu_switch_workspace()          # không được raise
        assert app.workspace_path == "/baseline/ws"

    # ------------------------------------------------------------------
    def test_container_picker_positive_control(self, monkeypatch, tmp_path):
        """Điều khiển dương: '1' → chọn container 1, action chạy đúng id."""
        import ctf_downloader.interactive_menu as im
        calls = []

        class FakeMgr:
            def __init__(self, *a, **k):
                pass

            def list_containers(self):
                return [{"id": 7, "name": "Aaa", "category": "Web", "solves_count": 1},
                        {"id": 8, "name": "Bbb", "category": "Pwn", "solves_count": 2}]

            def start_instance(self, cid):
                calls.append(("start", cid))

            def extend_instance(self, cid):
                calls.append(("extend", cid))

        con = FakeMenuCon(["1", "3"])          # MỘT console dùng chung 2 prompt
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        monkeypatch.setattr(im, "InstanceManager", FakeMgr)
        monkeypatch.setattr(im, "_pause", lambda: None)
        app = object.__new__(im.CTFInteractiveConsole)
        app.workspace_path = "/x"
        app.cookie = app.token = None
        app.config = {}
        app._menu_container_manager()
        assert calls == [("extend", 7)]

    def test_bug_superscript_digit_crashes_whole_menu(self, monkeypatch, tmp_path):
        # BUG-DEMO (chủ ý FAIL): '²'.isdigit()==True nhưng int('²') nổ
        # ValueError NGOÀI try — _menu_container_manager không bắt, run()
        # cũng chỉ bắt EOFError/KeyboardInterrupt → traceback giết cả phiên
        # menu. Fix: dùng ``ch.isdecimal()`` (hoặc bọc try/int) ở gate.
        import ctf_downloader.interactive_menu as im

        class Mgr:
            def __init__(self, *a, **k):
                pass

            def list_containers(self):
                return [{"id": 7, "name": "Aaa", "category": "Web",
                         "solves_count": 1},
                        {"id": 8, "name": "Bbb", "category": "Pwn",
                         "solves_count": 2}]

        con = FakeMenuCon(["²"])               # một console, một prompt
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        monkeypatch.setattr(im, "InstanceManager", Mgr)
        monkeypatch.setattr(im, "_pause", lambda: None)
        app = object.__new__(im.CTFInteractiveConsole)
        app.workspace_path = "/x"
        app.cookie = app.token = None
        app.config = {}
        raised = None
        try:
            app._menu_container_manager()
        except ValueError as exc:
            raised = exc
        assert raised is None, (
            f"BUG C13-MENU1: '²' vượt gate .isdigit() rồi nổ {raised!r} "
            f"trong _menu_container_manager — crash cả phiên menu. "
            f"Fix: interactive_menu.py gate dùng ch.isdecimal() hoặc "
            f"bọc try/int.")


# ====================================================================== #
# VÙNG 5 — Sniper Ctrl-C lúc ĐANG bắn
# ====================================================================== #
class BoomSubmitter:
    def __init__(self, boom_on_call=0):
        self.calls = []
        self.boom_on_call = boom_on_call
        self.submit_history = []
        self.platform = SimpleNamespace(last_verdict=None)

    def submit(self, challenge, flag, force=False):
        self.calls.append((challenge, flag, force))
        if len(self.calls) > self.boom_on_call:
            raise KeyboardInterrupt()


class TestSniperCtrlCMidFire:
    def _targets(self, tmp_path):
        p = tmp_path / "sniper.json"
        p.write_text(json.dumps([
            {"challenge": "a", "flag": "FA{aaaa}", "delay_seconds": 0},
            {"challenge": "b", "flag": "FB{bbbb}", "delay_seconds": 0},
        ]), encoding="utf-8")
        return p

    def test_ctrl_c_during_submit_leaves_state_consistent(self, tmp_path):
        """Ctrl-C giữa lúc submit đang chạy: run() phải nuốt KI, trả summary
        aborted=True, target đang bắn ở lại PENDING (attempts đã tăng),
        không mất mát solved/failed, không traceback."""
        self._targets(tmp_path)
        sub = BoomSubmitter(boom_on_call=0)   # phát ĐẦU TIÊN đã Ctrl-C
        svc = sn.SniperService(SimpleNamespace(root=tmp_path), sub)
        summary = svc.run(poll_interval=1, start_at="2020-01-01T00:00:00Z")
        assert isinstance(summary, dict)
        assert summary["aborted"] is True
        assert len(summary["solved"]) == 0 and len(summary["failed"]) == 0
        assert len(summary["pending"]) == 2           # cả hai còn ở hàng chờ
        assert summary["pending"][0]["attempts"] == 1  # đang bắn → +1 rồi hủy
        assert len(sub.calls) == 1                     # đúng 1 phát rồi dừng

    def test_ctrl_c_while_waiting_for_start_regression(self, monkeypatch, tmp_path):
        """Regression C12-S1: Ctrl-C lúc CANH GIỜ vẫn abort sạch (sleep
        trong vòng chờ raise KI → run() nuốt)."""
        import ctf_downloader.services.sniper_service as sn_mod

        def _raise_sleep(_s):
            raise KeyboardInterrupt()

        monkeypatch.setattr(sn_mod.time, "sleep", _raise_sleep)
        self._targets(tmp_path)                # có target để vào vòng chờ
        sub = BoomSubmitter(boom_on_call=-1)   # không bao giờ boom
        svc = sn.SniperService(SimpleNamespace(root=tmp_path), sub)
        summary = svc.run(poll_interval=1, start_at="2030-01-01T00:00:00Z")
        assert summary["aborted"] is True
        assert len(summary["pending"]) == 2
        assert sub.calls == []                 # chưa bắn phát nào


# ====================================================================== #
# VÙNG 6 — Watch checkpoint isolate (C11-02)
# ====================================================================== #
class BoomStateStore:
    def __init__(self):
        self.checkpoint_calls = []

    def checkpoint_type(self, state, sync_type):
        self.checkpoint_calls.append(sync_type)
        raise OSError(".ctf read-only (mô phỏng)")


class TestWatchCheckpointIsolate:
    def test_checkpoint_raising_10_ticks_no_backoff_spread(self):
        """checkpoint_type raise liên tục 10 vòng: mọi task vẫn tick ĐỦ 10
        lần, reward/postpone bình thường (mult=rl_mult=1, penalty=None) —
        lỗi lưu state không bị tính thành lỗi task, không backoff lan."""
        from ctf_downloader.services.watch_service import WatchService, PollScheduler

        svc = WatchService.__new__(WatchService)
        svc.scheduler = PollScheduler(rng=lambda lo, hi: (lo + hi) / 2)
        tasks = ("notices", "scoreboard", "challenges", "keepalive")
        for task, iv in (("notices", 15), ("scoreboard", 60),
                         ("challenges", 120), ("keepalive", 60)):
            svc.scheduler.register(task, iv)
        svc.state = {}
        svc.guard = None
        svc.keepalive = None
        # Các attr __init__ mà tick methods đọc trực tiếp (đi qua __new__)
        svc._last_score = None
        svc._known_chall_count = None
        svc._burst_until_mono = None
        svc.platform = SimpleNamespace(
            ctf_info=SimpleNamespace(platform_type="gzctf"),
            fetch_scoreboard=lambda: {},
            fetch_challenges=lambda: [])
        store = BoomStateStore()
        svc.state_store = store

        calls = {t: 0 for t in tasks}
        for t in tasks:
            orig = getattr(WatchService, f"_tick_{t}")

            # Instance attr (không descriptor-bind) → wrapper nhận ĐÚNG
            # signature handler(window_active=...) mà _run_round gọi.
            def _wrap(window_active=True, _orig=orig, _t=t):
                calls[_t] += 1
                return _orig(svc, window_active=window_active)

            setattr(svc, f"_tick_{t}", _wrap)

        for rnd in range(1, 11):
            for t in svc.scheduler._tasks.values():
                t["deadline"] = 0.0            # ép đến hạn ngay
            lines = svc._run_round({})         # không được raise
            for t in tasks:
                assert calls[t] == rnd, f"{t}: tick {rnd} chạy {calls[t]} lần"
                tk = svc.scheduler._tasks[t]
                assert tk["mult"] == 1.0, f"{t}: bị penalize tại vòng {rnd}"
                assert tk["rl_mult"] == 1.0
                assert tk["penalty"] is None
            ck = [c for c in store.checkpoint_calls]
            assert len(ck) == 4 * rnd          # mỗi task thử checkpoint mỗi vòng
            assert sum(1 for ln in lines if "checkpoint" in ln) == 4, \
                f"vòng {rnd}: thiếu cảnh báo checkpoint"
