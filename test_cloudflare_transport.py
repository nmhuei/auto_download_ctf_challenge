"""Cloudflare adaptive HTTP transport integration/regression tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from ctf_downloader.downloaders.http_downloader import HttpDownloader
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.detection import detect_platform_info
from ctf_downloader.services.session_factory import thread_local_sessions
from ctf_downloader.utils.http_client import (
    CloudflareAdaptiveSession,
    CloudflareChallengeError,
    HAS_CURL_CFFI,
    create_session,
    is_cloudflare_challenge,
    is_cloudflare_proxy_response,
)


_BINARY_PAYLOAD = (b"CF-STREAM-BINARY-0123456789\x00\xff" * 8192)


class _CFHandler(BaseHTTPRequestHandler):
    get_count = 0
    head_count = 0
    post_count = 0
    last_post_cookie = ""
    last_post_ua = ""
    mode = "challenge_then_browser"

    def log_message(self, *_args):
        pass

    def _challenge(self):
        body = b"<html><title>Just a moment...</title><div>cf-chl-test</div></html>"
        self.send_response(403)
        self.send_header("cf-mitigated", "challenge")
        self.send_header("cf-ray", "test-ray")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, body=b'{"ok":true}', *, cf_ray=True, clearance=False):
        self.send_response(200)
        if cf_ray:
            self.send_header("cf-ray", "test-ray")
            self.send_header("Server", "cloudflare")
        if clearance:
            self.send_header("Set-Cookie", "cf_clearance=clear123; Path=/; HttpOnly")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _is_requests_default(ua: str) -> bool:
        return "Chrome/120.0.0.0" in ua

    def do_GET(self):
        type(self).get_count += 1
        ua = self.headers.get("User-Agent", "")

        if self.mode == "persistent_challenge":
            return self._challenge()

        if self.mode == "ctfd_detection":
            if self._is_requests_default(ua):
                return self._challenge()
            if self.path in ("/", ""):
                return self._ok(
                    body=b"<html><footer>Powered by CTFd</footer></html>",
                    clearance=True,
                )
            body = b"not found"
            self.send_response(404)
            self.send_header("cf-ray", "test-ray")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.mode == "binary_download":
            if self._is_requests_default(ua):
                return self._challenge()
            body = _BINARY_PAYLOAD
            self.send_response(200)
            self.send_header("cf-ray", "test-ray")
            self.send_header("Server", "cloudflare")
            self.send_header("Set-Cookie", "cf_clearance=clear123; Path=/; HttpOnly")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.mode == "proxy_success":
            return self._ok(clearance=False)

        if self.mode == "challenge_then_browser":
            if self._is_requests_default(ua):
                return self._challenge()
            return self._ok(clearance=True)

        return self._ok(cf_ray=False)

    def do_HEAD(self):
        type(self).head_count += 1
        ua = self.headers.get("User-Agent", "")

        def challenge_head():
            self.send_response(403)
            self.send_header("cf-mitigated", "challenge")
            self.send_header("cf-ray", "test-ray")
            self.send_header("Content-Type", "text/html")
            self.end_headers()

        def ok_head(*, cf_ray=True, clearance=False):
            self.send_response(200)
            if cf_ray:
                self.send_header("cf-ray", "test-ray")
                self.send_header("Server", "cloudflare")
            if clearance:
                self.send_header(
                    "Set-Cookie", "cf_clearance=clear123; Path=/; HttpOnly"
                )
            self.end_headers()

        if self.mode == "persistent_challenge":
            return challenge_head()
        if self.mode == "first_post_challenge":
            return ok_head(cf_ray=False)
        if self.mode in (
            "challenge_then_browser", "binary_download", "ctfd_detection"
        ):
            if self._is_requests_default(ua):
                return challenge_head()
            return ok_head(clearance=True)
        if self.mode == "proxy_success":
            return ok_head(clearance=False)
        return ok_head(cf_ray=False)

    def do_POST(self):
        type(self).post_count += 1
        type(self).last_post_cookie = self.headers.get("Cookie", "")
        type(self).last_post_ua = self.headers.get("User-Agent", "")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)

        if self.mode == "first_post_challenge":
            return self._challenge()

        if (
            "cf_clearance=clear123" in type(self).last_post_cookie
            and not self._is_requests_default(type(self).last_post_ua)
        ):
            return self._ok(body=b'{"submitted":true}')
        return self._challenge()


@pytest.fixture
def cf_server():
    _CFHandler.get_count = 0
    _CFHandler.head_count = 0
    _CFHandler.post_count = 0
    _CFHandler.last_post_cookie = ""
    _CFHandler.last_post_ua = ""
    _CFHandler.mode = "challenge_then_browser"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CFHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_challenge_get_replays_once_with_browser_then_post_is_single(cf_server):
    session = create_session(retries=0)
    assert isinstance(session, CloudflareAdaptiveSession)

    first = session.get(cf_server + "/probe", timeout=5)
    assert first.status_code == 200
    assert session.cloudflare_seen is True
    assert session.cloudflare_active is True
    assert _CFHandler.get_count == 2
    assert session.cookies.get("cf_clearance") == "clear123"

    posted = session.post(
        cf_server + "/submit",
        json={"flag": "FLAG{x}"},
        timeout=5,
    )
    assert posted.status_code == 200
    assert posted.json()["submitted"] is True
    assert _CFHandler.post_count == 1
    assert "cf_clearance=clear123" in _CFHandler.last_post_cookie
    assert "Chrome/120.0.0.0" not in _CFHandler.last_post_ua


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_successful_cf_proxy_response_arms_browser_before_first_post(cf_server):
    _CFHandler.mode = "proxy_success"
    session = create_session(retries=0)

    first = session.get(cf_server + "/probe", timeout=5)
    assert first.status_code == 200
    assert _CFHandler.get_count == 1
    assert session.cloudflare_active is True

    # Seed a browser-shared clearance to model a user-supplied/browser-obtained
    # cookie after the harmless probe; POST itself must never be sent by
    # requests and then replayed.
    session._cf_browser_session.cookies.set("cf_clearance", "clear123")
    posted = session.post(cf_server + "/submit", data=b"x", timeout=5)
    assert posted.status_code == 200
    assert _CFHandler.post_count == 1
    assert "Chrome/120.0.0.0" not in _CFHandler.last_post_ua


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_persistent_managed_challenge_has_one_safe_replay_no_loop(cf_server):
    _CFHandler.mode = "persistent_challenge"
    session = create_session(retries=0)

    response = session.get(cf_server + "/probe", timeout=5)
    assert response.status_code == 403
    assert is_cloudflare_challenge(response)
    assert _CFHandler.get_count == 2
    assert session.cloudflare_active is True


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_first_mutation_preflight_activates_browser_before_post(cf_server):
    _CFHandler.mode = "challenge_then_browser"
    session = create_session(retries=0)

    response = session.post(cf_server + "/submit", json={"x": 1}, timeout=5)
    assert response.status_code == 200
    assert response.json()["submitted"] is True
    assert session.cloudflare_active is True
    assert session.cookies.get("cf_clearance") == "clear123"
    assert _CFHandler.head_count == 2  # requests HEAD + browser replay
    assert _CFHandler.get_count == 0
    assert _CFHandler.post_count == 1
    assert "Chrome/120.0.0.0" not in _CFHandler.last_post_ua


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_persistent_challenge_blocks_mutation_before_post(cf_server):
    _CFHandler.mode = "persistent_challenge"
    session = create_session(retries=0)

    with pytest.raises(CloudflareChallengeError, match="mutation chưa được gửi"):
        session.post(cf_server + "/submit", json={"x": 1}, timeout=5)
    assert session.cloudflare_active is True
    assert _CFHandler.head_count == 2
    assert _CFHandler.post_count == 0


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_post_only_challenge_after_clean_preflight_is_never_replayed(cf_server):
    _CFHandler.mode = "first_post_challenge"
    session = create_session(retries=0)

    with pytest.raises(CloudflareChallengeError, match="không được replay"):
        session.post(cf_server + "/submit", json={"x": 1}, timeout=5)
    assert session.cloudflare_active is True
    assert _CFHandler.head_count == 1
    assert _CFHandler.post_count == 1


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_ctfd_submit_surfaces_cloudflare_and_sends_no_post(cf_server):
    _CFHandler.mode = "persistent_challenge"
    session = create_session(retries=0, base_url=cf_server)
    platform = CTFdPlatform(cf_server, session)
    platform.nonce = "nonce"

    ok, message = platform.submit_flag(7, "FLAG{x}")
    assert ok is False
    assert "Cloudflare" in message
    assert _CFHandler.post_count == 0


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
@pytest.mark.parametrize("action", ["start", "stop", "extend"])
def test_ctfd_instance_mutations_short_circuit_cloudflare_fallbacks(
    cf_server, action
):
    _CFHandler.mode = "persistent_challenge"
    session = create_session(retries=0, base_url=cf_server)
    platform = CTFdPlatform(cf_server, session)
    platform.nonce = "nonce"

    if action == "start":
        ok, result = platform.start_instance(7)
        message = result.get("message", "")
    elif action == "stop":
        ok, message = platform.stop_instance(7)
    else:
        ok, message = platform.extend_instance(7)

    assert ok is False
    assert "Cloudflare" in message
    # Persistent challenge is caught by safe HEAD/browser preflight. No whale,
    # legacy, or generic container mutation endpoint is POST/PATCH/DELETE'd.
    assert _CFHandler.post_count == 0
    assert _CFHandler.head_count == 2


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_thread_local_worker_inherits_active_cloudflare_transport(cf_server):
    master = create_session(retries=0)
    first = master.get(cf_server + "/probe", timeout=5)
    assert first.status_code == 200
    assert master.cloudflare_active is True
    assert master.cookies.get("cf_clearance") == "clear123"

    with thread_local_sessions(master) as get_session:
        worker = get_session()
        assert worker is not master
        assert getattr(worker, "cloudflare_active", False) is True
        assert worker.cookies.get("cf_clearance") == "clear123"
        # First worker request must already use browser fingerprint; it should
        # not incur another challenge/replay pair.
        before = _CFHandler.get_count
        response = worker.get(cf_server + "/probe", timeout=5)
        assert response.status_code == 200
        assert _CFHandler.get_count == before + 1


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_http_downloader_streams_binary_through_cloudflare_browser_transport(cf_server, tmp_path):
    _CFHandler.mode = "binary_download"
    session = create_session(retries=0)

    path = HttpDownloader.download_file(
        cf_server + "/challenge.bin",
        str(tmp_path),
        session,
        preferred_filename="challenge.bin",
        timeout=5,
    )
    assert path is not None
    assert (tmp_path / "challenge.bin").read_bytes() == _BINARY_PAYLOAD
    assert session.cloudflare_active is True
    assert session.cookies.get("cf_clearance") == "clear123"
    # requests challenge + exactly one browser replay; the binary body is not
    # fetched a third time by the downloader.
    assert _CFHandler.get_count == 2


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_platform_detector_recovers_ctfd_behind_cloudflare(cf_server):
    _CFHandler.mode = "ctfd_detection"
    session = create_session(retries=0)

    platform, info = detect_platform_info(cf_server, session, quiet=True)
    assert isinstance(platform, CTFdPlatform)
    assert info.platform_type == "ctfd"
    assert info.confidence == "high"
    assert session.cloudflare_active is True
    assert session.cookies.get("cf_clearance") == "clear123"


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_active_browser_transport_preserves_cross_origin_auth_stripping():
    seen = {"src_auth": None, "src_api": None, "dst_auth": None, "dst_api": None}

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen["dst_auth"] = self.headers.get("Authorization")
            seen["dst_api"] = self.headers.get("X-API-Key")
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    dest = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    dest_thread = threading.Thread(target=dest.serve_forever, daemon=True)
    dest_thread.start()

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen["src_auth"] = self.headers.get("Authorization")
            seen["src_api"] = self.headers.get("X-API-Key")
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{dest.server_address[1]}/file",
            )
            self.end_headers()

    src = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    src_thread = threading.Thread(target=src.serve_forever, daemon=True)
    src_thread.start()
    try:
        session = create_session(
            token="secret-bearer",
            custom_headers={"X-API-Key": "secret-api-key"},
            retries=0,
        )
        assert session._activate_browser_transport() is True
        response, final_url = HttpDownloader._request_follow_redirects(
            session,
            "GET",
            f"http://127.0.0.1:{src.server_address[1]}/start",
            timeout=5,
        )
        response.close()
        assert final_url.endswith("/file")
        assert seen["src_auth"] == "Bearer secret-bearer"
        assert seen["src_api"] == "secret-api-key"
        assert seen["dst_auth"] is None
        assert seen["dst_api"] is None
    finally:
        src.shutdown()
        src.server_close()
        src_thread.join(timeout=2)
        dest.shutdown()
        dest.server_close()
        dest_thread.join(timeout=2)


@pytest.mark.skipif(not HAS_CURL_CFFI, reason="curl_cffi runtime dependency missing")
def test_browser_transport_network_error_normalizes_to_requests_exception():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    session = create_session(retries=0)
    assert session._activate_browser_transport() is True
    with pytest.raises(requests.RequestException):
        session.get(f"http://127.0.0.1:{port}/dead", timeout=0.5)


def test_detector_prefers_official_header_and_avoids_plain_cf_403_false_positive():
    class Resp:
        status_code = 403
        text = "ordinary forbidden"

    official = Resp()
    official.headers = {"cf-mitigated": "challenge", "content-type": "text/html"}
    assert is_cloudflare_proxy_response(official)
    assert is_cloudflare_challenge(official)

    plain = Resp()
    plain.headers = {"Server": "cloudflare", "cf-ray": "abc"}
    assert is_cloudflare_proxy_response(plain)
    assert not is_cloudflare_challenge(plain)


def test_cookie_parser_keeps_cf_clearance_with_platform_cookie():
    session = create_session(
        cookie="cf_clearance=clear123; session=platform456",
        cloudflare_fallback=False,
    )
    assert session.cookies.get("cf_clearance") == "clear123"
    assert session.cookies.get("session") == "platform456"
