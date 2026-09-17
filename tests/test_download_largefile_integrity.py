"""Integrity regressions for the large-file Range accelerator."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.downloaders.http_downloader import HttpDownloader


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = tuple(chunks)

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestDownloadUrlPolicy(unittest.TestCase):
    def test_rejects_non_http_scheme_and_embedded_credentials(self):
        with self.assertRaisesRegex(Exception, "chỉ cho phép http/https"):
            HttpDownloader._validate_remote_url("file:///etc/passwd")
        with self.assertRaisesRegex(Exception, "username/password"):
            HttpDownloader._validate_remote_url("https://user:pass@example.test/a.zip")

    def test_private_http_host_remains_allowed_for_ctf_labs(self):
        # Private/loopback is a legitimate CTF topology; policy is explicit.
        HttpDownloader._validate_remote_url("http://127.0.0.1:31337/challenge.zip")
        HttpDownloader._validate_remote_url("http://10.10.10.10/challenge.zip")


class TestLargeFileIntegrity(unittest.TestCase):
    def test_segment_rejects_wrong_content_range_even_when_size_matches(self):
        total = 8 * 1024 * 1024
        session = MagicMock()

        def wrong_range(_url, stream=True, timeout=30, headers=None):
            requested = headers["Range"].replace("bytes=", "")
            start_s, end_s = requested.split("-", 1)
            start, end = int(start_s), int(end_s)
            body = b"X" * (end - start + 1)
            # Deliberately lie about the first byte while preserving body size.
            return FakeResponse(
                206,
                {
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes 0-{len(body)-1}/{total}",
                },
                (body,),
            )

        session.get.side_effect = wrong_range
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "large.bin")
            part = target + ".part"
            ok = HttpDownloader._download_parallel_segments(
                "https://example.test/large.bin",
                target,
                part,
                total,
                session,
            )
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(part))
            self.assertFalse(any(name.endswith(".seg0") for name in os.listdir(td)))

    @patch.object(HttpDownloader, "_download_parallel_segments")
    def test_cloudflare_active_session_uses_single_stream_not_segment_threads(self, parallel):
        total = 8 * 1024 * 1024
        session = MagicMock()
        session.cloudflare_active = True
        session.get.return_value = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total),
                "Accept-Ranges": "bytes",
            },
            (b"A" * total,),
        )

        with tempfile.TemporaryDirectory() as td:
            path = HttpDownloader.download_file(
                "https://example.test/large.bin",
                td,
                session,
                preferred_filename="large.bin",
            )
            self.assertEqual(path, os.path.join(td, "large.bin"))
            self.assertEqual(os.path.getsize(path), total)
        parallel.assert_not_called()

    @patch.object(HttpDownloader, "_download_parallel_segments", return_value=False)
    def test_failed_parallel_path_reenters_validation_before_writing(self, _parallel):
        total = 9 * 1024 * 1024
        session = MagicMock()
        initial = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total),
                "Accept-Ranges": "bytes",
            },
            (),
        )
        # A malicious/broken fallback response with exactly the advertised
        # length. The old ad-hoc fallback path could write this despite 500.
        server_error = FakeResponse(
            500,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total),
            },
            (b"E" * total,),
        )
        session.get.side_effect = [initial, server_error]

        with tempfile.TemporaryDirectory() as td:
            path = HttpDownloader.download_file(
                "https://example.test/large.bin",
                td,
                session,
                preferred_filename="large.bin",
            )
            self.assertIsNone(path)
            self.assertFalse(os.path.exists(os.path.join(td, "large.bin")))
            self.assertFalse(os.path.exists(os.path.join(td, "large.bin.part")))
            self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
