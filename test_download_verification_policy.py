"""P1 download verification + redirect policy contracts."""

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.downloaders.http_downloader import (
    DownloadFailed,
    HttpDownloader,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=(), url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = tuple(chunks)
        self.url = url

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestVerifyModes(unittest.TestCase):
    URL = "https://example.test/a.bin"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "a.bin")

    def _seed(self, body=b"OLD", *, etag='"v1"', sha=None):
        with open(self.path, "wb") as f:
            f.write(body)
        HttpDownloader._write_final_metadata(self.path, {
            "url": self.URL,
            "final_url": self.URL,
            "etag": etag,
            "last_modified": None,
            "sha256": sha,
        })

    def test_fresh_download_persists_validator_metadata(self):
        session = MagicMock()
        session.get.return_value = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": "3",
                "ETag": '"v1"',
            },
            (b"ABC",),
            self.URL,
        )
        saved = HttpDownloader.download_file(
            self.URL,
            self.tmp.name,
            session,
            preferred_filename="a.bin",
        )
        self.assertEqual(open(saved, "rb").read(), b"ABC")
        meta = HttpDownloader.load_final_metadata(saved)
        self.assertEqual(meta["url"], self.URL)
        self.assertEqual(meta["etag"], '"v1"')
        self.assertEqual(meta["size"], 3)
        self.assertIsNone(meta["sha256"])

    def test_normal_304_skips_body_download(self):
        self._seed()
        session = MagicMock()
        session.head.return_value = FakeResponse(304, {"ETag": '"v1"'}, url=self.URL)

        saved = HttpDownloader.download_file(
            self.URL,
            self.tmp.name,
            session,
            preferred_filename="a.bin",
            verify_mode="normal",
        )

        self.assertEqual(saved, self.path)
        self.assertEqual(open(saved, "rb").read(), b"OLD")
        session.get.assert_not_called()
        self.assertEqual(
            session.head.call_args.kwargs["headers"]["If-None-Match"],
            '"v1"',
        )

    def test_normal_changed_etag_redownloads(self):
        self._seed()
        session = MagicMock()
        session.head.return_value = FakeResponse(
            200,
            {"Content-Length": "3", "ETag": '"v2"'},
            url=self.URL,
        )
        session.get.return_value = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": "3",
                "ETag": '"v2"',
            },
            (b"NEW",),
            self.URL,
        )

        saved = HttpDownloader.download_file(
            self.URL,
            self.tmp.name,
            session,
            preferred_filename="a.bin",
            verify_mode="normal",
        )

        self.assertEqual(open(saved, "rb").read(), b"NEW")
        self.assertEqual(HttpDownloader.load_final_metadata(saved)["etag"], '"v2"')
        self.assertEqual(session.get.call_count, 1)

    def test_strict_detects_same_size_local_corruption(self):
        good = b"GOOD"
        bad = b"EVIL"
        self._seed(
            bad,
            etag='"v1"',
            sha=hashlib.sha256(good).hexdigest(),
        )
        session = MagicMock()
        session.head.return_value = FakeResponse(304, {"ETag": '"v1"'}, url=self.URL)
        session.get.return_value = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(good)),
                "ETag": '"v1"',
            },
            (good,),
            self.URL,
        )

        saved = HttpDownloader.download_file(
            self.URL,
            self.tmp.name,
            session,
            preferred_filename="a.bin",
            verify_mode="strict",
        )

        self.assertEqual(open(saved, "rb").read(), good)
        meta = HttpDownloader.load_final_metadata(saved)
        self.assertEqual(meta["sha256"], hashlib.sha256(good).hexdigest())
        self.assertEqual(session.get.call_count, 1)

    def test_strict_first_download_records_sha256(self):
        session = MagicMock()
        session.get.return_value = FakeResponse(
            200,
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": "4",
                "ETag": '"v1"',
            },
            (b"DATA",),
            self.URL,
        )
        saved = HttpDownloader.download_file(
            self.URL,
            self.tmp.name,
            session,
            preferred_filename="a.bin",
            verify_mode="strict",
        )
        self.assertEqual(
            HttpDownloader.load_final_metadata(saved)["sha256"],
            hashlib.sha256(b"DATA").hexdigest(),
        )


class TestRedirectPolicy(unittest.TestCase):
    PUBLIC = "http://8.8.8.8/file.bin"
    PRIVATE = "http://127.0.0.1/file.bin"

    def test_public_to_private_redirect_blocked_by_default(self):
        session = MagicMock()
        session.headers = {"Authorization": "Bearer SECRET"}
        session.get.return_value = FakeResponse(
            302, {"Location": self.PRIVATE}, url=self.PUBLIC
        )

        with self.assertRaisesRegex(DownloadFailed, "public .* private"):
            HttpDownloader._request_follow_redirects(
                session, "GET", self.PUBLIC, stream=True
            )
        self.assertEqual(session.get.call_count, 1)

    def test_opt_in_allows_private_redirect_and_strips_cross_origin_secrets(self):
        session = MagicMock()
        session.headers = {
            "Authorization": "Bearer SECRET",
            "X-Api-Key": "TOPSECRET",
            "User-Agent": "ctf-test",
        }
        session.get.side_effect = [
            FakeResponse(302, {"Location": self.PRIVATE}, url=self.PUBLIC),
            FakeResponse(200, {"Content-Length": "1"}, (b"X",), self.PRIVATE),
        ]

        resp, final_url = HttpDownloader._request_follow_redirects(
            session,
            "GET",
            self.PUBLIC,
            stream=True,
            headers={"Range": "bytes=0-0"},
            allow_private_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(final_url, self.PRIVATE)
        second_headers = session.get.call_args_list[1].kwargs["headers"]
        self.assertIsNone(second_headers["Authorization"])
        self.assertIsNone(second_headers["X-Api-Key"])
        self.assertEqual(second_headers["Range"], "bytes=0-0")

    def test_initial_third_party_url_strips_platform_session_secrets(self):
        session = MagicMock()
        session._credential_origin = "https://ctf.example.test"
        session.headers = {
            "Authorization": "Bearer SECRET",
            "X-Api-Key": "TOPSECRET",
            "User-Agent": "ctf-test",
        }
        third_party = "https://cdn.other.test/file.bin"
        session.get.return_value = FakeResponse(
            200, {"Content-Length": "1"}, (b"X",), third_party
        )

        resp, final_url = HttpDownloader._request_follow_redirects(
            session,
            "GET",
            third_party,
            stream=True,
            headers={"Range": "bytes=0-0"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(final_url, third_party)
        sent = session.get.call_args.kwargs["headers"]
        self.assertIsNone(sent["Authorization"])
        self.assertIsNone(sent["X-Api-Key"])
        self.assertEqual(sent["Range"], "bytes=0-0")

    def test_private_initial_url_remains_valid_without_opt_in(self):
        session = MagicMock()
        session.headers = {}
        session.get.return_value = FakeResponse(
            200, {"Content-Length": "1"}, (b"X",), self.PRIVATE
        )
        resp, final_url = HttpDownloader._request_follow_redirects(
            session, "GET", self.PRIVATE, stream=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(final_url, self.PRIVATE)


class TestDownloaderConfigPolicy(unittest.TestCase):
    def test_verify_mode_validation(self):
        cfg = DownloaderConfig(url="https://ctf.test", verify_downloads="STRICT")
        cfg.validate()
        self.assertEqual(cfg.verify_downloads, "strict")

        with self.assertRaises(ValueError):
            DownloaderConfig(
                url="https://ctf.test", verify_downloads="paranoid"
            ).validate()


if __name__ == "__main__":
    unittest.main()
