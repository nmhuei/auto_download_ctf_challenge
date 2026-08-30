"""REVIEW-3 RESIDUALS — R-M1 / R-L1 / R-L2 (reviewer-3, verdict APPROVE).

R-M1 (M): các call-site Logger truyền ``markup=True`` nhét dữ liệu SERVER
(tên user/team/challenge, email...) THÔ vào giữa tag rich → tên chứa
``[/]`` văng MarkupError CRASH lệnh, ``[link=…]`` inject OSC-8 hyperlink.
Fix chuẩn ở TỪNG call-site: bọc ``rich.markup.escape(...)`` quanh PHẦN
BIẾN ĐỘNG, giữ tag trang trí nguyên vẹn. Test đi qua đường production
(platform.authenticate với payload server độc); hợp đồng: không crash,
tên hiện NGUYÊN VĂN, và chữ ký SGR (style) giống hệt lần render tên benign.

R-L1 (L): TTL-refresh solve-attribution reset ``cache={}`` TRƯỚC fetch →
exception giữa chừng mất data tốt của kỳ trước tối đa 300s, đồng thời ts
bị stamp mới dù fail → lần sau phải chờ đủ TTL nữa mới retry. Hợp đồng:
fetch vào biến local, chỉ swap cache + stamp ts SAU khi thành công; fail →
giữ data cũ + ts cũ (tick sau retry ngay).

R-L2 (L): prune pack writeup xoá MỌI dir lạ trong pack dir → user drop tay
thư mục notes vào bị mất ở re-run cùng ngày. Hợp đồng: dir của TOOL (liệt
kê trong manifest lần chạy trước) vẫn bị prune khi stale (C11-03 giữ
nguyên); dir LẠ không có trong manifest → GIỮ + Logger.warning liệt kê.

Chạy: python3 -m pytest test_review3_residuals.py -q
Không mạng thật — toàn bộ HTTP mock bằng FakeSession.
"""
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ctf_downloader.ui import theme as ui_theme
from ctf_downloader.utils import logger as logger_mod
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.platforms.custom_rest import CustomRESTPlatform


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text.replace("\x1b]\\", "]"))


def _sgr_seq(ansi: str) -> list:
    """Trích chuỗi các mã SGR — chữ ký style của lần render."""
    return re.findall(r"\x1b\[[0-9;]*m", ansi)


class _CaptureConsole:
    """Patch logger_mod.console để bắt ANSI output của Logger."""

    def __enter__(self):
        self.buf = io.StringIO()
        self._patcher = patch.object(
            logger_mod, "console",
            Console(file=self.buf, width=200, force_terminal=True,
                    color_system="truecolor", highlight=False,
                    theme=ui_theme.load_theme(None)))
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False

    @property
    def ansi(self) -> str:
        return self.buf.getvalue()

    @property
    def plain(self) -> str:
        return _strip_ansi(self.buf.getvalue())


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        if text:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""
        self.headers = headers or {}
        self.url = "https://fake.test/"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Session giả: map (method, url-substring) -> FakeResponse (longest match)."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.calls = []
        self.cookies = {}
        self.headers = {}

    def _handle(self, method, url, **kw):
        self.calls.append((method, url))
        best, best_len = None, -1
        for m, sub, resp in self.routes:
            if m == method and sub in url and len(sub) > best_len:
                best, best_len = resp, len(sub)
        return best or FakeResponse(404, text="not found")

    def get(self, url, timeout=None, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, timeout=None, **kw):
        return self._handle("POST", url, **kw)


class FlakySession(FakeSession):
    """FakeSession raise RuntimeError từ request thứ ``fail_after`` trở đi."""

    def __init__(self, routes=None, fail_after=0):
        super().__init__(routes)
        self._n = 0
        self.fail_after = fail_after

    def _handle(self, method, url, **kw):
        self._n += 1
        if self._n > self.fail_after:
            raise RuntimeError("network down")
        return super()._handle(method, url, **kw)


# ====================================================================== #
# R-M1 — escape phần biến động tại call-site markup=True
# ====================================================================== #

