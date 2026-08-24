"""WebDashboard (P2-5 ``ctf serve``) — unit/integration tests.

Chạy: python3 -m pytest test_web_dashboard.py -q
Toàn bộ qua fake repo / server loopback ephemeral port — KHÔNG mạng thật,
KHÔNG ghi vào workspace.
"""
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ctf_downloader.services.web_dashboard import WebDashboard


# ----------------------------------------------------------------------
# Fake repo — đủ interface StatusService cần: root, read_challenges(),
# iter_challenges(), read_metadata(), is_container().
# ----------------------------------------------------------------------
class FakeRepo:
    def __init__(self, tmp: Path):
        self.root = tmp
        # Tạo cấu trúc thư mục thật tối thiểu để scan đếm local files được.
        for mid in (1, 2):
            d = tmp / str(mid) / "challenge"
            d.mkdir(parents=True, exist_ok=True)
            (d / "dist.zip").write_bytes(b"x")
        self.challenges_data = {
            "ctf_info": {
                "title": "FakeCTF",
                "platform": "ctfd",
                "url": "https://fake.example.com",
                "user": "player1",
                "team": "teamX",
            },
            "challenges": [
                {"id": 1, "name": "<script>alert(1)</script>", "category": "Web",
                 "points": 100},
                {"id": 2, "name": "Safe Crypto", "category": "Crypto",
                 "points": 200},
            ],
        }
        self.metas = {
            1: {"id": 1, "name": "<script>alert(1)</script>",
                "category": "Web", "points": 100, "solved_by_me": False,
                "_status_like": None},
            2: {"id": 2, "name": "Safe Crypto", "category": "Crypto",
                "points": 200, "solved_by_me": True},
        }

    # --- WorkspaceRepo API dùng bởi StatusService.scan_local_challenges ---
    def read_challenges(self):
        return dict(self.challenges_data)

    def iter_challenges(self):
        return [self.root / str(mid) / "metadata.json"
                for mid in sorted(self.metas)]

    def read_metadata(self, path):
        mid = int(Path(path).parent.name)
        meta = dict(self.metas[mid])
        # StatusService.compute_status gắn _status; fake trả sẵn trạng thái
        # đa chiều để không phụ thuộc đĩa.
        if mid == 2:
            meta["status"] = {"solve": "solved_by_me", "flag": {"state": "submitted_correct"},
                              "writeup": "draft", "labels": ["todo-review"], "notes": "đã pwn"}
        else:
            meta["status"] = {"solve": "working", "flag": {"state": "hoarded"},
                              "writeup": "skeleton", "labels": ["pwn-plan"],
                              "notes": "thử SSTI"}
        return meta

    def read_status(self, meta_path):
        return self.read_metadata(meta_path).get("status") or {}

    def is_container(self, meta):
        return False


class WebDashboardTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="web_dash_"))
        self.repo = FakeRepo(self.tmp)
        self.dash = WebDashboard(self.repo)


class TestRendering(WebDashboardTestCase):
    def test_page_contains_title_and_escapes_script(self):
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        # Tên giải xuất hiện
        self.assertIn("FakeCTF", page)
        # Tên challenge độc bị escape — KHÔNG có <script> sống trong body
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_badge_uses_status_icons(self):
        from ctf_downloader.storage.constants import STATUS_ICONS
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        # Icon trục solve của chall đang working + solved_by_me đều phải đến
        # từ STATUS_ICONS constants (không hardcode ngoài design system).
        self.assertIn(STATUS_ICONS["solve"]["working"], page)
        self.assertIn(STATUS_ICONS["solve"]["solved_by_me"], page)
        self.assertIn(STATUS_ICONS["flag"]["hoarded"], page)
        self.assertIn(STATUS_ICONS["writeup"]["draft"], page)

    def test_filters_cat_and_q(self):
        data = self.dash.collect()
        only_web = WebDashboard.apply_filters(data, cat="web")
        self.assertEqual([c["id"] for c in only_web], [1])
        by_q = WebDashboard.apply_filters(data, q="ssti")   # khớp notes chall 1
        self.assertEqual([c["id"] for c in by_q], [1])
        by_label = WebDashboard.apply_filters(data, label="todo-review")
        self.assertEqual([c["id"] for c in by_label], [2])

    def test_auto_refresh_meta(self):
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertIn('http-equiv="refresh"', page)
        self.assertIn('content="30"', page)


class TestJsonApi(WebDashboardTestCase):
    def test_status_json_valid(self):
        raw = self.dash.status_json().decode("utf-8")
        payload = json.loads(raw)  # phải là JSON hợp lệ
        self.assertEqual(payload["title"], "FakeCTF")
        self.assertEqual(payload["solved_challenges"], 1)
        self.assertEqual(payload["total_challenges"], 2)
        names = [c["name"] for c in payload["challenges"]]
        self.assertIn("<script>alert(1)</script>", names)  # JSON giữ nguyên text
        self.assertIn("categories", payload)


class TestHttpServer(WebDashboardTestCase):
    """HTTP-level: chạy ThreadingHTTPServer trên port ephemeral."""

    def setUp(self):
        super().setUp()
        self.httpd = self.dash.make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_get_root_html_escapes_xss(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as resp:
            html_body = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIn("FakeCTF", html_body)
        self.assertNotIn("<script>alert(1)</script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_get_api_status_json_valid(self):
        with urllib.request.urlopen(self._url("/api/status.json"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(payload["title"], "FakeCTF")

    def test_post_returns_405(self):
        req = urllib.request.Request(
            self._url("/"), data=b"x=1", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)
        self.assertIn("GET", ctx.exception.headers.get("Allow", ""))

    def test_unknown_path_404(self):
        try:
            urllib.request.urlopen(self._url("/nope"), timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_busy_port_raises_oserror_with_clear_message(self):
        # Chiếm port bằng socket LISTENING rồi thử serve trên đúng port đó.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]
            with self.assertRaises(OSError) as ctx:
                self.dash.serve(host="127.0.0.1", port=busy_port)
            msg = str(ctx.exception)
            self.assertIn(str(busy_port), msg)
            self.assertTrue(
                "bận" in msg or "port" in msg.lower(),
                f"message phải nói rõ nguyên nhân port: {msg!r}")
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
