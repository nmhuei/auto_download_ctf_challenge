import socket

import requests

from ctf_downloader.utils.http_client import (
    diagnose_request_exception,
    create_session,
)


def _caused(outer, inner):
    outer.__cause__ = inner
    return outer


def test_connect_timeout_is_safe_to_retry_even_for_post():
    diag = diagnose_request_exception(
        requests.ConnectTimeout("connect timed out"), method="POST"
    )
    assert diag.code == "connect-timeout"
    assert diag.retryable is True
    assert "kết nối" in diag.summary.lower()


def test_read_timeout_retries_only_read_only_methods():
    get_diag = diagnose_request_exception(
        requests.ReadTimeout("read timed out"), method="GET"
    )
    post_diag = diagnose_request_exception(
        requests.ReadTimeout("read timed out"), method="POST"
    )
    assert get_diag.code == post_diag.code == "read-timeout"
    assert get_diag.retryable is True
    assert post_diag.retryable is False


def test_dns_failure_is_distinguished_from_generic_connection_error():
    exc = _caused(
        requests.ConnectionError("pool failed"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
    )
    diag = diagnose_request_exception(exc)
    assert diag.code == "dns-error"
    assert "dns" in diag.summary.lower()


def test_connection_refused_is_actionable():
    exc = _caused(
        requests.ConnectionError("connect failed"),
        ConnectionRefusedError(111, "Connection refused"),
    )
    diag = diagnose_request_exception(exc)
    assert diag.code == "connection-refused"
    assert "port" in diag.hint.lower()


def test_connection_reset_is_not_retryable_for_mutation():
    exc = _caused(
        requests.ConnectionError("read failed"),
        ConnectionResetError(104, "Connection reset by peer"),
    )
    assert diagnose_request_exception(exc, method="GET").retryable is True
    post = diagnose_request_exception(exc, method="POST")
    assert post.code == "connection-reset"
    assert post.retryable is False
    assert "mutation" in post.hint


def test_tls_and_proxy_failures_have_distinct_remediation():
    tls = diagnose_request_exception(requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"))
    proxy = diagnose_request_exception(requests.exceptions.ProxyError("proxy refused"))
    assert tls.code == "tls-error"
    assert "certificate" in tls.hint.lower()
    assert proxy.code == "proxy-error"
    assert "NO_PROXY" in proxy.hint


def test_invalid_url_and_redirect_loop_are_not_retried():
    invalid = diagnose_request_exception(requests.exceptions.MissingSchema("no scheme"))
    redirects = diagnose_request_exception(requests.exceptions.TooManyRedirects("loop"))
    assert invalid.code == "invalid-url" and not invalid.retryable
    assert redirects.code == "redirect-loop" and not redirects.retryable


def test_session_retry_policy_has_no_other_error_replay_and_safe_status_methods():
    session = create_session(retries=3)
    retry = session.get_adapter("https://").max_retries
    assert retry.other == 0
    assert retry.allowed_methods == frozenset({"HEAD", "GET", "OPTIONS"})
    assert retry.respect_retry_after_header is True


def test_health_doctor_surfaces_proxy_specific_fix():
    from ctf_downloader.services.health_service import DoctorReport, HealthService

    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise requests.exceptions.ProxyError("proxy refused")

    report = DoctorReport("https://ctf.example")
    ok = HealthService()._check_url(report, "https://ctf.example", BrokenSession())
    assert ok is False
    check = report.checks[-1]
    assert "proxy-error" in check.detail
    assert "NO_PROXY" in check.fix


def test_pull_detect_failure_uses_stable_dns_category(monkeypatch):
    from types import SimpleNamespace
    from ctf_downloader.services import pull_service as pull_mod

    captured = {}
    monkeypatch.setattr(
        pull_mod,
        "render_diagnostic",
        lambda diag: captured.setdefault("diag", diag),
    )
    exc = _caused(
        requests.ConnectionError("pool failed"),
        socket.gaierror(-3, "Temporary failure in name resolution"),
    )
    result = pull_mod.PullService._render_detect_failure(
        SimpleNamespace(output_dir="/tmp/ws"),
        0.0,
        exc,
    )
    diag = captured["diag"]
    assert result["ok"] is False
    assert "dns-error" in (diag.cause or "")
    assert any("DNS" in hint for hint in diag.hints)


def test_http_downloader_network_text_is_stable():
    from ctf_downloader.downloaders.http_downloader import HttpDownloader

    reason, hint = HttpDownloader._network_error_text(
        requests.ReadTimeout("slow"), "GET"
    )
    assert reason.startswith("read-timeout:")
    assert "timeout" in hint.lower()


def test_retry_after_parser_supports_delta_seconds_and_http_date():
    from email.utils import formatdate
    from ctf_downloader.utils.http_client import parse_retry_after_seconds

    now = 1_700_000_000.0
    assert parse_retry_after_seconds("90", now=now) == 90.0
    http_date = formatdate(now + 120, usegmt=True)
    parsed = parse_retry_after_seconds(http_date, now=now)
    assert parsed is not None
    assert abs(parsed - 120.0) < 0.001


def test_retry_after_parser_rejects_nan_invalid_and_clamps_past_date():
    from email.utils import formatdate
    from ctf_downloader.utils.http_client import parse_retry_after_seconds

    now = 1_700_000_000.0
    assert parse_retry_after_seconds("nan", now=now) is None
    assert parse_retry_after_seconds("not-a-date", now=now) is None
    assert parse_retry_after_seconds(formatdate(now - 30, usegmt=True), now=now) == 0.0
