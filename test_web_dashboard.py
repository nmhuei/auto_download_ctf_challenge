"""WebDashboard (P2-5 ``ctf serve``) — unit/integration tests.

Chạy: python3 -m pytest test_web_dashboard.py -q
Toàn bộ qua fake repo / server loopback ephemeral port — KHÔNG mạng thật,
KHÔNG ghi vào workspace.

v2: thêm test POST /api/submit (submit QUA SubmitService — inject fake qua
``submit_factory``), rate-limit 5s/session, CSRF-lite X-Requested-With,
prefill flag hoarded + escape mọi echo.
"""
import json
import socket
import threading
import time
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
        # Status đa chiều trả cho compute_status — test v2 mutate trực tiếp.
        self.statuses = {
            1: {"solve": "working", "flag": {"state": "hoarded"},
                "writeup": "skeleton", "labels": ["pwn-plan"],
                "notes": "thử SSTI"},
            2: {"solve": "solved_by_me",
                "flag": {"state": "submitted_correct"},
                "writeup": "draft", "labels": ["todo-review"], "notes": "đã pwn"},
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
        st = self.statuses.get(mid) or {}
        meta["status"] = json.loads(json.dumps(st))  # deep-copy rẻ
        return meta

    def read_status(self, meta_path, meta=None):
        # kwarg meta khớp WorkspaceRepo.read_status — compute_status truyền
        # metadata đã đọc xuyên suốt (perf: tránh double-read).
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

    def test_status_json_strips_ansi_from_server_strings(self):
        """hunt-c20 LOW: OSC/CSI từ tên challenge/team/platform thực thi
        được trên terminal qua ``curl /api/status.json`` → mọi trường string
        phải qua strip_ansi trước json.dumps."""
        self.repo.challenges_data["ctf_info"]["team"] = "\x1b]0;pwned\x07teamX"
        self.repo.challenges_data["ctf_info"]["platform"] = "ctf\x1b[31md"
        self.repo.metas[2]["name"] = "Safe\x1b[31mCrypto"
        raw = self.dash.status_json().decode("utf-8")
        self.assertNotIn("\x1b", raw, "còn ESC sequence trong JSON API")
        payload = json.loads(raw)
        self.assertEqual(payload["team"], "teamX")
        self.assertEqual(payload["platform"], "ctfd")
        names = [c["name"] for c in payload["challenges"]]
        self.assertIn("SafeCrypto", names)


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

    def _raw_head(self, path):
        """HEAD qua RAW SOCKET — http.client/urllib tự nuốt body cho HEAD
        nên không phát hiện được server vẫn ghi byte ra wire."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("HEAD", path)
            resp = conn.getresponse()
            status = resp.status
            length = int(resp.headers.get("Content-Length", "0") or 0)
            # Đọc TOÀN BỘ còn lại trên wire sau headers — nếu server tự ý
            # wfile.write(body) thì byte đó nằm đây.
            rest = resp.fp.read()
        finally:
            conn.close()
        return status, length, rest

    def test_head_request_returns_headers_without_body(self):
        """hunt-c20 LOW: HEAD delegate do_GET vẫn wfile.write(body) —
        response HEAD không được mang byte body nào (curl -I an toàn)."""
        for path in ("/api/status.json", "/"):
            with self.subTest(path=path):
                status, length, rest = self._raw_head(path)
                self.assertEqual(status, 200)
                # Content-Length vẫn khai báo size body thật (GET tương ứng)
                self.assertGreater(length, 0)
                self.assertEqual(rest, b"",
                                 f"HEAD {path} vẫn ghi {len(rest)} byte body ra wire")

    def test_head_unknown_path_404_no_body(self):
        status, length, rest = self._raw_head("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(rest, b"")

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


# ----------------------------------------------------------------------
# v2 — POST /api/submit
# ----------------------------------------------------------------------
class FakeSubmitter:
    """Thay SubmitService: ghi nhận call, không chạm mạng."""

    def __init__(self, result=(True, "Correct!"), exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def submit(self, challenge, flag, force=False):
        self.calls.append((challenge, flag))
        if self.exc is not None:
            raise self.exc
        return self.result


class TestSubmitEndpointUnit(WebDashboardTestCase):
    """handle_submit_request trực tiếp (không qua HTTP)."""

    def _hdr(self):
        return {"X-Requested-With": "XMLHttpRequest"}

    def test_valid_post_calls_service_and_returns_ok(self):
        fake = FakeSubmitter()
        self.dash._submit_factory = lambda: fake
        code, payload, retry = self.dash.handle_submit_request(
            json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode(),
            self._hdr())
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "Correct!")
        self.assertIsNone(retry)
        self.assertEqual(fake.calls, [(1, "FLAG{x}")])

    def test_missing_challenge_returns_400(self):
        fake = FakeSubmitter()
        self.dash._submit_factory = lambda: fake
        for body in ({"flag": "FLAG{x}"}, {"challenge": "", "flag": "F"},
                     {"challenge": 1}, {}):
            with self.subTest(body=body):
                code, payload, _ = self.dash.handle_submit_request(
                    json.dumps(body).encode(), self._hdr())
                self.assertEqual(code, 400)
                self.assertFalse(payload["ok"])
        self.assertEqual(fake.calls, [])  # service không bị gọi

    def test_invalid_json_body_400(self):
        code, payload, _ = self.dash.handle_submit_request(
            b"not-json{{", self._hdr())
        self.assertEqual(code, 400)
        self.assertFalse(payload["ok"])

    def test_csrf_header_required(self):
        fake = FakeSubmitter()
        self.dash._submit_factory = lambda: fake
        body = json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode()
        for hdrs in ({}, {"X-Requested-With": "form-data"}):
            with self.subTest(hdrs=hdrs):
                code, _, _ = self.dash.handle_submit_request(body, hdrs)
                self.assertEqual(code, 403)
        self.assertEqual(fake.calls, [])

    def test_rate_limit_two_consecutive_calls_429(self):
        fake = FakeSubmitter()
        self.dash._submit_factory = lambda: fake
        body = json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode()
        hdr = self._hdr()
        code1, _, _ = self.dash.handle_submit_request(body, hdr)
        code2, payload2, retry2 = self.dash.handle_submit_request(body, hdr)
        self.assertEqual(code1, 200)
        self.assertEqual(code2, 429)
        self.assertFalse(payload2["ok"])
        self.assertGreaterEqual(int(retry2 or 0), 1)
        self.assertIn("retry_after", payload2)
        self.assertEqual(len(fake.calls), 1)  # request 2 không chạm service

    def test_rate_limit_reset_after_cooldown(self):
        from unittest.mock import patch

        import ctf_downloader.services.web_dashboard as wd
        fake = FakeSubmitter()
        self.dash._submit_factory = lambda: fake
        body = json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode()
        hdr = self._hdr()
        t0 = [time.monotonic()]

        def fake_mono():
            return t0[0]

        with patch.object(wd.time, "monotonic", fake_mono):
            self.assertEqual(
                self.dash.handle_submit_request(body, hdr)[0], 200)
            # +6s (> cooldown 5s) → được submit tiếp
            t0[0] += self.dash.SUBMIT_COOLDOWN + 1
            self.assertEqual(
                self.dash.handle_submit_request(body, hdr)[0], 200)
        self.assertEqual(len(fake.calls), 2)

    def test_service_exception_returns_500(self):
        self.dash._submit_factory = lambda: FakeSubmitter(exc=RuntimeError("boom"))
        code, payload, _ = self.dash.handle_submit_request(
            json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode(),
            self._hdr())
        self.assertEqual(code, 500)
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["message"])

    def test_factory_failure_returns_503(self):
        def _broken():
            raise RuntimeError("no network")
        self.dash._submit_factory = _broken
        code, payload, _ = self.dash.handle_submit_request(
            json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode(),
            self._hdr())
        self.assertEqual(code, 503)
        self.assertFalse(payload["ok"])


class TestSubmitUI(WebDashboardTestCase):
    """Ô input flag + nút Submit chỉ trên hàng OPEN/working; prefill hoarded."""

    def test_submit_button_only_on_submittable_rows(self):
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        # chall 1 đang working → có nút; chall 2 solved_by_me → không.
        self.assertIn('data-submit-challenge="1"', page)
        self.assertNotIn('data-submit-challenge="2"', page)

    def test_flag_prefill_when_hoarded(self):
        self.repo.statuses[1]["flag"] = {
            "state": "hoarded", "value": "PTITCTF{h04rd3d}"}
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertIn('value="PTITCTF{h04rd3d}"', page)

    def test_no_prefill_for_non_hoarded_state(self):
        self.repo.statuses[1]["flag"] = {
            "state": "found_unverified", "value": "PTITCTF{maybe}"}
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertNotIn("PTITCTF{maybe}", page)

    def test_xss_echo_prefill_value_escaped(self):
        evil = '"><script>alert(9)</script>'
        self.repo.statuses[1]["flag"] = {"state": "hoarded", "value": evil}
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertNotIn("<script>alert(9)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_toast_uses_textcontent_not_innerhtml(self):
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertIn("textContent", page)
        self.assertNotIn(".innerHTML", page)

    def test_inline_fetch_sets_csrf_header(self):
        page = self.dash.render_page(self.dash.collect()).decode("utf-8")
        self.assertIn("'X-Requested-With': 'XMLHttpRequest'", page)
        self.assertIn("/api/submit", page)


class TestSubmitHttp(TestHttpServer):
    """HTTP-level POST /api/submit trên server thật (loopback ephemeral)."""

    def _post(self, path="/api/submit", body=None, csrf=True,
              extra=None, method="POST"):
        data = json.dumps(body if body is not None
                          else {"challenge": 1, "flag": "FLAG{x}"}).encode()
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-Requested-With"] = "XMLHttpRequest"
        headers.update(extra or {})
        req = urllib.request.Request(self._url(path), data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8")), \
                    resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw}
            return e.code, payload, e.headers

    def setUp(self):
        super().setUp()
        self.fake = FakeSubmitter()
        self.dash._submit_factory = lambda: self.fake

    def test_post_submit_valid_over_http(self):
        code, payload, headers = self._post(
            body={"challenge": 1, "flag": "FLAG{over_http}"})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "Correct!")
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(self.fake.calls, [(1, "FLAG{over_http}")])

    def test_post_submit_missing_challenge_400(self):
        code, payload, _ = self._post(body={"flag": "FLAG{x}"})
        self.assertEqual(code, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.fake.calls, [])

    def test_post_submit_rate_limited_429_with_retry_after(self):
        code1, _, _ = self._post()
        code2, payload2, headers2 = self._post()
        self.assertEqual(code1, 200)
        self.assertEqual(code2, 429)
        self.assertFalse(payload2["ok"])
        self.assertGreaterEqual(int(headers2.get("Retry-After", "0")), 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_post_without_csrf_header_403(self):
        code, _, _ = self._post(csrf=False)
        self.assertEqual(code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_post_other_path_still_405(self):
        code, payload, headers = self._post(path="/")
        self.assertEqual(code, 405)
        self.assertIn("GET", headers.get("Allow", ""))

    def test_put_delete_patch_on_submit_path_405(self):
        for meth in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=meth):
                req = urllib.request.Request(
                    self._url("/api/submit"),
                    data=json.dumps({"challenge": 1, "flag": "x"}).encode(),
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    method=meth)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 405)

    def test_v1_get_paths_still_work_alongside_v2(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as r1:
            self.assertEqual(r1.status, 200)
            html_body = r1.read().decode("utf-8")
        with urllib.request.urlopen(self._url("/api/status.json"),
                                    timeout=5) as r2:
            self.assertEqual(r2.status, 200)
            payload = json.loads(r2.read().decode("utf-8"))
        self.assertIn("FakeCTF", html_body)
        self.assertNotIn("<script>alert(1)</script>", html_body)
        self.assertEqual(payload["title"], "FakeCTF")

    # --- POST path parsing: querystring OK, trailing slash / path lạ → 405 ---
    def test_post_with_querystring_accepted(self):
        code, payload, _ = self._post(path="/api/submit?src=ui",
                                      body={"challenge": 1, "flag": "Q"})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_post_trailing_slash_405(self):
        code, _, headers = self._post(path="/api/submit/",
                                      body={"challenge": 1, "flag": "x"})
        self.assertEqual(code, 405)
        self.assertIn("POST", headers.get("Allow", ""))

    def test_post_form_encoded_body_is_400_not_crash(self):
        req = urllib.request.Request(
            self._url("/api/submit"),
            data=b"challenge=1&flag=x",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code, raw = resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read().decode("utf-8")
        self.assertEqual(code, 400)
        self.assertNotIn("Traceback", raw)


class TestSubmitEdgeCases(WebDashboardTestCase):
    """Lock-in hành vi biên: challenge lạ, không leak stack trace / flag."""

    def _hdr(self):
        return {"X-Requested-With": "XMLHttpRequest"}

    def test_nonexistent_challenge_ok_false_no_crash(self):
        # SubmitService trả (False, msg) cho challenge không tồn tại
        # → dashboard 200 {ok:false}, KHÔNG crash, KHÔNG stack trace.
        fake = FakeSubmitter(
            result=(False, "Không tìm thấy challenge khớp: '9999'"))
        self.dash._submit_factory = lambda: fake
        code, payload, _ = self.dash.handle_submit_request(
            json.dumps({"challenge": 9999, "flag": "FLAG{x}"}).encode(),
            self._hdr())
        self.assertEqual(code, 200)
        self.assertFalse(payload["ok"])
        self.assertNotIn("Traceback", payload["message"])
        self.assertEqual(fake.calls, [(9999, "FLAG{x}")])

    def test_service_exception_no_stack_trace_leak(self):
        exc = RuntimeError("boom-detail")
        self.dash._submit_factory = lambda: FakeSubmitter(exc=exc)
        code, payload, _ = self.dash.handle_submit_request(
            json.dumps({"challenge": 1, "flag": "FLAG{x}"}).encode(),
            self._hdr())
        self.assertEqual(code, 500)
        self.assertFalse(payload["ok"])
        # Chỉ message ngắn gọn — không bao giờ leak full traceback.
        self.assertNotIn("Traceback", payload["message"])
        self.assertIn("boom-detail", payload["message"])

    def test_flag_never_echoed_in_error_payloads(self):
        flag = "FLAG{n0t-l34k}"
        body = json.dumps({"challenge": 1, "flag": flag}).encode()
        cases = [
            self.dash.handle_submit_request(body, {}),                # 403 CSRF
            self.dash.handle_submit_request(b"{not-json", self._hdr()),  # 400 JSON dở
            self.dash.handle_submit_request(                          # 400 thiếu trường
                json.dumps({"challenge": ""}).encode(), self._hdr()),
        ]
        for (code, payload, _retry), expect in zip(cases, (403, 400, 400)):
            with self.subTest(code=code):
                self.assertEqual(code, expect)
                self.assertNotIn(flag, json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
