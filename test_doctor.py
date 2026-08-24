"""P1-3 — ``ctf doctor`` 🩺 health-check platform trước giờ giải.

Chạy: python3 -m pytest test_doctor.py -q
Toàn bộ HTTP được mock qua session giả — KHÔNG gọi mạng thật.
Các case: all-pass | auth-fail | network-dead | render chứa icon.
"""
import io
import unittest
from unittest.mock import MagicMock

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
# 1. All-pass — mọi check ✅
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
# 2. Auth-fail — cookie hết hạn → chỉ check Auth ❌
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
# 3. Network-dead — mọi check ❌ nhưng report vẫn render đủ
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

        # Report vẫn render đầy đủ 6 dòng + tổng kết, không raise
        out = capture_render(report)
        self.assertIn("ctf doctor", out)
        self.assertIn("Tổng kết: 0/6 checks pass", out)
        self.assertIn("Không kết nối được", out)


# ----------------------------------------------------------------------
# 4. Render chứa icon + tổng kết X/Y
# ----------------------------------------------------------------------

class TestDoctorRender(unittest.TestCase):
    def test_render_contains_icons_and_summary(self):
        svc = HealthService()
        report = svc.check(URL, cookie="session=abc", session=ctfd_session())
        out = capture_render(report)

        for icon in ("🌐", "🔍", "🔑", "🧩", "⏱️", "🏴"):
            self.assertIn(icon, out, f"thiếu icon {icon} trong render")
        self.assertIn("Tổng kết: 6/6 checks pass", out)
        self.assertIn("sẵn sàng cho giờ giải", out)
        self.assertIn(URL, out)

    def test_partial_report_summary(self):
        report = DoctorReport(url=URL)
        report.add("A", True, "ok", icon="🌐")
        report.add("B", False, "bad", icon="🔑")
        out = capture_render(report)
        self.assertIn("Tổng kết: 1/2 checks pass", out)
        self.assertIn("❌", out)
        self.assertIn("✅", out)

    def test_doctor_check_fields(self):
        chk = DoctorCheck(name="X", ok=True, detail="d", icon="🧩")
        self.assertEqual((chk.name, chk.ok, chk.detail, chk.icon),
                         ("X", True, "d", "🧩"))


if __name__ == "__main__":
    unittest.main()
