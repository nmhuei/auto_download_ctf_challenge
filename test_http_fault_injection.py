"""P0 integration tests for the direct HTTP downloader.

These tests intentionally use a real localhost HTTP server instead of mocking
requests so Range/If-Range, chunked bodies and connection semantics are
exercised end-to-end.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from ctf_downloader.downloaders.http_downloader import HttpDownloader


_PAYLOAD = (b"0123456789abcdef" * 4096)  # 64 KiB
_ETAG = '"ctf-v1"'


class _FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, _fmt, *_args):
        return

    def _record(self):
        type(self).seen.append({
            "path": self.path,
            "range": self.headers.get("Range"),
            "if_range": self.headers.get("If-Range"),
        })

    def _full(self, *, include_length=True):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", _ETAG)
        if include_length:
            self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def do_GET(self):  # noqa: N802
        self._record()

        if self.path == "/chunked":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("ETag", _ETAG)
            self.end_headers()
            for piece in (_PAYLOAD[:12345], _PAYLOAD[12345:]):
                self.wfile.write(f"{len(piece):X}\r\n".encode("ascii"))
                self.wfile.write(piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        if self.path in ("/range", "/wrong-range"):
            range_header = self.headers.get("Range")
            if range_header:
                if_range = self.headers.get("If-Range")
                if if_range and if_range != _ETAG:
                    self._full()
                    return

                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                body = _PAYLOAD[start:]
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("ETag", _ETAG)
                advertised_start = 0 if self.path == "/wrong-range" else start
                self.send_header(
                    "Content-Range",
                    f"bytes {advertised_start}-{advertised_start + len(body) - 1}/{len(_PAYLOAD)}",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._full()
            return

        self._full()


class TestHttpFaultInjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _FaultHandler.seen = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FaultHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        _FaultHandler.seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = requests.Session()
        self.addCleanup(self.session.close)

    def test_normal_and_chunked_downloads_are_finalized_atomically(self):
        p1 = HttpDownloader.download_file(
            self.base + "/normal", self.tmp.name, self.session,
            preferred_filename="normal.bin",
        )
        p2 = HttpDownloader.download_file(
            self.base + "/chunked", self.tmp.name, self.session,
            preferred_filename="chunked.bin",
        )
        self.assertEqual(open(p1, "rb").read(), _PAYLOAD)
        self.assertEqual(open(p2, "rb").read(), _PAYLOAD)
        self.assertFalse(os.path.exists(p1 + ".part"))
        self.assertFalse(os.path.exists(p2 + ".part"))

    def test_resume_sends_if_range_from_persisted_etag(self):
        target = os.path.join(self.tmp.name, "resume.bin")
        part = target + ".part"
        prefix = _PAYLOAD[:10000]
        with open(part, "wb") as f:
            f.write(prefix)
        with open(HttpDownloader._resume_meta_path(part), "w", encoding="utf-8") as f:
            json.dump({"etag": _ETAG, "last_modified": None}, f)

        path = HttpDownloader.download_file(
            self.base + "/range", self.tmp.name, self.session,
            preferred_filename="resume.bin",
        )

        self.assertEqual(open(path, "rb").read(), _PAYLOAD)
        range_req = next(row for row in _FaultHandler.seen if row["range"])
        self.assertEqual(range_req["range"], f"bytes={len(prefix)}-")
        self.assertEqual(range_req["if_range"], _ETAG)
        self.assertFalse(os.path.exists(HttpDownloader._resume_meta_path(part)))

    def test_changed_etag_forces_full_restart_instead_of_mixing_versions(self):
        target = os.path.join(self.tmp.name, "changed.bin")
        part = target + ".part"
        with open(part, "wb") as f:
            f.write(b"OLD-VERSION-PREFIX")
        with open(HttpDownloader._resume_meta_path(part), "w", encoding="utf-8") as f:
            json.dump({"etag": '"old-version"', "last_modified": None}, f)

        path = HttpDownloader.download_file(
            self.base + "/range", self.tmp.name, self.session,
            preferred_filename="changed.bin",
        )

        self.assertEqual(open(path, "rb").read(), _PAYLOAD)
        first = _FaultHandler.seen[0]
        self.assertIsNotNone(first["range"])
        self.assertEqual(first["if_range"], '"old-version"')

    @patch.object(HttpDownloader, "_retry_backoff", return_value=0)
    def test_wrong_content_range_is_rejected_and_restarted(self, _backoff):
        target = os.path.join(self.tmp.name, "wrong.bin")
        part = target + ".part"
        prefix = _PAYLOAD[:8192]
        with open(part, "wb") as f:
            f.write(prefix)
        with open(HttpDownloader._resume_meta_path(part), "w", encoding="utf-8") as f:
            json.dump({"etag": _ETAG, "last_modified": None}, f)

        path = HttpDownloader.download_file(
            self.base + "/wrong-range", self.tmp.name, self.session,
            preferred_filename="wrong.bin",
        )

        self.assertEqual(open(path, "rb").read(), _PAYLOAD)
        self.assertGreaterEqual(len(_FaultHandler.seen), 2)
        self.assertIsNotNone(_FaultHandler.seen[0]["range"])
        self.assertIsNone(_FaultHandler.seen[-1]["range"])


if __name__ == "__main__":
    unittest.main()