class TestRM1MarkupEscapeAtCallSites(unittest.TestCase):
    """Đường production authenticate() với dữ liệu server chứa markup độc.

    Hợp đồng mỗi site: (1) không raise MarkupError; (2) tên hiện NGUYÊN VĂN;
    (3) tag trang trí vẫn render đúng màu — chữ ký SGR giống hệt khi tên là
    'alice' (nếu biến động không được escape, tag trong tên sẽ làm lệch style).
    """

    EVIL = "t[/]eam"          # driver crash: stray close-tag
    EVIL_LINK = "[link=https://evil.example]click[/link]"  # driver OSC-8

    def _assert_site(self, render_fn, evil):
        with _CaptureConsole() as cap:
            render_fn(evil)                       # KHÔNG được raise
        self.assertIn(evil, cap.plain,
                      f"dữ liệu server phải hiện nguyên văn: {cap.plain!r}")
        sig_evil = _sgr_seq(cap.ansi)
        with _CaptureConsole() as cap_ok:
            render_fn("alice")
        sig_ok = _sgr_seq(cap_ok.ansi)
        self.assertEqual(sig_evil, sig_ok,
                         "tag trang trí bị lệch style do thiếu escape")

    # --- ctfd.authenticate (users/me) ---------------------------------
    def _ctfd(self, name):
        s = FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"success": True,
                                "data": {"id": 7, "name": name}})),
        ])
        return CTFdPlatform("https://ctf.test", s)

    def test_ctfd_authenticate_escapes_server_user_name(self):
        self._assert_site(lambda n: self._ctfd(n).authenticate(), self.EVIL)

    def test_ctfd_authenticate_link_tag_renders_plain_no_osc8(self):
        with _CaptureConsole() as cap:
            self._ctfd(self.EVIL_LINK).authenticate()
        self.assertIn(self.EVIL_LINK, cap.plain)
        self.assertNotIn("\x1b]8;", cap.ansi, "không được inject OSC-8 hyperlink")

    # --- gzctf.authenticate (profile + game title/team) ---------------
    def _gzctf(self, name, email, team, title):
        s = FakeSession([
            ("GET", "/api/account/profile",
             FakeResponse(200, {"userName": name, "email": email})),
            ("GET", "/api/game/42",
             FakeResponse(200, {"title": title, "teamName": team})),
        ])
        p = GZCTFPlatform("https://gz.test/games/42/challenges", s)
        return p

    def test_gzctf_authenticate_escapes_user_email_team_title(self):
        p = self._gzctf("u[/]ser", "a@b[/]c", "t[/]eam", "T[/]itle")
        with _CaptureConsole():
            p.authenticate()                     # KHÔNG được raise
        # assert chi tiết nằm trong _assert_site-style: làm thủ công vì 4 biến
        with _CaptureConsole() as cap:
            p2 = self._gzctf("[i]u[/]", "e@x", "[b]t[/]", "[h]T")
            p2.authenticate()
        for raw in ("[i]u[/]", "[b]t[/]", "[h]T"):
            self.assertIn(raw, cap.plain)

    def test_gzctf_authenticate_benign_signature_stable(self):
        self._assert_site(
            lambda n: self._gzctf(n, "a@b.c", "teamX", "Title X").authenticate(),
            self.EVIL)

    # --- rctf.authenticate (users/me goodUserData) --------------------
    def _rctf(self, name):
        s = FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"kind": "goodUserData",
                                "data": {"name": name}})),
        ])
        return RCTFPlatform("https://r.test", s)

    def test_rctf_authenticate_escapes_server_team_name(self):
        self._assert_site(lambda n: self._rctf(n).authenticate(), self.EVIL)

    # --- custom_rest.authenticate (/api/auth/me) ----------------------
    def _custom(self, name):
        s = FakeSession([
            ("GET", "/api/auth/me",
             FakeResponse(200, {"success": True,
                                "data": {"user": {"username": name}}})),
        ])
        return CustomRESTPlatform("https://c.test", s)

    def test_custom_rest_authenticate_escapes_server_username(self):
        self._assert_site(lambda n: self._custom(n).authenticate(), self.EVIL)


# ====================================================================== #
# R-L1 — TTL-refresh chỉ swap cache SAU khi fetch thành công
# ====================================================================== #

