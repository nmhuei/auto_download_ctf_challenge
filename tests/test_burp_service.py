"""Tests for BurpService MCP integration and automatic cookie extraction."""

import json
from unittest.mock import patch, MagicMock
import pytest

from ctf_downloader.services.burp_service import BurpService, _normalize_domain
from ctf_downloader.services.auth_service import AuthService


def test_normalize_domain():
    assert _normalize_domain("https://asisctf.com/challenges") == "asisctf.com"
    assert _normalize_domain("http://ctf.example.org:8080/api") == "ctf.example.org"
    assert _normalize_domain("ctfd.local") == "ctfd.local"
    assert _normalize_domain("https://sub.domain.ctf.io/") == "sub.domain.ctf.io"


def test_burp_service_availability_when_closed():
    burp = BurpService(host="127.0.0.1", mcp_port=65530, proxy_port=65531)
    assert burp.is_mcp_available(timeout=0.2) is False
    assert burp.is_proxy_available(timeout=0.2) is False
    assert burp.extract_cookies("https://example.com") == {}
    assert burp.get_cookie_header("https://example.com") is None


def test_parse_cookies_from_raw_history():
    burp = BurpService()
    
    sample_entry_1 = {
        "request": (
            "GET /challenges HTTP/1.1\r\n"
            "Host: asisctf.com\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "Cookie: session=xyz123; XSRF-TOKEN=token_abc; ignored_cookie=test\r\n\r\n"
        ),
        "response": (
            "HTTP/2 200 OK\r\n"
            "Set-Cookie: asis_session=live_session_val; Path=/; HttpOnly; Secure; SameSite=Lax\r\n\r\n"
            "<html>Challenges</html>"
        ),
        "notes": ""
    }

    sample_entry_other = {
        "request": (
            "GET /status HTTP/1.1\r\n"
            "Host: other-site.org\r\n"
            "Cookie: other_session=wrong_site\r\n\r\n"
        ),
        "response": "HTTP/1.1 200 OK\r\n\r\n",
        "notes": ""
    }

    raw_text = json.dumps(sample_entry_1) + json.dumps(sample_entry_other)
    cookies = burp._parse_cookies_from_raw_history(raw_text, "asisctf.com")

    assert "session" in cookies
    assert cookies["session"] == "xyz123"
    assert "XSRF-TOKEN" in cookies
    assert cookies["XSRF-TOKEN"] == "token_abc"
    assert "asis_session" in cookies
    assert cookies["asis_session"] == "live_session_val"
    
    # Other site cookies must NOT leak
    assert "other_session" not in cookies


def test_auth_service_auto_burp_integration():
    with patch.object(BurpService, "is_mcp_available", return_value=True), \
         patch.object(BurpService, "get_cookie_header", return_value="session=burp_extracted_cookie; XSRF=abc"):
        
        cookie, token = AuthService.resolve("https://asisctf.com", cookie_arg="burp")
        assert cookie == "session=burp_extracted_cookie; XSRF=abc"

        # Also when no cookie provided, should auto-detect from Burp MCP
        cookie2, token2 = AuthService.resolve("https://non-configured-domain.ctf")
        assert cookie2 == "session=burp_extracted_cookie; XSRF=abc"


def test_burp_cookie_scoping_prevents_parent_leak():
    burp = BurpService()
    parent_entry = {
        "request": "GET / HTTP/1.1\r\nHost: example.com\r\nCookie: parent_token=secret_parent\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\n\r\n"
    }
    sub_entry = {
        "request": "GET / HTTP/1.1\r\nHost: sub.example.com\r\nCookie: sub_token=secret_sub\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\n\r\n"
    }
    raw_text = json.dumps(parent_entry) + json.dumps(sub_entry)

    # When querying sub.example.com, parent_token must NOT leak
    sub_cookies = burp._parse_cookies_from_raw_history(raw_text, "sub.example.com")
    assert "sub_token" in sub_cookies
    assert "parent_token" not in sub_cookies

    # When querying example.com, both exact and subdomain may be captured
    parent_cookies = burp._parse_cookies_from_raw_history(raw_text, "example.com")
    assert "parent_token" in parent_cookies
    assert "sub_token" in parent_cookies


def test_burp_latest_cookie_precedence():
    burp = BurpService()
    first_req = {
        "request": "GET /login HTTP/1.1\r\nHost: asisctf.com\r\nCookie: session=old_value\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\n\r\n"
    }
    second_res = {
        "request": "GET /dashboard HTTP/1.1\r\nHost: asisctf.com\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\nSet-Cookie: session=fresh_value; Path=/\r\n\r\n"
    }
    raw_array = json.dumps([first_req, second_res])
    cookies = burp._parse_cookies_from_raw_history(raw_array, "asisctf.com")
    assert cookies.get("session") == "fresh_value"


def test_burp_exact_host_takes_precedence_over_subdomain_collision():
    burp = BurpService()
    sub_entry = {
        "request": "GET / HTTP/1.1\r\nHost: sub.example.com\r\nCookie: session=sub_session\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\n\r\n"
    }
    exact_entry = {
        "request": "GET / HTTP/1.1\r\nHost: example.com\r\nCookie: session=exact_parent_session\r\n\r\n",
        "response": "HTTP/1.1 200 OK\r\n\r\n"
    }
    # Even if sub_entry comes after exact_entry in history, exact match must win
    raw_text = json.dumps(exact_entry) + json.dumps(sub_entry)
    cookies = burp._parse_cookies_from_raw_history(raw_text, "example.com")
    assert cookies.get("session") == "exact_parent_session"
