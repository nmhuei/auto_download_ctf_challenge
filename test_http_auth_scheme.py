"""Authorization/cookie-origin regression contracts for shared HTTP sessions."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from ctf_downloader.services.session_factory import create_session


def test_ctfd_prefixed_token_uses_token_scheme():
    s = create_session(token="ctfd_abc123")
    assert s.headers["Authorization"] == "Token ctfd_abc123"


def test_generic_token_value_containing_word_token_is_still_bearer():
    s = create_session(token="mytoken123")
    assert s.headers["Authorization"] == "Bearer mytoken123"


def test_explicit_bearer_scheme_is_not_double_prefixed():
    s = create_session(token="Bearer abc.def.ghi")
    assert s.headers["Authorization"] == "Bearer abc.def.ghi"


def test_explicit_token_scheme_is_not_double_prefixed():
    s = create_session(token="Token legacy-ctfd-value")
    assert s.headers["Authorization"] == "Token legacy-ctfd-value"


def test_base_url_scopes_platform_and_cf_cookies_to_platform_host():
    s = create_session(
        cookie="session=SECRET; cf_clearance=CLEAR",
        base_url="https://ctf.example.test/challenges",
    )
    same = s.prepare_request(requests.Request(
        "GET", "https://ctf.example.test/api/v1/challenges"
    ))
    other = s.prepare_request(requests.Request(
        "GET", "https://cdn.other.test/file.zip"
    ))
    assert "session=SECRET" in (same.headers.get("Cookie") or "")
    assert "cf_clearance=CLEAR" in (same.headers.get("Cookie") or "")
    assert other.headers.get("Cookie") is None


def test_base_url_scopes_cookie_to_localhost_without_global_leak():
    s = create_session(
        cookie="session=LOCAL",
        base_url="http://localhost:8000/challenges",
    )
    same = s.prepare_request(requests.Request(
        "GET", "http://localhost:8000/api"
    ))
    other = s.prepare_request(requests.Request(
        "GET", "http://evil.test/file"
    ))
    assert same.headers.get("Cookie") == "session=LOCAL"
    assert other.headers.get("Cookie") is None


def test_cross_origin_scope_removes_inherited_auth_and_api_key():
    s = create_session(
        token="secret-bearer",
        custom_headers={"X-API-Key": "secret-api"},
        base_url="https://ctf.example.test",
    )
    scoped = s._scope_request_kwargs("https://cdn.other.test/file", {})
    assert scoped["headers"]["Authorization"] is None
    assert scoped["headers"]["X-API-Key"] is None


def test_cross_origin_explicit_per_request_auth_is_preserved():
    s = create_session(
        token="platform-token",
        base_url="https://ctf.example.test",
    )
    scoped = s._scope_request_kwargs(
        "https://api.other.test/file",
        {"headers": {"Authorization": "Bearer CDN-SPECIFIC"}},
    )
    assert scoped["headers"]["Authorization"] == "Bearer CDN-SPECIFIC"


@pytest.mark.parametrize("force_browser", [False, True])
def test_same_host_different_port_does_not_receive_platform_credentials(force_browser):
    seen = {"cookie": None, "auth": None, "api": None}

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen["cookie"] = self.headers.get("Cookie")
            seen["auth"] = self.headers.get("Authorization")
            seen["api"] = self.headers.get("X-API-Key")
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    owner = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    owner_thread = threading.Thread(target=owner.serve_forever, daemon=True)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    owner_thread.start()
    target_thread.start()
    try:
        owner_url = f"http://127.0.0.1:{owner.server_address[1]}"
        target_url = f"http://127.0.0.1:{target.server_address[1]}"
        s = create_session(
            token="platform-token",
            cookie="session=SECRET; cf_clearance=CLEAR",
            custom_headers={"X-API-Key": "secret-api"},
            base_url=owner_url,
            use_browser_impersonation=force_browser,
        )
        response = s.get(target_url + "/attachment", timeout=5)
        assert response.status_code == 200
        assert seen["cookie"] in (None, "")
        assert seen["auth"] is None
        assert seen["api"] is None
    finally:
        owner.shutdown()
        owner.server_close()
        owner_thread.join(timeout=2)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


def test_force_browser_mode_keeps_adaptive_origin_security_policy():
    s = create_session(
        token="platform-token",
        cookie="session=SECRET",
        base_url="https://ctf.example.test",
        use_browser_impersonation=True,
    )
    assert s.cloudflare_active is True
    assert s.credential_origin == "https://ctf.example.test"
    scoped = s._scope_request_kwargs("https://cdn.other.test/file", {})
    assert scoped["headers"]["Authorization"] is None
    same = s.prepare_request(requests.Request(
        "GET", "https://ctf.example.test/api"
    ))
    other = s.prepare_request(requests.Request(
        "GET", "https://cdn.other.test/file"
    ))
    assert "session=SECRET" in (same.headers.get("Cookie") or "")
    assert other.headers.get("Cookie") is None
