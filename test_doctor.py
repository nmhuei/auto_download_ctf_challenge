"""P1-3 — ``ctf doctor`` health-check platform trước giờ giải.

Chạy: python3 -m pytest test_doctor.py -q
Toàn bộ HTTP được mock qua session giả — KHÔNG gọi mạng thật.
Các case: all-pass | auth-fail | network-dead | render glyph PHOSPHOR.
"""
import io
import unittest
from unittest.mock import MagicMock, patch

from rich.console import Console

from ctf_downloader.platforms.base import EventTimes  # noqa: F401
from ctf_downloader.services.health_service import (
    DoctorCheck,
    DoctorReport,
    HealthService,
)


# ----------------------------------------------------------------------
# Helpers — mock session theo mẫu test_event_window.py
# ----------------------------------------------------------------------

def make_resp(status_code=200, json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no json")
    r.text = text if text != "" else (
        __import__("json").dumps(json_data) if json_data is not None else "")
    r.headers = headers or {}
    return r


def make_mock_session(routes):
    """routes: list[(fragment, response)] — khớp theo thứ tự, fallback 404."""
    s = MagicMock()

    def get(url, *a, **kw):
        for frag, resp in routes:
            if frag in url:
                return resp
        return make_resp(404)

    def post(url, *a, **kw):
        return make_resp(404)

    s.get.side_effect = get
    s.post.side_effect = post
    return s


BASE_HTML = """
<html><head><title>Test CTF</title></head><body>
<script>window.init = { 'csrfNonce': 'abc123', 'start': '1756000000',
'end': '4102444800' };</script>
<footer>Powered by CTFd</footer>
</body></html>
"""

RULES_HTML = """
<html><head><title>Competition Rules</title></head><body>
<h1>Rules</h1>
<p>All flags follow the format /^FLAG\\{.*\\}$/ exactly.</p>
<p>No attacking the scoreboard infrastructure.</p>
</body></html>
"""


def ctfd_session(users_me_status=200, challenges_status=200):
    """Session giả cho một platform CTFd khoẻ mạnh."""
    return make_mock_session([
        ("/api/v1/users/me",
         make_resp(users_me_status,
                   json_data={"success": True, "data": {"name": "tester"}}
                   ) if users_me_status == 200 else make_resp(users_me_status)),
        ("/api/v1/challenges",
         make_resp(challenges_status,
                   json_data={"success": True, "data": []}
                   ) if challenges_status == 200 else make_resp(challenges_status)),
        ("/rules", make_resp(200, text=RULES_HTML)),
        ("/challenges", make_resp(200, text=BASE_HTML)),
        ("", make_resp(200, text=BASE_HTML)),   # base URL
    ])


def capture_render(report):
    buf = io.StringIO()
    report.render(console=Console(file=buf, width=200))
    return buf.getvalue()


URL = "https://ctf.test/"


# ----------------------------------------------------------------------
# 1. All-pass — mọi check ✔
# ----------------------------------------------------------------------

class TestDoctorAllPass(unittest.TestCase):
    def test_all_checks_pass(self):
        svc = HealthService()
        report = svc.check(URL, cookie="session=abc", session=ctfd_session())

        self.assertEqual(report.total, 6)
        self.assertTrue(report.all_passed(),
                        f"còn fail: {[c.name for c in report.checks if not c.ok]}")
        self.assertEqual(report.passed, 6)

        names = [c.name for c in report.checks]
        for expected in ("URL sống", "Platform detect", "Auth hợp lệ",
                         "Capabilities", "Event window", "Flag format"):
            self.assertIn(expected, names)

        # Detect ra CTFd với confidence high
        det = next(c for c in report.checks if c.name == "Platform detect")
        self.assertIn("CTFd", det.detail)
        # Auth nhận được user
        auth = next(c for c in report.checks if c.name == "Auth hợp lệ")
        self.assertIn("tester", auth.detail)
        # Event window có cả start/end + trạng thái
        win = next(c for c in report.checks if c.name == "Event window")
        self.assertIn("UTC", win.detail)
        self.assertIn("ĐANG DIỄN RA", win.detail)
        # Flag format tìm thấy regex neo
        ff = next(c for c in report.checks if c.name == "Flag format")
        self.assertIn("^FLAG\\{.+\\}$", ff.detail)


# ----------------------------------------------------------------------
# 2. Auth-fail — cookie hết hạn → chỉ check Auth ✗
# ----------------------------------------------------------------------

class TestDoctorAuthFail(unittest.TestCase):
    def test_auth_fail_marks_only_auth(self):
        svc = HealthService()
        report = svc.check(
            URL, cookie="session=expired",
            session=ctfd_session(users_me_status=403, challenges_status=403))

        auth = next(c for c in report.checks if c.name == "Auth hợp lệ")
        self.assertFalse(auth.ok)
        self.assertIn("cookie hết hạn", auth.detail)

        # Các check khác vẫn phải chạy bình thường
        det = next(c for c in report.checks if c.name == "Platform detect")
        self.assertTrue(det.ok)
        url_chk = next(c for c in report.checks if c.name == "URL sống")
        self.assertTrue(url_chk.ok)
        self.assertEqual(report.passed, 5)

    def test_no_credentials_is_auth_fail(self):
        svc = HealthService()
        report = svc.check(URL, session=ctfd_session())
        auth = next(c for c in report.checks if c.name == "Auth hợp lệ")
        self.assertFalse(auth.ok)
        self.assertIn("cookie/token", auth.detail.lower())


# ----------------------------------------------------------------------
# 3. Network-dead — mọi check ✗ nhưng report vẫn render đủ
# ----------------------------------------------------------------------

class TestDoctorNetworkDead(unittest.TestCase):
    def _dead_session(self):
        import requests

        s = MagicMock()
        s.get.side_effect = requests.exceptions.ConnectionError("network dead")
        s.post.side_effect = requests.exceptions.ConnectionError("network dead")
        return s

    def test_all_fail_but_report_renders(self):
        svc = HealthService()
        dead = self._dead_session()
        report = svc.check(URL, cookie="session=abc", session=dead)

        self.assertEqual(report.total, 6)
        self.assertEqual(report.passed, 0)
        self.assertFalse(report.all_passed())
        for chk in report.checks:
            self.assertFalse(chk.ok, f"{chk.name} không được pass khi mạng chết")

        # Report vẫn render đầy đủ 6 dòng + tổng kết, không raise.
        # (codex-r2: header giờ là AppHeader — "CTF·TOOLKIT │ doctor · url")
        out = capture_render(report)
        self.assertIn("CTF·TOOLKIT", out)
        self.assertIn("doctor", out)
        self.assertIn("Tổng kết: 0/6 checks pass", out)
        self.assertIn("Không kết nối được", out)


# ----------------------------------------------------------------------
# 4. Render chứa icon + tổng kết X/Y
# ----------------------------------------------------------------------

class TestDoctorRender(unittest.TestCase):
    def test_render_contains_glyphs_and_summary(self):
        svc = HealthService()
        report = svc.check(URL, cookie="session=abc", session=ctfd_session())
        out = capture_render(report)

        # codex-r2 P0b: surface doctor dùng AppHeader chuẩn như các lệnh khác
        # (▐██ CTF·TOOLKIT │ doctor · <url>) thay vì title "ctf doctor" thuần.
        self.assertIn("CTF·TOOLKIT", out)
        self.assertIn("doctor", out)
        # Glyph semantic ✔ một lần mỗi dòng đạt; heading CHECK/KẾT QUẢ faint.
        self.assertIn("✔", out)
        self.assertIn("CHECK", out)
        self.assertIn("KẾT QUẢ", out)
        self.assertIn("Tổng kết: 6/6 checks pass", out)
        self.assertIn("sẵn sàng cho giờ giải", out)
        self.assertIn(URL, out)
        # Không emoji chrome (quy tắc glyph PHOSPHOR).
        for bad in ("🌐", "🔍", "🔑", "🧩", "⏱️", "🏴",
                    "✅", "❌", "🩺", "🔴"):
            self.assertNotIn(bad, out, f"còn emoji chrome {bad} trong render")

    def test_partial_report_diagnostic_mini(self):
        report = DoctorReport(url=URL)
        report.add("A", True, "ok")
        report.add("B", False, "nguyên nhân X",
                   fix="chạy lệnh fix --flag-format")
        out = capture_render(report)
        self.assertIn("Tổng kết: 1/2 checks pass", out)
        # Diagnostic mini: ✗ tên → ╰─▶ nguyên nhân → ℹ lệnh fix.
        self.assertIn("✗", out)
        self.assertIn("╰─▶", out)
        self.assertIn("ℹ", out)
        self.assertIn("--flag-format", out)

    def test_doctor_check_fields(self):
        chk = DoctorCheck(name="X", ok=True, detail="d", fix="fix cmd")
        self.assertEqual((chk.name, chk.ok, chk.detail, chk.fix),
                         ("X", True, "d", "fix cmd"))


# ----------------------------------------------------------------------
# 5. codex-r2 P0: màu vai trò + AppHeader + natural width (ANSI thật)
# ----------------------------------------------------------------------

def capture_render_ansi(report) -> str:
    buf = io.StringIO()
    report.render(console=Console(
        file=buf, width=200, force_terminal=True, color_system="truecolor"))
    return buf.getvalue()


class TestDoctorPhosphorRoles(unittest.TestCase):
    FG_MUTED_ANSI = "\x1b[38;2;153;145;126m"
    # codex-r3 #1: error-red/solved-green hex mới (bỏ #FF5C57/#46C46B).
    # Amber Refit (palette C1): fg/muted/solved retune, accent giữ #FFB000.
    ERROR_ANSI = "\x1b[38;2;229;83;75m"      # ERROR  = #E5534B
    WARN_ANSI = "\x1b[38;2;234;197;79m"
    ACCENT_ANSI = "\x1b[38;2;255;176;0m"
    SOLVED_ANSI = "\x1b[38;2;98;201;126m"    # SOLVED = #62C97E
    ACCENT_DEEP_ANSI = "\x1b[38;2;107;67;0m"  # ACCENT_DEEP = #6B4300

    def _partial_report(self):
        report = DoctorReport(url=URL)
        report.add("A", True, "ok")
        report.add("B", False, "nguyên nhân X",
                   fix="chạy lệnh fix --flag-format")
        return report

    def test_branch_connector_is_muted_not_red(self):
        """codex-r2 P0c: ``╰─▶`` là connector cấu trúc → muted; đỏ chỉ dành
        cho glyph kết quả ✗."""
        out = capture_render_ansi(self._partial_report())
        self.assertIn(self.FG_MUTED_ANSI + "╰─▶", out)
        self.assertNotIn(self.ERROR_ANSI + "╰─▶", out)

    def test_summary_warn_glyph_amber_accent_text(self):
        """Tổng kết chưa pass đủ: glyph ``!`` warn #EAC54F, phần text accent
        amber — hai vai trò riêng, không tô cả cụm một màu."""
        out = capture_render_ansi(self._partial_report())
        self.assertIn(self.WARN_ANSI + "!", out)
        self.assertIn(self.ACCENT_ANSI + "Tổng kết: 1/2 checks pass", out)

    def test_result_glyphs_semantically_colored(self):
        out = capture_render_ansi(self._partial_report())
        self.assertIn(self.SOLVED_ANSI + "✔", out)
        self.assertIn(self.ERROR_ANSI + "✗", out)

    def test_ok_label_outside_green_span(self):
        """synthesis-v6 MF1: semantic green CHỈ bọc glyph ✔ — ``Text(OK,
        style=SOLVED)`` cũ đặt base-style SOLVED cho cả object nên nhuộm
        green cả nhãn tên check lẫn dòng tổng kết. Sau fix: span green đóng
        NGAY sau glyph, nhãn/text nằm ngoài."""
        svc = HealthService()
        report = svc.check(URL, cookie="session=abc", session=ctfd_session())
        out = capture_render_ansi(report)
        # Dạng lỗi cũ: cả cụm glyph+nhãn trong MỘT span solved.
        self.assertNotIn(self.SOLVED_ANSI + "✔     URL sống", out)
        self.assertNotIn(self.SOLVED_ANSI + "Tổng kết", out)
        # Glyph vẫn xanh đúng vai trò; reset NGAY sau glyph (nhãn outside).
        self.assertIn(self.SOLVED_ANSI + "✔\x1b[0m", out)
        # Nhãn check vẫn hiện đầy đủ, không mất chữ sau khi tách span.
        self.assertIn("URL sống", out)
        self.assertIn("Tổng kết: 6/6 checks pass", out)

    def test_no_trailing_whitespace_padding_to_width(self):
        """codex-r2 P0c: bỏ padding trắng kéo dòng tới hết 80 cột — bảng
        natural width, không dòng nào kết thúc bằng run-space."""
        svc = HealthService()
        report = svc.check(URL, cookie="session=abc", session=ctfd_session())
        out = capture_render_ansi(report)
        import re as _re
        for ln in out.splitlines():
            plain = _re.sub(r"\x1b\[[0-9;]*m", "", ln)
            self.assertFalse(plain != plain.rstrip(),
                             f"dòng còn đệm trắng cuối: {plain!r}")

    def test_app_header_present_with_brand_block(self):
        out = capture_render_ansi(self._partial_report())
        # combo B Phosphor Radar: dải scanline accent.deep mở đầu AppHeader,
        # brand ``CTF·TOOLKIT`` bold amber ở dòng 2 (căn giữa).
        self.assertIn(self.ACCENT_DEEP_ANSI + "░", out)
        self.assertIn("\x1b[1;" + self.ACCENT_ANSI[2:] + " CTF·TOOLKIT ", out)
        self.assertIn(URL, out)

    def test_capabilities_values_colored_individually(self):
        """codex-r2 P0c: Capabilities ✔/✗ tô semantic TỪNG giá trị thay vì
        nhét glyph vào một chuỗi detail muted."""
        report = DoctorReport(url=URL)
        report.add("Capabilities", True,
                   caps=[("container động", True), ("scoreboard", False)])
        out = capture_render_ansi(report)
        # glyph ✔/✗ tô semantic riêng từng giá trị; nhãn muted
        self.assertIn(self.SOLVED_ANSI + "✔", out)
        self.assertIn(self.ERROR_ANSI + "✗", out)
        self.assertIn("\x1b[38;2;153;145;126m container động", out)


class TestDoctorChromeAndWrap(unittest.TestCase):
    """codex-r3 #2/#3: FooterBar cuối surface + wrap continuation thụt đúng
    cột nội dung (không về cột 1)."""

    def test_footer_bar_is_last_line(self):
        out = capture_render(self._partial())
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertIn("q thoát", lines[-1])
        self.assertIn("di chuyển", lines[-1])
        self.assertIn("?", lines[-1])

    def _partial(self):
        report = DoctorReport(url=URL)
        report.add("A", True, "ok")
        report.add("B", False, "nguyên nhân X",
                   fix="chạy lệnh fix --flag-format")
        return report

    def test_wrap_continuation_indented_to_content_column(self):
        # Detail dài hơn width → phải tự chia chunk với thụt đầu dòng,
        # KHÔNG để rich soft-wrap đẩy continuation về cột 1.
        report = DoctorReport(url=URL)
        report.add("Event window", False,
                   "Bắt đầu 2026-08-24 09:00 UTC — kết thúc "
                   "2026-08-25 17:00 UTC → ĐANG DIỄN RA (LIVE) "
                   + "còn lại nhiều nội dung nữa để ép xuống dòng ",
                   fix="Kiểm tra kết nối rồi nhập tay lại tham số "
                       + "bằng lệnh fix rất dài để tràn bề rộng khung ")
        buf = io.StringIO()
        report.render(console=Console(file=buf, width=80))
        lines = buf.getvalue().splitlines()
        # Có ít nhất 1 continuation nằm đúng cột sau nhãn ``╰─▶ `` (cột 10).
        cont = [ln for ln in lines if ln.startswith(" " * 10)
                and ln.strip()]
        self.assertTrue(cont, f"không có continuation thụt cột nội dung:\n"
                              f"{buf.getvalue()}")
        # Mọi dòng không thuộc dạng dòng đầu khối đều phải được thụt lề.
        # (combo B: 4 dòng AppHeader radar — scanline / title dot-wing /
        # ▍ lệnh / ▸ timestamp — đều hợp lệ mở đầu ở cột 1.)
        allowed_prefixes = ("▐██", "CHECK", "KẾT QUẢ", "!", "✔", "✗", "↑↓",
                            "░░", "·", "▍", "▸")
        for ln in lines:
            if not ln.strip() or ln.startswith(allowed_prefixes):
                continue
            self.assertTrue(ln.startswith(" "),
                            f"continuation về cột 1: {ln!r}")

    def test_ok_detail_wrap_indents_to_label_column(self):
        report = DoctorReport(url=URL)
        report.add("Event window", True,
                   "Chi tiết rất dài " + "w" * 200)
        buf = io.StringIO()
        report.render(console=Console(file=buf, width=80))
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        conts = [ln for ln in lines if ln.startswith(" " * 6)]
        self.assertTrue(conts,
                        "detail của check ✔ phải wrap về cột nội dung 6")


class TestDetectQuiet(unittest.TestCase):
    def test_health_service_calls_detector_quiet(self):
        """codex-r2 P0a: doctor gọi detector với quiet=True — không log
        "[*] Detected Platform" 16-color lẫn vào surface PHOSPHOR."""
        from ctf_downloader.platforms.detector import detect_platform_info

        with patch("ctf_downloader.platforms.detector.detect_platform_info") as m_det:
            fake_platform = MagicMock()
            fake_platform.authenticate.return_value = True
            fake_platform.fetch_event_times.return_value = None
            fake_platform.fetch_rules.return_value = None
            m_det.return_value = (fake_platform, MagicMock(
                platform_type="ctfd", confidence="high",
                capabilities={"container": True, "scoreboard": True,
                              "rules_via_api": True}))
            svc = HealthService()
            svc.check(URL, cookie="session=abc")
            _, kwargs = m_det.call_args
            self.assertTrue(kwargs.get("quiet"))

    def test_quiet_true_suppresses_detection_log(self):
        import contextlib
        import io as _io

        from ctf_downloader.platforms import detection

        with contextlib.redirect_stderr(_io.StringIO()), \
                patch.object(detection.Logger, "info") as m_info, \
                patch.object(detection.Logger, "warning") as m_warn:
            detection.detect_platform_info(URL, ctfd_session(), quiet=True)
            for call in list(m_info.call_args_list) + list(m_warn.call_args_list):
                self.assertNotIn("Detected Platform", str(call))


if __name__ == "__main__":
    unittest.main()
