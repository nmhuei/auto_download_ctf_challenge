"""Deep integration/fault tests for automatic account registration paths."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ctf_downloader.platforms.base import PlatformRegisterUnsupported
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform, solve_hash_pow
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.services.register_service import RegisterService
from ctf_downloader.utils.gzctf_crypto import encrypt_api_data
from ctf_downloader.utils.tempmail import TempMailClient


def _json(handler, status, payload, headers=None):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


class _GZModernHandler(BaseHTTPRequestHandler):
    server_version = "GZModernTest/1"
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_key_b64 = base64.b64encode(public_key).decode()
    pow_counter = 0
    pow = {}
    register_password = None
    register_payload = None
    submitted_flag = None

    def log_message(self, *_args):
        return

    @classmethod
    def reset(cls):
        cls.pow_counter = 0
        cls.pow = {}
        cls.register_password = None
        cls.register_payload = None
        cls.submitted_flag = None

    @classmethod
    def decrypt_api(cls, value):
        raw = base64.b64decode(value)
        eph = X25519PublicKey.from_public_bytes(raw[:32])
        nonce = raw[32:44]
        ciphertext = raw[44:]
        shared = cls.private_key.exchange(eph)
        key = hashlib.sha256(shared).digest()
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode()

    @staticmethod
    def verify_pow(challenge_hex, answer_hex, difficulty):
        digest = hashlib.sha256(bytes.fromhex(challenge_hex) + bytes.fromhex(answer_hex)).digest()
        bits = 0
        for byte in digest:
            if byte == 0:
                bits += 8
                continue
            bits += 8 - byte.bit_length()
            break
        return bits >= difficulty

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.lower()
        if path == "/api/config":
            return _json(self, 200, {
                "title": "Modern GZ",
                "slogan": "",
                "apiPublicKey": type(self).public_key_b64,
                "portMapping": "Direct",
                "defaultLifetime": 120,
            })
        if path == "/api/captcha":
            return _json(self, 200, {"type": "HashPow", "siteKey": ""})
        if path == "/api/captcha/powchallenge":
            type(self).pow_counter += 1
            idx = type(self).pow_counter
            cid = f"{idx:012x}"
            # Deliberately distinct from ID: this catches the historical bug.
            challenge = (bytes([0xA0 + idx]) * 8).hex()
            type(self).pow[cid] = (challenge, 8)
            return _json(self, 200, {
                "id": cid, "challenge": challenge, "difficulty": 8,
            })
        if path == "/api/game/1/challenges/7/status/sub1":
            body = b'"Accepted"'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return _json(self, 404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        path = urlparse(self.path).path.lower()
        if path == "/api/account/register":
            ticket = body.get("challenge", "")
            try:
                cid, answer = ticket.split(":", 1)
                challenge, difficulty = type(self).pow.pop(cid)
            except Exception:
                return _json(self, 400, {"title": "bad pow", "status": 400})
            if len(answer) != 16 or not self.verify_pow(challenge, answer, difficulty):
                return _json(self, 400, {"title": "bad pow", "status": 400})
            try:
                password = type(self).decrypt_api(body.get("password", ""))
            except Exception:
                return _json(self, 400, {"title": "bad encryption", "status": 400})
            type(self).register_password = password
            type(self).register_payload = body
            return _json(
                self, 200,
                {"title": "registered", "data": "LoggedIn", "status": 200},
                {"Set-Cookie": "GZCTF_Token=jwt-modern; Path=/; HttpOnly"},
            )
        if path == "/api/game/1/challenges/7":
            try:
                type(self).submitted_flag = type(self).decrypt_api(body.get("flag", ""))
            except Exception:
                return _json(self, 400, {"title": "bad encrypted flag"})
            raw = b'"sub1"'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return _json(self, 404, {"error": "not found"})


class _RCTFHandler(BaseHTTPRequestHandler):
    mode = "immediate"
    register_calls = 0

    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/v2/integrations/client/config":
            protected = type(self).mode == "captcha"
            return _json(self, 200, {
                "kind": "goodClientConfigV2",
                "data": {
                    "registrationsEnabled": True,
                    "captcha": {
                        "provider": "hcaptcha",
                        "protectedEndpoints": {"register": protected},
                    } if protected else None,
                },
            })
        return _json(self, 404, {"kind": "badEndpoint"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if path == "/api/v2/auth/register":
            type(self).register_calls += 1
            if type(self).mode == "rate":
                return _json(self, 429, {
                    "kind": "badRateLimit",
                    "message": "slow",
                    "data": {"timeLeft": 12345},
                })
            self.assert_fields = body
            return _json(self, 200, {
                "kind": "goodRegisterV2",
                "message": "ok",
                "data": {"authToken": "auth-v2", "teamToken": "team-v2"},
            })
        return _json(self, 404, {"kind": "badEndpoint"})


class _CTFdHandler(BaseHTTPRequestHandler):
    mode = "normal"
    posted = None

    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/register":
            extra = ""
            if type(self).mode == "captcha":
                extra = '<div class="cf-turnstile"></div>'
            elif type(self).mode == "required":
                extra = '<input name="tos" type="checkbox" required>'
            html = (
                '<form method="post"><input type="hidden" name="nonce" value="hidden-N">'
                '<input name="name" required><input name="email" required>'
                '<input name="password" type="password" required>' + extra + '</form>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path == "/api/v1/users/me" and type(self).posted:
            return _json(self, 200, {
                "success": True, "data": {"type": "user", "name": type(self).posted["name"]},
            })
        return _json(self, 401, {"success": False})

    def do_POST(self):  # noqa: N802
        from urllib.parse import parse_qs
        path = urlparse(self.path).path
        if path == "/register":
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(n).decode())
            type(self).posted = {k: v[0] for k, v in form.items()}
            self.send_response(302)
            self.send_header("Location", "/challenges")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return _json(self, 404, {"error": "not found"})


class _ServerCase(unittest.TestCase):
    handler = None

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)


class TestGZModernIntegration(_ServerCase):
    handler = _GZModernHandler

    def setUp(self):
        _GZModernHandler.reset()
        super().setUp()

    def test_current_gz_register_encrypts_password_and_hashes_challenge_not_id(self):
        session = requests.Session()
        self.addCleanup(session.close)
        platform = GZCTFPlatform(self.base + "/games/1/challenges", session)
        result = platform.register(
            username="modernuser", email="u@example.test", password="Secret!123"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(_GZModernHandler.register_password, "Secret!123")
        self.assertEqual(result.get("register_status"), "LoggedIn")
        self.assertEqual(result.get("token"), "jwt-modern")
        self.assertEqual(_GZModernHandler.pow_counter, 1)
        payload = _GZModernHandler.register_payload
        self.assertNotEqual(payload["password"], "Secret!123")
        cid, answer = payload["challenge"].split(":", 1)
        # The test server already rejected the request unless challenge bytes validated.
        self.assertEqual(len(cid), 12)
        self.assertEqual(len(answer), 16)

    def test_current_gz_flag_submit_encrypts_flag_with_api_public_key(self):
        session = requests.Session()
        self.addCleanup(session.close)
        platform = GZCTFPlatform(self.base + "/games/1/challenges", session)
        platform.game_id = 1
        ok, message = platform.submit_flag(7, "GZCTF{encrypted_submit}")
        self.assertTrue(ok, message)
        self.assertEqual(
            _GZModernHandler.submitted_flag,
            "GZCTF{encrypted_submit}",
        )
        self.assertEqual(platform.last_verdict, "correct")

    def test_crypto_wire_roundtrip_matches_upstream_frame(self):
        server_priv = X25519PrivateKey.generate()
        server_pub = base64.b64encode(server_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        eph = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        nonce = bytes(range(12))
        encoded = encrypt_api_data("hello", server_pub, ephemeral_private_key=eph, nonce=nonce)
        raw = base64.b64decode(encoded)
        self.assertEqual(len(raw[:32]), 32)
        self.assertEqual(raw[32:44], nonce)
        shared = server_priv.exchange(X25519PublicKey.from_public_bytes(raw[:32]))
        clear = AESGCM(hashlib.sha256(shared).digest()).decrypt(nonce, raw[44:], None)
        self.assertEqual(clear, b"hello")


class TestRCTFModernIntegration(_ServerCase):
    handler = _RCTFHandler

    def setUp(self):
        _RCTFHandler.mode = "immediate"
        _RCTFHandler.register_calls = 0
        super().setUp()

    def test_rctf_v2_immediate_registration_returns_both_tokens(self):
        session = requests.Session()
        self.addCleanup(session.close)
        p = RCTFPlatform(self.base, session)
        result = p.register(username="teamx", email="team@example.test", password="unused")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["token"], "auth-v2")
        self.assertEqual(result["team_token"], "team-v2")
        self.assertEqual(_RCTFHandler.register_calls, 1)

    def test_rctf_captcha_protected_registration_fails_before_post(self):
        _RCTFHandler.mode = "captcha"
        p = RCTFPlatform(self.base, requests.Session())
        self.addCleanup(p.session.close)
        with self.assertRaises(PlatformRegisterUnsupported):
            p.register(username="teamx", email="team@example.test", password="unused")
        self.assertEqual(_RCTFHandler.register_calls, 0)

    def test_rctf_rate_limit_preserves_server_time_left(self):
        _RCTFHandler.mode = "rate"
        p = RCTFPlatform(self.base, requests.Session())
        self.addCleanup(p.session.close)
        result = p.register(username="teamx", email="team@example.test", password="unused")
        self.assertFalse(result["ok"])
        self.assertIn("12345", result["message"])


class TestCTFdModernIntegration(_ServerCase):
    handler = _CTFdHandler

    def setUp(self):
        _CTFdHandler.mode = "normal"
        _CTFdHandler.posted = None
        super().setUp()

    def test_hidden_nonce_current_template_registers(self):
        session = requests.Session()
        self.addCleanup(session.close)
        p = CTFdPlatform(self.base, session)
        result = p.register(username="u1", email="u@example.test", password="pw")
        self.assertTrue(result["ok"], result)
        self.assertEqual(_CTFdHandler.posted["nonce"], "hidden-N")

    def test_captcha_marker_stops_before_post(self):
        _CTFdHandler.mode = "captcha"
        p = CTFdPlatform(self.base, requests.Session())
        self.addCleanup(p.session.close)
        with self.assertRaises(PlatformRegisterUnsupported):
            p.register(username="u1", email="u@example.test", password="pw")
        self.assertIsNone(_CTFdHandler.posted)

    def test_required_unknown_field_stops_before_post(self):
        _CTFdHandler.mode = "required"
        p = CTFdPlatform(self.base, requests.Session())
        self.addCleanup(p.session.close)
        with self.assertRaises(PlatformRegisterUnsupported):
            p.register(username="u1", email="u@example.test", password="pw")
        self.assertIsNone(_CTFdHandler.posted)


class _FakeMail:
    def __init__(self, messages):
        self.messages = messages
        self.fetches = []

    def list_messages(self, token=None):
        return [{"id": str(i)} for i in range(len(self.messages))]

    def fetch_message_text(self, msg_id):
        self.fetches.append(msg_id)
        return self.messages[int(msg_id)]

    def wait_for_content_match(self, matcher, timeout_s=120.0):
        for msg in self.list_messages():
            content = self.fetch_message_text(msg["id"])
            hit = matcher(content)
            if hit:
                return msg, content, hit
        return None


class TestVerificationHookPaths(unittest.TestCase):
    def _svc(self, mail):
        return RegisterService(
            config_loader=lambda: {},
            config_updater=lambda mut: mut({}),
            tempmail_factory=lambda: mail,
            detect_fn=lambda *_: (_ for _ in ()).throw(AssertionError("unused")),
        )

    def test_unrelated_first_email_is_skipped_for_ctfd(self):
        mail = _FakeMail([
            "welcome, no verify link",
            "click https://ctf.test/confirm/ABC_123",
        ])
        hook = self._svc(mail)._make_verify_hook(mail)
        session = SimpleNamespace(get=lambda url, timeout=None: SimpleNamespace(status_code=200))
        self.assertTrue(hook(session, platform="ctfd", base_url="https://ctf.test"))
        self.assertEqual(mail.fetches, ["0", "1"])

    def test_rctf_verify_link_posts_verify_token(self):
        mail = _FakeMail(["verify https://r.test/verify?token=TOKEN123"])
        hook = self._svc(mail)._make_verify_hook(mail)
        seen = {}
        def post(url, json=None, timeout=None):
            seen["url"], seen["json"] = url, json
            return SimpleNamespace(status_code=200, json=lambda: {
                "kind": "goodRegisterV2",
                "data": {"authToken": "a", "teamToken": "t"},
            })
        out = hook(SimpleNamespace(post=post), platform="rctf", base_url="https://r.test")
        self.assertTrue(out["ok"])
        self.assertEqual(seen["json"], {"verifyToken": "TOKEN123"})

    def test_gz_verify_link_posts_base64_token_and_email(self):
        mail = _FakeMail(["verify https://gz.test/account/verify?token=abc%2B%2F%3D&email=ZW1haWw%3D"])
        hook = self._svc(mail)._make_verify_hook(mail)
        seen = {}
        def post(url, json=None, timeout=None):
            seen["url"], seen["json"] = url, json
            return SimpleNamespace(status_code=200, json=lambda: {})
        out = hook(SimpleNamespace(post=post), platform="gzctf", base_url="https://gz.test")
        self.assertTrue(out["ok"])
        self.assertEqual(seen["json"]["token"], "abc+/=")
        self.assertEqual(seen["json"]["email"], "ZW1haWw=")


class TestRegisterReservationConcurrency(unittest.TestCase):
    def test_two_concurrent_runs_make_exactly_one_platform_register_call(self):
        state = {}
        lock = threading.Lock()
        calls = {"n": 0}
        start = threading.Barrier(2)

        class Platform:
            def register(self, **kwargs):
                with lock:
                    calls["n"] += 1
                time.sleep(0.1)
                return {"ok": True, "message": "ok"}

        info = SimpleNamespace(platform_type="ctfd", confidence="high")

        def loader():
            with lock:
                return json.loads(json.dumps(state))

        def updater(mutator):
            from ctf_downloader.storage.fileio import SKIP_WRITE
            with lock:
                fresh = json.loads(json.dumps(state))
                result = mutator(fresh)
                if result is SKIP_WRITE:
                    return None
                state.clear()
                state.update(result)
                return json.loads(json.dumps(result))

        def make_service():
            return RegisterService(
                now_fn=lambda: 1_000_000.0,
                config_loader=loader,
                config_updater=updater,
                tempmail_factory=lambda: None,
                detect_fn=lambda *_: (Platform(), info),
            )

        results = []
        def worker():
            svc = make_service()
            start.wait()
            try:
                results.append(("ok", svc.run("https://ctf.test", email="a@b.c")))
            except Exception as exc:
                results.append(("err", str(exc)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, _ in results if kind == "err"), 1)
        errors = [val for kind, val in results if kind == "err"]
        self.assertTrue(
            any("Rate limit" in val or "TRƯỚC network POST" in val for val in errors),
            errors,
        )


class TestTempMailRateLimit(unittest.TestCase):
    def test_explicit_429_get_respects_retry_after_once(self):
        sleeps = []

        class Resp:
            def __init__(self, status, headers=None):
                self.status_code = status
                self.headers = headers or {}
                self.text = "limited" if status == 429 else "ok"

        class Session:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, timeout=None, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Resp(429, {"Retry-After": "2"})
                return Resp(200)

        sess = Session()
        client = TempMailClient(
            session=sess,
            base_url="https://mail.test",
            sleep_fn=sleeps.append,
        )
        resp = client._request("GET", "/domains")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sess.calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_explicit_429_post_is_not_replayed(self):
        sleeps = []

        class Resp:
            status_code = 429
            headers = {"Retry-After": "2"}
            text = "limited"

        class Session:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, timeout=None, **kwargs):
                self.calls += 1
                return Resp()

        sess = Session()
        client = TempMailClient(
            session=sess,
            base_url="https://mail.test",
            sleep_fn=sleeps.append,
        )
        from ctf_downloader.utils.tempmail import TempMailError
        with self.assertRaisesRegex(TempMailError, "Mutation không được tự replay"):
            client._request("POST", "/accounts", json={"x": 1})
        self.assertEqual(sess.calls, 1)
        self.assertEqual(sleeps, [])

    def test_network_error_on_post_is_not_retried_blindly(self):
        class Session:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, timeout=None, **kwargs):
                self.calls += 1
                raise TimeoutError("ambiguous timeout")

        sess = Session()
        client = TempMailClient(session=sess, base_url="https://mail.test")
        from ctf_downloader.utils.tempmail import TempMailError
        with self.assertRaises(TempMailError):
            client._request("POST", "/accounts", json={"x": 1})
        self.assertEqual(sess.calls, 1)


if __name__ == "__main__":
    unittest.main()