class TestRL1AttributionTTLSafeSwap(unittest.TestCase):
    """Exception giữa chừng fetch phải GIỮ data tốt của kỳ trước + không
    stamp ts mới (để tick sau retry ngay thay vì chờ đủ TTL)."""

    def _ctfd_ok_session(self):
        return FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"success": True,
                                "data": {"id": 7, "name": "me"}})),
            ("GET", "/api/v1/teams/me", FakeResponse(401, text="no")),
            ("GET", "/api/v1/users/me/solves",
             FakeResponse(200, {"success": True, "data": [
                 {"challenge_id": 3, "user": {"id": 7, "name": "me"},
                  "date": 1756160000}]})),
        ])

    def _gzctf_ok_session(self):
        return FakeSession([
            ("GET", "/api/game/42/scoreboard",
             FakeResponse(200, {"items": [
                 {"id": 11, "name": "teamX",
                  "solvedChallenges": [{"id": 5, "userName": "me"}]}]})),
        ])

    def _rctf_ok_session(self):
        return FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"kind": "goodUserData", "data": {
                 "name": "me",
                 "solves": [{"chalId": 5, "createdAt": 1756160000000}]}})),
        ])

    def _platform(self, kind, session):
        if kind == "ctfd":
            return CTFdPlatform("https://ctf.test", session)
        if kind == "gzctf":
            p = GZCTFPlatform("https://gz.test/games/42/challenges", session)
            p.ctf_info.user_name = "me"
            p.ctf_info.team_name = "teamX"
            return p
        return RCTFPlatform("https://r.test", session)

    def _ids(self, kind):
        return [3] if kind == "ctfd" else [5]

    # -- hợp đồng chung cho cả 3 platform ------------------------------
    def _run_contract(self, kind, ok_session):
        p = self._platform(kind, ok_session())
        ids = self._ids(kind)

        out1 = p.fetch_solve_attribution(ids)
        key = ids[0]
        self.assertIn(key, out1, f"{kind}: fetch đầu phải populate cache")
        n_calls = len(p.session.calls)

        # Trong TTL: dùng cache, KHÔNG phát request mới
        out2 = p.fetch_solve_attribution(ids)
        self.assertEqual(len(p.session.calls), n_calls,
                         f"{kind}: còn TTL mà gọi lại API")
        self.assertIn(key, out2)

        # Hết TTL + network exception giữa chừng fetch:
        old_ts = p._solve_attr_ts
        old_cache = p._solve_attr_cache
        p._solve_attr_ts = old_ts - p.SOLVE_ATTR_TTL - 1
        p.session = FlakySession(fail_after=0)   # mọi request đều raise
        out3 = p.fetch_solve_attribution(ids)
        self.assertIn(key, out3,
                      f"{kind}: fail giữa chừng phải trả data cũ, không mất")
        self.assertIs(p._solve_attr_cache, old_cache,
                      f"{kind}: cache cũ bị wipe trước fetch (R-L1)")
        self.assertEqual(p._solve_attr_ts, old_ts - p.SOLVE_ATTR_TTL - 1,
                         f"{kind}: ts KHÔNG được stamp mới khi fetch fail "
                         "(để tick sau retry sớm)")

        # Session hồi phục → tick sau retry NGAY (không cần hết TTL)
        healed = ok_session()
        p.session = healed
        out4 = p.fetch_solve_attribution(ids)
        self.assertGreater(len(healed.calls), 0,
                           f"{kind}: sau fail phải retry ngay, không dùng cache rỗng")
        self.assertIn(key, out4)

    def test_ctfd_ttl_safe_swap(self):
        self._run_contract("ctfd", self._ctfd_ok_session)

    def test_gzctf_ttl_safe_swap(self):
        self._run_contract("gzctf", self._gzctf_ok_session)

    def test_rctf_ttl_safe_swap(self):
        self._run_contract("rctf", self._rctf_ok_session)

    # -- lần ĐẦU TIÊN đã fail: trả {} không raise, lần sau retry -------
    def test_first_fetch_fail_returns_empty_and_retries(self):
        for kind, ids in (("ctfd", [3]), ("gzctf", [5]), ("rctf", [5])):
            p = self._platform(kind, FakeSession())
            p.session = FlakySession(fail_after=0)
            out = p.fetch_solve_attribution(ids)
            self.assertEqual(out, {}, f"{kind}: fail lần đầu -> dict rỗng")
            self.assertIsNone(getattr(p, "_solve_attr_ts", None),
                              f"{kind}: chưa bao giờ thành công thì chưa stamp ts")
            healed = self._ctfd_ok_session() if kind == "ctfd" else (
                self._gzctf_ok_session() if kind == "gzctf"
                else self._rctf_ok_session())
            p.session = healed
            out2 = p.fetch_solve_attribution(ids)
            self.assertIn(ids[0], out2, f"{kind}: heal xong phải fetch lại được")


# ====================================================================== #
# R-L2 — prune pack: giữ dir lạ + warning, vẫn prune stale dir của tool
if __name__ == "__main__":
    unittest.main()
