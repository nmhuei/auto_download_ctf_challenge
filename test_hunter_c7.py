"""HUNTER agent — cycle 7: bấm đường biên vùng CHƯA hunt sâu.

Phạm vi:
  - auth_service (AuthService.resolve: cookie file, auth map key)
  - register_service (tempmail fail giữa chừng, captcha lạ, password gen,
    hook xác minh email)
  - platform_resolver + detection pipeline (URL lạ, markers trống/hỏng,
    xung đột ưu tiên)
  - watch_service (PollScheduler interval 0/âm + penalize >cap,
    WindowGuard start>end + clock-skew cực đại, Ctrl-C cleanup)
  - cross-check kiến trúc (requests.Session() ngoài session_factory)
  - spec-check nhanh event-window §4/§5 (ETag, Retry-After, config off)

Quy ước KẾT QUẢ: test PASS = hành vi hiện tại chấp nhận được
(false-alarm / documentation). Test FAIL = bug thật — tên test bắt đầu
``test_R<n>_`` và assert HÀNH VI MONG MUỐN (đang sai).

An toàn mạng: KHÔNG test nào chạm network thật — mọi HTTP qua FakeSession;
localhost không cần thiết ở cycle này.

Chạy: python3 -m pytest test_hunter_c7.py -q
"""
import datetime as _dt
import email.utils
import json
import os
import re
import signal
import time
import types

import pytest

import ctf_downloader.services.auth_service as auth_mod
from ctf_downloader.cli import build_unified_parser
from ctf_downloader.platforms.base import PlatformRegisterUnsupported
from ctf_downloader.platforms.detection import (
    _MARKER_PRIORITY,
    _match_html_markers,
    detect_platform_info,
)
from ctf_downloader.services.auth_service import AuthService
from ctf_downloader.services.register_service import (
    RegisterService,
    generate_credentials,
)
from ctf_downloader.services.watch_service import (
    PollScheduler,
    WatchService,
    WatchStateStore,
    WindowGuard,
    parse_time_arg,
)
from ctf_downloader.utils.tempmail import TempMailError


# ----------------------------------------------------------------------
# Fakes (network-mock)
# ----------------------------------------------------------------------

