"""Tests auto-register (spec auto-register): GZCTF/CTFd flows, captcha-dừng,
tempmail parse, credentials deterministic, rate limit 60s.

Mọi HTTP đều mock (FakeSession) — KHÔNG gọi mạng thật.
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.platforms.base import PlatformRegisterUnsupported
from ctf_downloader.platforms.ctfd import ctfd_register
from ctf_downloader.platforms.gzctf import (GZCTFPlatform, gzctf_register,
                                            solve_hash_pow)


# --------------------------------------------------------------------------- #
# Fake HTTP layer
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", url=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (
            __import__("json").dumps(json_data) if json_data is not None else "")
        self.url = url

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeCookie:
    def __init__(self, name, value):
        self.name, self.value = name, value


class FakeCookies:
    """Đủ API requests.Cookies cho tool: set/get + iterable Cookie objects."""

    def __init__(self):
        self._d = {}

    def set(self, name, value):
        self._d[name] = value

    def get(self, name, default=None):
        return self._d.get(name, default)

    def __iter__(self):
        for k, v in self._d.items():
            yield FakeCookie(k, v)

    def __bool__(self):
        return bool(self._d)


class FakeSession:
    """Session giả định: map (method, url-contains) -> handler hoặc response."""

    def __init__(self, routes=None):
        # routes: list of (method, substring, responder(session, url, **kw))
        self.routes = routes or []
        self.calls = []
        self.headers = {}
        self.cookies = FakeCookies()

    def _find(self, method, url):
        # Trùng substring DÀI NHẤT thắng (tránh /api/captcha nuốt
        # /api/captcha/PowChallenge).
        best, best_len = None, -1
        for m, sub, resp in self.routes:
            if m == method and sub in url and len(sub) > best_len:
                best, best_len = resp, len(sub)
        return best

    def _handle(self, method, url, **kw):
        self.calls.append((method, url))
        handler = self._find(method, url)
        if callable(handler):
            try:
                return handler(self, url, **kw)
            except TypeError:
                # lambda 2 tham số không nhận kwargs (timeout/json/data)
                return handler(self, url)
        return handler or FakeResponse(404, text="not found")

    def get(self, url, timeout=None, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, timeout=None, **kw):
        return self._handle("POST", url, **kw)

    def request(self, method, url, **kw):
        return self._handle(method.upper(), url, **kw)


def make_gz_platform(session):
    return GZCTFPlatform("https://gz.example.com/games/6/challenges", session)


class TestCredentialGenerator(unittest.TestCase):
    def test_deterministic_with_seed(self):
        from ctf_downloader.services.register_service import generate_credentials
        a = generate_credentials("player", rng=random.Random(42))
        b = generate_credentials("player", rng=random.Random(42))
        self.assertEqual(a, b)

    def test_shape_and_strength(self):
        from ctf_downloader.services.register_service import generate_credentials
        creds = generate_credentials("ptit", rng=random.Random(7))
        self.assertEqual(len(creds["username"]), len("ptit") + 6)
        suffix = creds["username"][len("ptit"):]
        self.assertTrue(suffix.isalnum() and suffix.islower())
        pw = creds["password"]
        self.assertEqual(len(pw), 16)
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))
        self.assertTrue(any(not c.isalnum() for c in pw))


class TestGZCTFRegister(unittest.TestCase):
    def _routes_ok(self):
        return [
            ("GET", "/api/config", lambda s, u: FakeResponse(
                200, json_data={"Title": "T", "Slogan": "", "PortMapping": "",
                                "DefaultLifetime": 0})),
            # GZCTF hiện đại: KHÔNG bật captcha vẫn trả 200 {"type":"None"}
            ("GET", "/api/captcha", lambda s, u: FakeResponse(
                200, json_data={"type": "None", "siteKey": ""})),
            ("POST", "/api/account/register",
             lambda s, u: FakeResponse(200, text='""')),
            ("POST", "/api/account/login",
             lambda s, u: (s.cookies.set("GZCTF_Token", "jwt123"),
                           FakeResponse(200, text='"jwt123"'))[1]),
        ]

    def test_register_ok_and_login(self):
        sess = FakeSession(self._routes_ok())
        platform = make_gz_platform(sess)
        result = platform.register(username="playerabc123", email="a@b.c",
                                   password="S3cure!pass")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result.get("token"), "jwt123")
        posted = [c for c in sess.calls if c[0] == "POST"]
        self.assertIn(("POST", "https://gz.example.com/api/account/register"), posted)
        self.assertIn(("POST", "https://gz.example.com/api/account/login"), posted)

    def test_captcha_turnstile_stops_clean(self):
        routes = [("GET", "/api/config", lambda s, u: FakeResponse(
            200, json_data={"Title": "T", "CaptchaProvider": "Turnstile"}))]
        sess = FakeSession(routes)
        platform = make_gz_platform(sess)
        with self.assertRaises(PlatformRegisterUnsupported) as ctx:
            platform.register(username="u", email="a@b.c", password="p")
        self.assertIn("captcha", str(ctx.exception))

    def test_sitekey_config_stops_clean(self):
        routes = [("GET", "/api/config", lambda s, u: FakeResponse(
            200, json_data={"Title": "T", "TurnstileSiteKey": "0xABC"}))]
        sess = FakeSession(routes)
        platform = make_gz_platform(sess)
        with self.assertRaises(PlatformRegisterUnsupported):
            platform.register(username="u", email="a@b.c", password="p")

    def test_hashpow_solved_and_ticket_sent(self):
        import hashlib
        # id 12-hex (6 bytes) như PowChallenge upstream
        challenge_id = "deadbeefcafe"
        nonce = solve_hash_pow(challenge_id, 8)
        self.assertIsNotNone(nonce)
        self.assertEqual(len(nonce), 16)  # AnswerLength*2 = 16 hex (8 bytes)
        digest = hashlib.sha256(
            bytes.fromhex(challenge_id) + bytes.fromhex(nonce)).digest()
        # 8 bit 0 ở đầu <=> byte đầu tiên == 0
        self.assertEqual(digest[0], 0)

        captured = {}

        def capture_post(s, url, **kw):
            captured.update(kw.get("json") or {})
            return FakeResponse(200, text="ok")

        routes = [
            ("GET", "/api/config", lambda s, u: FakeResponse(
                200, json_data={"Title": "T"})),
            ("GET", "/api/captcha", lambda s, u: FakeResponse(
                200, json_data={"type": "HashPow"})),
            ("GET", "/api/captcha/PowChallenge", lambda s, u: FakeResponse(
                200, json_data={"id": challenge_id, "difficulty": 8})),
            ("POST", "/api/account/register", capture_post),
        ]
        result = gzctf_register(make_gz_platform(FakeSession(routes)),
                                username="u", email="a@b.c", password="p")
        self.assertTrue(result["ok"], result.get("message"))
        # Ticket đúng wire-format: field "challenge" = "<id>:<answer>"
        ticket = captured.get("challenge")
        self.assertIsInstance(ticket, str)
        challenge_part, answer_part = ticket.split(":")
        self.assertEqual(challenge_part, challenge_id)
        self.assertEqual(answer_part, nonce)

    def test_captcha_type_none_with_sitekey_stops(self):
        # type None NHƯNG siteKey có giá trị -> vẫn là captcha -> dừng sạch
        routes = [("GET", "/api/config", lambda s, u: FakeResponse(
            200, json_data={})),
            ("GET", "/api/captcha", lambda s, u: FakeResponse(
                200, json_data={"type": "None",
                                "siteKey": "0xTURNSTILEKEY"}))]
        with self.assertRaises(PlatformRegisterUnsupported):
            gzctf_register(make_gz_platform(FakeSession(routes)),
                           username="u", email="a@b.c", password="p")

    def test_http_error_reported_not_raised(self):
        routes = [
            ("GET", "/api/config", lambda s, u: FakeResponse(200, json_data={})),
            ("GET", "/api/captcha", lambda s, u: FakeResponse(
                200, json_data={"type": "None", "siteKey": ""})),
            ("POST", "/api/account/register",
             lambda s, u: FakeResponse(400, text='"Ten dang nhap da ton tai"')),
        ]
        result = gzctf_register(make_gz_platform(FakeSession(routes)),
                                username="taken", email="a@b.c", password="p")
        self.assertFalse(result["ok"])
        self.assertIn("400", result["message"])


_CTFD_REGISTER_HTML = """
<html><head><title>Test CTF</title></head><body>
<script>window.init = {"csrfNonce": "nonceXYZ789"};</script>
</body></html>
"""


class TestCTFdRegister(unittest.TestCase):
    def _routes(self, require_me=True):
        return [
            ("GET", "/register", lambda s, u: FakeResponse(
                200, text=_CTFD_REGISTER_HTML)),
            ("POST", "/register", lambda s, u: FakeResponse(
                302, text="", url="https://ctf.example.com/profile")),
            ("GET", "/api/v1/users/me", lambda s, u: FakeResponse(
                200, json_data={"success": True,
                                "data": {"type": "user", "name": "playerabc"}})),
        ]

    def test_nonce_flow_registers(self):
        sess = FakeSession(self._routes())
        from ctf_downloader.platforms.ctfd import CTFdPlatform
        platform = CTFdPlatform("https://ctf.example.com", sess)
        result = platform.register(username="playerabc", email="a@b.c",
                                   password="pw123456")
        self.assertTrue(result["ok"], result.get("message"))
        self.assertEqual(result.get("user_name"), "playerabc")
        posted = [c for c in sess.calls if c[0] == "POST"]
        self.assertIn(("POST", "https://ctf.example.com/register"), posted)

    def test_nonce_missing_fails_clean(self):
        routes = [("GET", "/register", lambda s, u: FakeResponse(
            200, text="<html>no nonce here</html>"))]
        from ctf_downloader.platforms.ctfd import CTFdPlatform
        platform = CTFdPlatform("https://ctf.example.com", FakeSession(routes))
        result = platform.register(username="u", email="a@b.c", password="p")
        self.assertFalse(result["ok"])
        self.assertIn("csrfNonce", result["message"])

    def test_verify_email_hook_called_after_register(self):
        confirm_hits = []

        def hit_confirm(s, url):
            confirm_hits.append(url)
            return FakeResponse(200, text="confirmed")

        routes = self._routes()
        routes.append(("GET", "/confirm/", hit_confirm))
        from ctf_downloader.platforms.ctfd import CTFdPlatform
        platform = CTFdPlatform("https://ctf.example.com", FakeSession(routes))

        class StubMail:
            def wait_for_message(self, **kw):
                return {"id": "m1"}

            def fetch_message_text(self, mid):
                self.mid = mid
                return ("Please confirm: https://ctf.example.com/confirm/abc123 "
                        "thanks")

        mail = StubMail()
        hook_calls = []

        def hook(session):
            from ctf_downloader.utils.tempmail import TempMailClient
            content = mail.fetch_message_text("m1")
            link = TempMailClient.find_confirm_link(content)
            hook_calls.append(link)
            session.get(link, timeout=5)
            return True

        result = platform.register(username="u", email="tmp@x.y",
                                   password="p", verify_email_hook=hook)
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("email_verified"))
        self.assertEqual(hook_calls,
                         ["https://ctf.example.com/confirm/abc123"])
        self.assertEqual(confirm_hits,
                         ["https://ctf.example.com/confirm/abc123"])

    def test_base_default_unsupported(self):
        from ctf_downloader.platforms.ctfd import CTFdPlatform
        # rCTF/generic kế thừa default raise — kiểm tra qua một stub subclass
        class Stub(CTFdPlatform.__mro__[1]):
            def __init__(self):
                from types import SimpleNamespace
                self.ctf_info = SimpleNamespace(platform_type="stub")

            def authenticate(self): pass
            def fetch_challenges(self): pass
            def get_full_file_url(self, p): pass
            def submit_flag(self, cid, flag): pass
        with self.assertRaises(PlatformRegisterUnsupported):
            Stub().register(username="u", email="a@b.c", password="p")


class TestTempMailParsing(unittest.TestCase):
    def _client_with_routes(self, routes):
        from ctf_downloader.utils.tempmail import TempMailClient
        client = TempMailClient(session=FakeSession(routes),
                                base_url="https://api.mail.tm.test",
                                rng=random.Random(1))
        return client

    def test_domains_and_create_mailbox(self):
        routes = [
            ("GET", "/domains", lambda s, u: FakeResponse(
                200, json_data={"hydra:member": [{"domain": "x1.test",
                                                  "isActive": True}]})),
            ("POST", "/accounts", lambda s, u: FakeResponse(
                201, json_data={"id": "acc1"})),
            ("POST", "/token", lambda s, u: FakeResponse(
                200, json_data={"token": "JWT-TOKEN"})),
        ]
        client = self._client_with_routes(routes)
        address, password, token = client.create_mailbox(local_hint="ctf")
        self.assertTrue(address.endswith("@x1.test"))
        self.assertTrue(address.startswith("ctf"))
        self.assertEqual(token, "JWT-TOKEN")

    def test_list_messages_parse_hydra(self):
        routes = [
            ("GET", "/messages", lambda s, u: FakeResponse(
                200, json_data={"hydra:member": [
                    {"id": "m1", "subject": "Confirm your account",
                     "from": {"address": "no-reply@ctf.example.com"}},
                    {"id": "m2", "subject": "Welcome"}]})),
        ]
        msgs = self._client_with_routes(routes).list_messages(token="T")
        self.assertEqual([m["id"] for m in msgs], ["m1", "m2"])

    def test_confirm_link_regex(self):
        from ctf_downloader.utils.tempmail import TempMailClient as T
        text = ("<a href=\"https://ctf.example.com/confirm/AbC123\">verify</a>")
        self.assertEqual(T.find_confirm_link(text),
                         "https://ctf.example.com/confirm/AbC123")
        self.assertIsNone(T.find_confirm_link("no links here"))
        self.assertIsNone(T.find_confirm_link("https://x.test/other/path"))

    def test_wait_for_message_timeout_no_raise(self):
        routes = [("GET", "/messages", lambda s, u: FakeResponse(
            200, json_data={"hydra:member": []}))]
        from ctf_downloader.utils.tempmail import TempMailClient
        sleeps = []
        client = TempMailClient(session=FakeSession(routes),
                                base_url="https://api.mail.tm.test")
        got = client.wait_for_message(timeout_s=0.2, interval=0.05,
                                      sleep_fn=sleeps.append, token="T")
        self.assertIsNone(got)
        self.assertTrue(sleeps)


class TestRateLimit60s(unittest.TestCase):
    def setUp(self):
        from ctf_downloader.services.register_service import RegisterService

    def _make_service(self, now_box, store):
        from ctf_downloader.services.register_service import RegisterService

        class FakeInfo:
            platform_type = "gzctf"
            confidence = "high"

        class FakePlatform:
            def __init__(self):
                self.calls = 0

            def register(self, *, username, email, password,
                         verify_email_hook=None):
                self.calls += 1
                return {"ok": True, "message": "Registered"}

        holder = {"platform": FakePlatform()}

        def detect(url, session):
            return holder["platform"], FakeInfo()

        svc = RegisterService(now_fn=lambda: now_box[0],
                              sleep_fn=lambda *_: None,
                              config_loader=lambda: dict(store),
                              config_saver=lambda cfg: store.clear() or store.update(cfg),
                              tempmail_factory=lambda: None,
                              detect_fn=detect)
        return svc, holder["platform"]

    def test_second_call_within_60s_blocked(self):
        now = [1_000_000.0]
        store = {}
        svc, platform = self._make_service(now, store)
        r1 = svc.run(url="https://gz.example.com", email="a@b.c",
                     workspace="/tmp/nonexistent_ws_xyz")
        self.assertTrue(r1["ok"])
        self.assertEqual(platform.calls, 1)
        # Chưa tới 60s sau lần 1 -> chặn
        now[0] += 10
        with self.assertRaises(RuntimeError) as ctx:
            svc.run(url="https://gz.example.com/", email="a@b.c")
        self.assertIn("Rate limit", str(ctx.exception))
        self.assertEqual(platform.calls, 1)  # không tạo thêm tài khoản nào

    def test_after_60s_allowed_again(self):
        now = [1_000_000.0]
        store = {}
        svc, platform = self._make_service(now, store)
        svc.run(url="https://gz.example.com", email="a@b.c")
        now[0] += 61
        r2 = svc.run(url="https://gz.example.com", email="a@b.c")
        self.assertTrue(r2["ok"])
        self.assertEqual(platform.calls, 2)

    def test_auth_map_saved_under_workspace(self):
        import tempfile
        ws = tempfile.mkdtemp(prefix="ws_reg_test_")
        now = [1_000_000.0]
        store = {}
        svc, _ = self._make_service(now, store)
        result = svc.run(url="https://gz.example.com", email="a@b.c",
                         workspace=ws)
        key = os.path.abspath(ws)
        saved = store.get("auth", {}).get(key) or {}
        self.assertEqual(saved.get("username"),
                         result["credentials"]["username"])
        self.assertIn("password", saved)
        self.assertIn("email", saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
