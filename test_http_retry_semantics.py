import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ctf_downloader.utils.http_client import create_session


class _RetryHandler(BaseHTTPRequestHandler):
    get_503_count = 0
    get_429_count = 0
    post_count = 0

    def log_message(self, *_args):
        pass

    def _send(self, status, body=b"ok", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        cls = type(self)
        if self.path == "/eventual-503":
            cls.get_503_count += 1
            if cls.get_503_count < 3:
                return self._send(503, b"busy")
            return self._send(200, b"ok")
        if self.path == "/eventual-429":
            cls.get_429_count += 1
            if cls.get_429_count < 2:
                return self._send(429, b"slow", {"Retry-After": "0"})
            return self._send(200, b"ok")
        return self._send(404, b"missing")

    def do_POST(self):
        type(self).post_count += 1
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        return self._send(503, b"mutation rejected")


@pytest.fixture
def retry_server():
    _RetryHandler.get_503_count = 0
    _RetryHandler.get_429_count = 0
    _RetryHandler.post_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RetryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_get_503_is_retried_until_success(retry_server):
    session = create_session(retries=3, backoff_factor=0)
    response = session.get(retry_server + "/eventual-503", timeout=2)
    assert response.status_code == 200
    assert _RetryHandler.get_503_count == 3


def test_get_429_respects_retry_after_and_retries(retry_server):
    session = create_session(retries=2, backoff_factor=0)
    response = session.get(retry_server + "/eventual-429", timeout=2)
    assert response.status_code == 200
    assert _RetryHandler.get_429_count == 2


def test_post_503_is_never_replayed(retry_server):
    session = create_session(retries=5, backoff_factor=0)
    response = session.post(
        retry_server + "/mutation",
        json={"flag": "FLAG{x}"},
        timeout=2,
    )
    assert response.status_code == 503
    assert _RetryHandler.post_count == 1