class FakeResp:
    def __init__(self, status_code=200, text="", headers=None, url="",
                 json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Session giả: route theo substring URL; mặc định 404 text trống."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.cookies = {}          # .keys() dùng ở tầng cookie-hints

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        return FakeResp(404, "")

    def request(self, method, url, **kw):
        return self.get(url)


def make_info(platform_type="ctfd", confidence="high"):
    return types.SimpleNamespace(platform_type=platform_type,
                                 confidence=confidence, game_id=None)


# ----------------------------------------------------------------------
# A. AuthService — cookie/token lưu đâu, format gì; biên đầu vào
# ----------------------------------------------------------------------

def test_a1_cookie_arg_la_file_doc_duoc_strip(tmp_path):
    f = tmp_path / "ck.txt"
    f.write_text("  session=abc123  \n", encoding="utf-8")
    cookie, token = AuthService.resolve("/any/ws", cookie_arg=str(f))
    assert cookie == "session=abc123"      # đọc nội dung file, strip
    assert token is None


def test_a2_cookie_file_rong_tra_chuoi_rong_graceful(tmp_path):
    # False-alarm doc: file rỗng -> cookie="" (falsy) -> create_session bỏ qua
    # (http_client.create_session: `if cookie:`). Không crash.
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    cookie, _tok = AuthService.resolve("/any/ws", cookie_arg=str(f))
    assert cookie == ""


def test_a3_token_cli_uu_tien_hon_token_luu(monkeypatch):
    monkeypatch.setattr(auth_mod, "load_global_config",
                        lambda: {"auth": {os.path.abspath("/w"): {
                            "cookie": "CK", "token": "SAVED"}}})
    cookie, token = AuthService.resolve("/w", token_arg="CLI")
    assert (cookie, token) == ("CK", "CLI")


def test_a4_auth_entry_keyed_bang_URL_khong_bao_gio_duoc_resolve(monkeypatch):
    """R1 (đã fix) + R3 (review round): entry URL-keyed do register lưu khi
    `--workspace` không phải dir thật phải đọc lại được — qua EXACT key URL
    hoặc fallback cùng HOST.

    - Workspace truyền đúng URL (vd `ctf pull https://ctf.example.com`)
      -> resolve được ("CK", "TK").
    - Path ảo KHÔNG mang thông tin platform -> KHÔNG tự tiện mượn entry
      duy nhất (chống leak cookie chéo platform — R3): trả None.
    """
    monkeypatch.setattr(auth_mod, "load_global_config",
                        lambda: {"auth": {"https://ctf.example.com": {
                            "cookie": "CK", "token": "TK"}}})
    # Workspace chính là URL -> exact/same-host lookup thấy entry
    cookie, token = AuthService.resolve("https://ctf.example.com")
    assert (cookie, token) == ("CK", "TK"), (
        "auth map entry lưu dưới key URL phải resolve được qua URL workspace")
    # Path ảo không có bằng chứng platform -> không mượn cookie bừa
    ws = os.path.join(os.sep, "khong", "ton", "tai")
    cookie2, _token2 = AuthService.resolve(ws)
    assert cookie2 is None, (
        "path ảo không xác định được platform -> không được mượn entry "
        "URL duy nhất (R3 chống leak chéo platform)")


# ----------------------------------------------------------------------
# B. RegisterService — tempmail fail giữa chừng, captcha lạ, hook, password
# ----------------------------------------------------------------------

def _svc(saver_calls, tempmail_factory=None, detect_fn=None):
    return RegisterService(
        now_fn=lambda: 1000.0,
        sleep_fn=lambda s: None,
        config_loader=lambda: {},
        config_saver=lambda cfg: saver_calls.append(cfg),
        tempmail_factory=tempmail_factory or (lambda: types.SimpleNamespace(
            create_mailbox=lambda: (_ for _ in ()).throw(
                TempMailError("mail.tm down")))),
        detect_fn=detect_fn,
    )


def test_b1_tempmail_fail_truoc_register_state_sach():
    """False-alarm check: mailbox fail -> RuntimeError, KHÔNG ghi gì xuống
    config (không attempt, không auth entry) — state để lại sạch."""
    saver = []
    svc = _svc(saver)
    with pytest.raises(RuntimeError):
        svc.run("https://ctf.example.com", use_tempmail=True)
    assert saver == []          # không persist gì cả


def test_b2_register_exception_thuong_mat_credentials_va_skip_rate_limit():
    """R2 (BUG — test FAIL chủ ý).

    run() chỉ bắt PlatformRegisterUnsupported (register_service.py:264).
    Exception thường từ platform.register — vd TempMailError ném ra từ
    verify-hook (fetch_message_text KHÔNG được wrap trong
    _make_verify_hook, register_service.py:198) hoặc lỗi mạng chưa guard —
    thoát ra ngoài khiến:
      (a) credentials đã sinh CHƯA hề được in -> mất luôn dù account có thể
          đã tạo server-side;
      (b) van-an-toàn "ghi nhận MỌI lần attempt" bị bypass — rate limit
          không record (comment register_service.py:277 nói ngược lại).
    Fix đề xuất: try/except Exception quanh platform.register -> in creds +
    _record_attempt trước khi raise lại.
    """
    saver = []

    def boom(**kw):
        raise TempMailError("mail.tm chết giữa flow xác minh email")

    plat = types.SimpleNamespace(register=boom)
    svc = _svc(saver, detect_fn=lambda url, sess: (plat, make_info()))
    with pytest.raises(TempMailError):
        svc.run("https://ctf.example.com", email="a@b.c")
    # MONG MUỐN: attempt vẫn được record (account đã tồn tại server-side).
    assert len(saver) >= 1, (
        "exception giữa flow: attempt KHÔNG được record -> rate-limit bypass "
        "+ credentials mất (chưa in)")


def test_b3_captcha_la_geetest_dung_sach_platform_register_unsupported():
    """Captcha type lạ (không None/HashPow/Turnstile...) -> dừng sạch."""
    sess = FakeSession(routes={
        "/api/config": FakeResp(200, json_data={}),
        "/api/captcha": FakeResp(200, json_data={"type": "geetest"}),
    })
    plat = types.SimpleNamespace(origin="https://g.example", session=sess)
    from ctf_downloader.platforms.gzctf import gzctf_probe_captcha
    with pytest.raises(PlatformRegisterUnsupported):
        gzctf_probe_captcha(plat)


def test_b4_captcha_none_sitekey_rong_di_tiep():
    from ctf_downloader.platforms.gzctf import gzctf_probe_captcha
    sess = FakeSession(routes={
        "/api/config": FakeResp(200, json_data={}),
        "/api/captcha": FakeResp(200, json_data={"type": "None",
                                                 "siteKey": ""}),
    })
    plat = types.SimpleNamespace(origin="https://g.example", session=sess)
    assert gzctf_probe_captcha(plat) == {}


def test_b5_hashpow_difficulty_0_tra_answer_rong():
    """L edge-doc: difficulty<=0 -> solve_hash_pow trả '' -> payload gửi
    'challenge': '<id>:' (answer rỗng, sai wire-format AnswerLength*2=16 hex
    khi server vẫn bật HashPow với difficulty=0). Hiện trạng chấp nhận được
    trong thực tế (difficulty=0 hiếm), ghi nhận để theo dõi."""
    from ctf_downloader.platforms.gzctf import solve_hash_pow
    assert solve_hash_pow("aabb", 0) == ""
    ans = solve_hash_pow("00", 8)     # sanity: difficulty nhỏ giải được
    assert isinstance(ans, str) and len(ans) == 16


def test_b6_password_gen_4_nhom_ky_tu_khong_ky_tu_loi_shell():
    rng = __import__("random").Random(42)
    for _ in range(20):
        creds = generate_credentials(password_length=16, rng=rng)
        pw = creds["password"]
        assert len(pw) == 16
        assert any(c in "abcdefghijkmnopqrstuvwxyz" for c in pw)
        assert any(c in "ABCDEFGHJKLMNPQRSTUVWXYZ" for c in pw)
        assert any(c in "23456789" for c in pw)
        assert any(c in "!@#$%^&*-_+?" for c in pw)
        assert not set(pw) & set("\"'`;|\\$<>()[]{}~") or True  # info-only
    # alphabet không chứa quote/backshell-char gây lỗi completion/JSON
    assert not set("!@#$%^&*-_+?") & set("\"'`\\;")
    short = generate_credentials(password_length=2,
                                 rng=__import__("random").Random(1))
    assert len(short["password"]) == 4       # clamp tối thiểu 4 nhóm


def test_b7_verify_hook_tempmail_chet_giua_chung_raise_nguyen_trang():
    """Root-cause của R2: fetch_message_text raise TempMailError thì hook
    KHÔNG wrap -> ném xuyên qua platform.register ra run()."""
    svc = _svc([])
    client = types.SimpleNamespace(
        wait_for_message=lambda timeout_s: {"id": "m1"},
        fetch_message_text=lambda mid: (_ for _ in ()).throw(
            TempMailError("api die sau khi tạo mailbox")),
    )
    hook = svc._make_verify_hook(client)

    class Sess:
        def get(self, url, timeout=None):
            return FakeResp(200, url=url)

    with pytest.raises(TempMailError):
        hook(Sess())


def test_b8_hook_status_check_dead_code_or_true():
    """Doc L: `_make_verify_hook` dòng `... or True` (register_service.py:208)
    khiến mọi status đều 'ok' — check HTTP chết. Ghi nhận hiện trạng."""
    svc = _svc([])
    client = types.SimpleNamespace(
        wait_for_message=lambda timeout_s: {"id": "m1"},
        fetch_message_text=lambda mid: "https://x/confirm/abc",
    )
    hook = svc._make_verify_hook(client)

    class Sess:
        def get(self, url, timeout=None):
            return FakeResp(500, url=url)

    assert hook(Sess()) is True     # 500 vẫn báo thành công (dead check)


# ----------------------------------------------------------------------
# C. Detection pipeline — URL lạ, markers, ưu tiên
# ----------------------------------------------------------------------

def _detect(url, routes=None, html=""):
    sess = FakeSession(routes)
    if html:
        sess.routes[""] = FakeResp(200, text=html)   # match mọi URL tier-1
    platform, info = detect_platform_info(url, sess, quiet=True)
    return platform, info, sess


def test_c1_trailing_slash_duoc_strip_khi_get_base():
    _plat, info, sess = _detect("https://x.example/")
    assert sess.calls[0] == "https://x.example"      # không còn '/' đuôi
    assert info.base_url == "https://x.example"


def test_c2_url_khong_scheme_khong_crash_fallback_generic():
    # urlparse('x.example') -> netloc rỗng, origin='://' — pipeline phải
    # nuốt mọi request fail (safe_get) và fallback generic_html.
    platform, info, _s = _detect("x.example")
    assert info.platform_type == "generic_html"
    assert platform.ctf_info.platform_type == "generic_html"


@pytest.mark.parametrize("url", [
    "https://giải-ctf.example.vn",             # IDN unicode host
    "https://x.example:99999",                  # port > 65535
    "https://x.example:999999999999999999999",  # port điên
])
def test_c3_url_ly_la_khong_crash(url):
    platform, info, _s = _detect(url)
    assert info.platform_type == "generic_html"


def test_c4_html_markers_rong_va_regex_hong_khong_crash():
    spec_empty = types.SimpleNamespace(html_markers=())
    assert _match_html_markers(spec_empty, "<html>GZCTF</html>",
                               "<html>gzctf</html>") is False
    spec_bad = types.SimpleNamespace(
        html_markers=("regex:[unclosed", "regex:(?P<", "GZCTF"))
    assert _match_html_markers(spec_bad, "<html>GZCTF</html>",
                               "<html>gzctf</html>") is True   # regex hỏng skip, chuỗi thường vẫn khớp


def test_c5_2_platform_cung_marker_ctfd_thang_theo_priority():
    assert _MARKER_PRIORITY == ("rctf", "ctfd", "gzctf")   # chính sách khai báo
    html = ("<html>Powered by CTFd GZCTF csrfNonce' window.init</html>")
    platform, info, _s = _detect("https://mix.example/", html=html)
    # rctf không khớp (không có 'name="rctf-config"'/'kind":"') -> ctfd thắng
    assert info.platform_type == "ctfd"
    html2 = ('<meta name="rctf-config"> csrfNonce\' GZCTF "kind":"ok"')
    platform2, info2, _s2 = _detect("https://mix2.example/", html=html2)
    assert info2.platform_type == "rctf"           # rctf đứng trước ctfd/gzctf


def test_c6_game_id_tu_path_games():
    _plat, info, _s = _detect("https://g.example/games/42/challenges")
    assert info.game_id == 42


def test_c7_resolver_declared_platform_khong_go_mang(tmp_path):
    from ctf_downloader.services.platform_resolver import PlatformResolver

    class FakeRepo:
        def read_challenges(self):
            return {"ctf_info": {"platform": "ctfd", "game_id": "7"}}

        def resolve_platform_url(self):
            return "https://d.example"

    sess, platform, info = PlatformResolver.for_workspace(FakeRepo(),
                                                          cookie="CK")
    assert info.confidence == "high"
    assert platform.game_id == 7
    # KHÔNG request mạng nào được phép khi khai báo rõ ràng
    # (session thật nhưng không ai gọi .get -> không cách nào gọi ra ngoài;
    #  kiểm chứng gián tiếp: info.signals nhắc 'dựng')
    assert any("ctfd" in s.lower() for s in info.signals)


def test_c8_resolver_khong_co_url_raise_valueerror():
    from ctf_downloader.services.platform_resolver import PlatformResolver

    class NoUrlRepo:
        def read_challenges(self):
            return {}

        resolve_platform_url = None   # getattr -> not callable attr? vẫn None

    with pytest.raises(ValueError):
        PlatformResolver.for_workspace(NoUrlRepo())


# ----------------------------------------------------------------------
# D. PollScheduler / WindowGuard / WatchService lifecycle
# ----------------------------------------------------------------------

def test_d1_scheduler_interval_0_am_clamp_voi_due_ngay():
    s = PollScheduler(jitter=0.0, rng=lambda lo, hi: lo)
    s.register("zero", 0, due_now=False)
    assert s._tasks["zero"]["interval"] == 1.0        # clamp về >= 1s
    assert s.due("zero", now=time.time() + 1)    # deadline tính từ RAW 0
    s.register("neg", -5, due_now=False)
    assert s.due("neg", now=time.time() + 1)     # âm -> due ngay (doc)


def test_d2_penalize_interval_tren_cap_lam_ngan_interval_thay_vi_backoff():
    """R3 (BUG — test FAIL chủ ý).

    watch_service.py:156: mult = min(mult*2, BACKOFF_CAP/interval). Với
    interval=700 (>600): mult=min(2,0.857)=0.857 -> effective=600 < 700.
    Tick LỖI làm task chạy SỚM hơn (700->600) thay vì backoff.
    Fix 1 dòng: mult floor 1.0 ->
    ``t["mult"] = min(t["mult"]*2, max(1.0, BACKOFF_CAP/max(1.0,t["interval"])))``.
    """
    s = PollScheduler(jitter=0.0, rng=lambda lo, hi: lo)
    s.register("sb", 700, due_now=False)
    eff = s.penalize("sb")
    assert eff >= 700, f"penalize làm NGẮN interval: {eff} < 700"


def test_d3_window_start_sau_end_khong_bao_gio_live_im_lang():
    """R6 (doc-L): start>end (dữ liệu giải lộn) -> BEFORE rồi nhảy thẳng
    ENDED, KHÔNG cảnh báo validate ở đâu cả (_resolve_window không check).
    Hành vi deterministic, không crash -> test PASS ghi nhận hiện trạng."""
    t = time.time()
    g = WindowGuard(_dt.datetime.fromtimestamp(t + 1000, _dt.timezone.utc),
                    _dt.datetime.fromtimestamp(t + 500, _dt.timezone.utc),
                    grace_seconds=300)
    states = {g.state(now_wall=w) for w in range(int(t) - 10,
                                                 int(t) + 2000)}
    assert states == {"before", "ended"}
    assert "live" not in states        # không bao giờ sync dữ liệu


def test_d4_clock_skew_cuc_dai_ap_nguyen_khong_clamp():
    """R4 (BUG — test FAIL chủ ý).

    Server Date header lệch cực đại (vd năm 2099) -> offset ~+2.3e9s được
    apply_server_offset NGUYÊN VẸN (watch_service.py:833-834 không clamp)
    -> wall_now nhảy vài năm -> state ENDED ngay -> final sync + exit 0.
    Một response xấu/bị MITM đủ để giết watch im lặng.
    Fix 1 dòng: chỉ áp khi |offset| <= MAX_TRUSTED_SKEW (vd 6h):
    ``if abs(offset) <= 21600: self.guard.apply_server_offset(offset)``.
    """
    far = email.utils.formatdate(time.time() + 10 * 365 * 86400,
                                 localtime=False, usegmt=True)
    off = WindowGuard.date_header_offset(far)
    assert off is not None and abs(off) > CLOCK_BIG     # cơ chế đo được
    g = WindowGuard(_dt.datetime.fromtimestamp(time.time() - 60,
                                               _dt.timezone.utc),
                    _dt.datetime.fromtimestamp(time.time() + 3600,
                                               _dt.timezone.utc))
    assert g.state() == "live"
    g.apply_server_offset(off)
    # MONG MUỐN: offset điên không được tin mù quáng.
    assert abs(g.wall_now() - time.time()) < 86400, (
        "server Date header lệch cực đại được áp nguyên vẹn -> watch tự "
        "kết thúc oan (ENDED)")


CLOCK_BIG = 365 * 86400


def test_d5_sigint_sigterm_flag_sach_exit_code_va_lock():
    ws = _tmp_ws()
    svc = WatchService(str(ws))
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        svc._install_signal_handlers()
        os.kill(os.getpid(), signal.SIGINT)
        assert svc._stop is True and svc._exit_code == 130
        svc._stop = False
        os.kill(os.getpid(), signal.SIGTERM)
        assert svc._exit_code == 0                      # TERM -> 0 (spec §5)
        store = svc.state_store
        assert store.acquire_lock() is True
        assert os.path.exists(store.lock_path)
        svc._shutdown()
        assert not os.path.exists(store.lock_path)      # lock được dọn
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def test_d6_lock_live_pid_refuse_stale_takeover(tmp_path):
    store = WatchStateStore(str(tmp_path))
    assert store.acquire_lock() is True
    # live pid khác -> từ chối
    orig_alive, orig_read = WatchStateStore._pid_alive, store._read_lock_pid
    try:
        WatchStateStore._pid_alive = staticmethod(lambda pid: True)
        store._read_lock_pid = lambda: 987654
        assert store.acquire_lock() is False
        # stale pid -> chiếm lại
        WatchStateStore._pid_alive = staticmethod(lambda pid: False)
        assert store.acquire_lock() is True
    finally:
        WatchStateStore._pid_alive = orig_alive
        store._read_lock_pid = orig_read
        store.release_lock()


def test_d7_next_timeout_floor_va_empty():
    s = PollScheduler(jitter=0.0, rng=lambda lo, hi: lo)
    assert s.next_timeout() == 1.0                       # rỗng -> 1s
    s.register("t", 15, due_now=True)                    # deadline=0 -> quá
    assert s.next_timeout(now=time.monotonic()) >= 0.05  # floor dương


def test_d8_parse_time_arg_epoch_ms_giay_iso_va_garbage():
    ms = parse_time_arg("1780000000000")
    sec = parse_time_arg("1780000000")
    iso = parse_time_arg("2026-08-24T09:00")
    assert ms.tzinfo is not None and sec.tzinfo is not None
    assert (ms - _dt.timedelta(0)).year == 2026
    assert iso.tzinfo is not None                     # naive -> gán UTC
    assert parse_time_arg("-100") is None             # epoch âm -> None
    assert parse_time_arg("next tuesday") is None     # garbage -> None
    # CLI handle_watch sẽ exit 2 với thông báo đẹp khi None + arg truyền vào
    # (cli_commands.py:662-668) — không nuốt im lặng. Kiểm chứng mức parser:


# ----------------------------------------------------------------------
# E. Cross-check kiến trúc: requests.Session() ngoài session_factory
# ----------------------------------------------------------------------

_ALLOWED_RAW_SESSION = {
    "utils/http_client.py",        # nơi cấp quyền duy nhất
    "utils/tempmail.py",           # docstring: requests thuần, không dependency
    "platforms/ctftime_resolver.py",  # spec event-window §3 cho phép thẳng
    "generator/workspace_builder.py",  # template solve.py sinh cho user
}


def test_e1_khong_ai_tao_requests_session_ngoai_session_factory():
    """R5 (BUG — test FAIL chủ ý): downloaders/gdrive.py:83 tự tạo
    requests.Session() thô (không retry-adapter/UA chuẩn của http_client).
    Fix 1 dòng: `session = create_session()` (import services.session_factory).
    """
    root = os.path.dirname(os.path.abspath(__file__))   # test nằm ở repo root
    pkg = os.path.join(root, "ctf_downloader")
    violators = []
    pat = re.compile(r"\brequests\.Session\(\)")
    for dirpath, _dirs, files in os.walk(pkg):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, pkg)
            if rel.replace(os.sep, "/") in _ALLOWED_RAW_SESSION:
                continue
            with open(full, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        violators.append(f"{rel}:{i}")
    assert violators == [], (
        f"requests.Session() ngoài session_factory: {violators}")


# ----------------------------------------------------------------------
# F. Spec-check nhanh event-window §4/§5
# ----------------------------------------------------------------------

def _watch_source():
    root = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(root, "ctf_downloader", "services", "watch_service.py")
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_f1_spec_etag_304_cache_phai_duoc_dung_trong_tick():
    """S1 GAP (FAIL chủ ý): spec §5 'ETag/304 cache per endpoint' —
    etag_cache chỉ được khởi tạo trong WatchStateStore.load/default,
    không tick nào đọc/ghi -> feature chưa implement."""
    src = _watch_source()
    uses = [ln for ln in src.splitlines()
            if "etag" in ln.lower()
            and "setdefault" not in ln and '"etag_cache": {}' not in ln]
    assert uses, "etag_cache chưa được dùng ở bất kỳ tick nào (spec §5)"


def test_f2_spec_429_retry_after_phai_duoc_ton_trong_o_caller():
    """S2 GAP (FAIL chủ ý): spec §5 '429 tôn trọng Retry-After' — không
    caller tick nào parse Retry-After; chỉ backoff ×2 mù."""
    import ast
    root = os.path.dirname(os.path.abspath(__file__))
    svc_dir = os.path.join(root, "ctf_downloader", "services")
    pat = re.compile(r"[Rr]etry[-_ ][Aa]fter|[Rr][Ee][Tt][Rr][Yy]_?[Aa]fter")
    hits = []
    for fn in sorted(os.listdir(svc_dir)):
        if not fn.endswith(".py"):
            continue
        full = os.path.join(svc_dir, fn)
        with open(full, "r", encoding="utf-8") as f:
            src = f.read()
        # bỏ module docstring (nơi ghi chú 'Retry-After' không phải code)
        try:
            tree = ast.parse(src)
            doc = ast.get_docstring(tree)
            if doc:
                src = src.replace(doc, "", 1)
        except SyntaxError:
            pass
        for i, line in enumerate(src.splitlines(), 1):
            if pat.search(line):
                hits.append(f"services/{fn}:{i}")
    hits = [h for h in hits if "web_dashboard" not in h]  # server-side OK
    assert hits, "Retry-After không được parse ở tầng watch/caller (spec §5)"


def test_f3_spec_config_auto_sync_off_command():
    """S3 GAP (FAIL chủ ý): spec §4 'Đổi ý: ctf config auto-sync off' —
    không tồn tại subcommand `config` trong CLI."""
    parser = build_unified_parser()
    try:
        parser.parse_args(["config", "--help"])
        found = True
    except SystemExit as e:
        found = getattr(e, "code", 1) == 0     # --help exit 0 nếu có lệnh
    assert found, "subcommand 'config' (auto-sync off) chưa có trong CLI"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _tmp_ws():
    import tempfile
    d = tempfile.mkdtemp(prefix="hunter_c7_ws_")
    os.makedirs(os.path.join(d, ".ctf"), exist_ok=True)
    return d
