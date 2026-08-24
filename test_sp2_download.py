"""
SP2 — Large-file consent + hạ tầng download.
Chạy: python3 -m unittest test_sp2_download.py -v
Toàn bộ test dùng mock, KHÔNG gọi mạng tới server thật.
"""
import json
import os
import shutil
import types
import unittest
from unittest.mock import MagicMock, patch, call

import requests

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.utils.http_client import create_session, DEFAULT_USER_AGENT
from ctf_downloader.extractors.link_extractor import LinkExtractor
from ctf_downloader.downloaders.http_downloader import (
    HttpDownloader,
    DownloadFailed,
    LargeFileSkipped,
)
from ctf_downloader.downloaders.manager import DownloadManager
from ctf_downloader.downloaders.mediafire import MediafireDownloader
from ctf_downloader.downloaders.dropbox import DropboxDownloader


class FakeStdin:
    """Giả lập sys.stdin với chế độ tty / non-tty."""

    def __init__(self, isatty: bool):
        self._is_tty = isatty

    def isatty(self) -> bool:
        return self._is_tty


class FakeResponse:
    """
    Response giả cho requests. Hỗ trợ context manager, iter_content (tuỳ chọn
    raise exception giữa chừng để mô phỏng mất kết nối) và .json().
    """

    def __init__(
        self,
        status_code: int = 200,
        headers: dict = None,
        chunks: tuple = (),
        error_after_chunks: int = None,
        error: Exception = None,
        text: str = "",
        json_data: dict = None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = tuple(chunks)
        self._error_after = error_after_chunks
        self._error = error or requests.exceptions.ChunkedEncodingError("Connection broken")
        self.text = text
        self._json_data = json_data or {}
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def iter_content(self, chunk_size: int = 65536):
        emitted = 0
        for chunk in self._chunks:
            if self._error_after is not None and emitted >= self._error_after:
                raise self._error
            yield chunk
            emitted += 1
        if self._error_after is not None and emitted < self._error_after:
            # đã hết chunk nhưng vẫn chưa đủ mốc lỗi -> raise luôn
            raise self._error

    def json(self):
        return self._json_data


def make_session_mock(head_raises=True):
    """
    Session MagicMock: mặc định HEAD raise (probe trả unknown-size);
    các test override session.get / session.head theo nhu cầu.
    """
    session = MagicMock()
    if head_raises:
        session.head.side_effect = requests.ConnectionError("no head support")
    return session


BINARY_HEADERS = {"Content-Type": "application/octet-stream"}


class TestConfigDefaults(unittest.TestCase):
    def test_default_size_limit_is_1gb_and_zero_disables_gate(self):
        cfg = DownloaderConfig(url="https://demo.ctfd.io")
        self.assertEqual(cfg.size_limit_bytes, 1073741824)

        cfg_zero = DownloaderConfig(url="https://demo.ctfd.io", size_limit_bytes=0)
        self.assertEqual(cfg_zero.size_limit_bytes, 0)


class TestHttpClientDefaults(unittest.TestCase):
    def test_browser_user_agent_present(self):
        session = create_session()
        ua = session.headers.get("User-Agent")
        self.assertTrue(ua)
        self.assertEqual(ua, DEFAULT_USER_AGENT)
        # UA phải giống browser thật (Chrome trên Linux), không phải python-requests
        self.assertNotIn("python-requests", ua)
        self.assertIn("Chrome", ua)

    def test_accept_json_first_no_global_content_type(self):
        session = create_session()
        accept = session.headers.get("Accept", "")
        self.assertIn("application/json", accept)
        # Content-Type KHÔNG được đặt toàn cục (phá form login)
        self.assertNotIn("content-type", {k.lower() for k in session.headers.keys()})

    def test_retry_allowed_methods_excludes_post(self):
        session = create_session()
        adapter = session.adapters["https://"]
        allowed = adapter.max_retries.allowed_methods
        self.assertIn("GET", allowed)
        self.assertIn("HEAD", allowed)
        self.assertIn("OPTIONS", allowed)
        self.assertNotIn("POST", allowed)


class TestConsentGate(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def _manager(self, limit=1024):
        return DownloadManager(session=make_session_mock(), timeout=5, size_limit_bytes=limit)

    def test_under_limit_no_prompt(self):
        mgr = self._manager(limit=1024)
        with patch("sys.stdin", FakeStdin(True)), patch("builtins.input") as mock_input:
            self.assertTrue(mgr._confirm_large_download("https://x/f.bin", 512))
            mock_input.assert_not_called()

    def test_zero_limit_never_prompts_even_for_huge_file(self):
        mgr = self._manager(limit=0)
        with patch("sys.stdin", FakeStdin(False)), patch("builtins.input") as mock_input:
            self.assertTrue(mgr._confirm_large_download("https://x/f.bin", 10 ** 12))
            mock_input.assert_not_called()

    def test_unknown_size_no_prompt(self):
        mgr = self._manager(limit=1024)
        with patch("sys.stdin", FakeStdin(False)), patch("builtins.input") as mock_input:
            self.assertTrue(mgr._confirm_large_download("https://x/f.bin", None))
            mock_input.assert_not_called()

    def test_over_limit_user_says_yes(self):
        mgr = self._manager(limit=1024)
        with patch("sys.stdin", FakeStdin(True)), patch("builtins.input", return_value="y") as mock_input:
            self.assertTrue(mgr._confirm_large_download("https://x/big.bin", 2048))
            mock_input.assert_called_once()
            self.assertIn("[y/N]", mock_input.call_args[0][0])

    def test_over_limit_user_says_no(self):
        mgr = self._manager(limit=1024)
        with patch("sys.stdin", FakeStdin(True)), patch("builtins.input", return_value="n"):
            self.assertFalse(mgr._confirm_large_download("https://x/big.bin", 2048))

    def test_over_limit_non_interactive_auto_skip_with_warning(self):
        mgr = self._manager(limit=1024)
        with patch("sys.stdin", FakeStdin(False)), patch("builtins.input") as mock_input:
            self.assertFalse(mgr._confirm_large_download("https://x/big.bin", 2048))
            mock_input.assert_not_called()


class TestDirectDownloadFlow(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def _list_files(self):
        return sorted(os.listdir(self.tmp_dir))

    def test_preflight_head_over_limit_skips_without_get(self):
        session = make_session_mock(head_raises=False)
        session.head.return_value = FakeResponse(
            status_code=200,
            headers={"Content-Length": "5000000000"},
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=1073741824)

        with patch("sys.stdin", FakeStdin(False)):
            success, path, msg = mgr.download_url("https://host.com/huge.iso", self.tmp_dir)

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("skipped_large_file", msg)
        session.get.assert_not_called()
        self.assertEqual(self._list_files(), [])

    def test_preflight_head_within_limit_downloads(self):
        session = make_session_mock(head_raises=False)
        session.head.return_value = FakeResponse(status_code=200, headers={"Content-Length": "6"})
        session.get.return_value = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": "6"},
            chunks=(b"hello!",),
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=1073741824)

        with patch("sys.stdin", FakeStdin(False)):
            success, path, msg = mgr.download_url("https://host.com/small.zip", self.tmp_dir)

        self.assertTrue(success, msg)
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"hello!")

    def test_unknown_size_aborts_early_when_content_length_exceeds_limit(self):
        # HEAD fail -> unknown; GET không có Content-Length nhưng body vượt ngưỡng
        session = make_session_mock(head_raises=True)
        session.get.return_value = FakeResponse(
            status_code=200,
            headers=dict(BINARY_HEADERS),  # không có Content-Length
            chunks=(b"A" * 700, b"B" * 700),
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=1000)

        with patch("sys.stdin", FakeStdin(False)):
            success, path, msg = mgr.download_url("https://host.com/mystery.bin", self.tmp_dir)

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("skipped_large_file", msg)
        # Không lưu bất kỳ file nào (.part/.tmp phải bị dọn sạch)
        self.assertEqual(self._list_files(), [])
        # Chỉ mở đúng 1 GET (ngắt sớm, KHÔNG tải tiếp body / retry)
        self.assertEqual(session.get.call_count, 1)

    def test_unknown_size_aborts_before_writing_when_cl_known(self):
        # CL xuất hiện ở response cuối NGAY TRƯỚC KHI ghi -> không viết byte nào
        session = make_session_mock(head_raises=True)
        session.get.return_value = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": str(5 * 1024 * 1024 * 1024)},
            chunks=(b"data",),
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=1024)

        with patch("sys.stdin", FakeStdin(False)):
            success, path, msg = mgr.download_url("https://host.com/mystery.bin", self.tmp_dir)

        self.assertFalse(success)
        self.assertIn("skipped_large_file", msg)
        self.assertEqual(self._list_files(), [])

    def test_interstitial_html_guard_does_not_save_file(self):
        session = make_session_mock(head_raises=True)
        session.get.return_value = FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            chunks=(b"<html>login please</html>",),
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=0)

        success, path, msg = mgr.download_url("https://host.com/file.zip", self.tmp_dir)

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("HTML", msg)
        self.assertIn("interstitial", msg)
        self.assertEqual(self._list_files(), [])

    def test_discord_expired_link_message(self):
        session = make_session_mock(head_raises=True)
        session.get.return_value = FakeResponse(status_code=404, headers=BINARY_HEADERS)
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=0)

        url = "https://cdn.discordapp.com/attachments/1/2/chall.zip?ex=666&is=555&hm=abc"
        success, path, msg = mgr.download_url(url, self.tmp_dir, link_type="direct_file")

        self.assertFalse(success)
        self.assertIn("Discord", msg)
        self.assertIn("hết hạn", msg)


class TestResume(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def test_resume_sends_range_header_and_merges_206_body(self):
        first = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": "300"},
            chunks=(b"A" * 200,),
            error_after_chunks=1,  # nhận 200 byte rồi đứt kết nối
        )
        second = FakeResponse(
            status_code=206,  # Partial Content
            headers={**BINARY_HEADERS, "Content-Length": "100"},
            chunks=(b"B" * 100,),
        )
        session = MagicMock()
        session.get.side_effect = [first, second]

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0
        )

        self.assertIsNotNone(saved)
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"A" * 200 + b"B" * 100)

        # GET lần 2 phải mang Range: bytes=200-
        self.assertEqual(session.get.call_count, 2)
        second_kwargs = session.get.call_args_list[1].kwargs
        self.assertEqual(second_kwargs.get("headers", {}).get("Range"), "bytes=200-")

        # Không còn file tạm sau khi hoàn tất
        self.assertEqual(sorted(os.listdir(self.tmp_dir)), ["blob.bin"])

    def test_server_ignoring_range_200_restarts_from_scratch(self):
        partial_part_path = os.path.join(self.tmp_dir, "blob.bin.part")
        with open(partial_part_path, "wb") as f:
            f.write(b"STALE" * 10)

        fresh = FakeResponse(
            status_code=200,  # server không hỗ trợ Range -> trả 200 đầy đủ
            headers={**BINARY_HEADERS, "Content-Length": "8"},
            chunks=(b"COMPLETE",),
        )
        session = MagicMock()
        session.get.return_value = fresh

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0
        )

        self.assertIsNotNone(saved)
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"COMPLETE")
        self.assertFalse(os.path.exists(partial_part_path))


class TestMegaBranches(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def test_link_extractor_classifies_mega_without_tool_as_not_downloadable(self):
        with patch("shutil.which", return_value=None):
            link = LinkExtractor.classify_link("https://mega.nz/file/AbCdEf/file")
        self.assertEqual(link.link_type, "mega")
        self.assertFalse(link.is_downloadable)

    def test_link_extractor_classifies_mega_with_tool_as_downloadable(self):
        with patch("shutil.which", return_value="/usr/bin/megadl"):
            link = LinkExtractor.classify_link("https://mega.nz/file/AbCdEf/file")
        self.assertEqual(link.link_type, "mega")
        self.assertTrue(link.is_downloadable)

    def test_manager_skips_mega_without_tool_with_install_hint(self):
        mgr = DownloadManager(session=make_session_mock(), timeout=5, size_limit_bytes=0)
        with patch("ctf_downloader.downloaders.mega.shutil.which", return_value=None):
            success, path, msg = mgr.download_url("https://mega.nz/file/xyz", self.tmp_dir, link_type="mega")

        self.assertFalse(success)
        self.assertIsNone(path)
        self.assertIn("megatools", msg)
        self.assertIn("megadl", msg)

    def test_manager_runs_megadl_when_tool_available(self):
        def fake_run(cmd, **kwargs):
            # megadl "tải" file vào đích trước khi trả exit 0
            dest_dir = kwargs.get("cwd")
            out_file = os.path.join(dest_dir, "payload.rar")
            with open(out_file, "wb") as f:
                f.write(b"MEGADATA")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        mgr = DownloadManager(session=make_session_mock(), timeout=5, size_limit_bytes=0)
        with patch("ctf_downloader.downloaders.mega.shutil.which", return_value="/usr/bin/megadl"), \
             patch("ctf_downloader.downloaders.mega.subprocess.run", side_effect=fake_run) as mock_run:
            success, path, msg = mgr.download_url("https://mega.nz/file/xyz", self.tmp_dir, link_type="mega")

        self.assertTrue(success, msg)
        self.assertEqual(path, os.path.join(self.tmp_dir, "payload.rar"))
        cmd_used = mock_run.call_args[0][0]
        self.assertEqual(os.path.basename(cmd_used[0]), "megadl")
        self.assertIn("https://mega.nz/file/xyz", cmd_used)

    def test_mega_tool_failure_reports_exit_code(self):
        mgr = DownloadManager(session=make_session_mock(), timeout=5, size_limit_bytes=0)
        failing = types.SimpleNamespace(returncode=2, stdout="", stderr="No valid login")
        with patch("ctf_downloader.downloaders.mega.shutil.which", return_value="/usr/bin/megadl"), \
             patch("ctf_downloader.downloaders.mega.subprocess.run", return_value=failing):
            success, path, msg = mgr.download_url("https://mega.nz/file/xyz", self.tmp_dir, link_type="mega")

        self.assertFalse(success)
        self.assertIn("exit 2", msg)
        self.assertIn("No valid login", msg)


class TestMediafirePreflight(unittest.TestCase):
    def test_extract_quick_key_patterns(self):
        self.assertEqual(
            MediafireDownloader.extract_quick_key("https://www.mediafire.com/file/abc123/payload.rar/file"),
            "abc123",
        )
        self.assertEqual(
            MediafireDownloader.extract_quick_key("https://www.mediafire.com/download/xyz789/a.7z"),
            "xyz789",
        )
        self.assertEqual(MediafireDownloader.extract_quick_key("https://mediafire.com/?oldkey1"), "oldkey1")
        self.assertIsNone(MediafireDownloader.extract_quick_key("https://example.com/nothing"))

    def test_expected_size_from_public_api(self):
        session = MagicMock()
        api_resp = FakeResponse(
            status_code=200,
            json_data={"response": {"status": "ok", "file_info": {"size": "2048"}}},
        )

        def fake_get(url, *args, **kwargs):
            self.assertIn("get_info.php", url)
            self.assertEqual(kwargs.get("params", {}).get("quick_key"), "abc123")
            return api_resp

        session.get.side_effect = fake_get
        size = MediafireDownloader.get_expected_size(
            "https://www.mediafire.com/file/abc123/payload.rar/file", session, timeout=5
        )
        self.assertEqual(size, 2048)

    def test_expected_size_fallback_scrape_when_api_fails(self):
        session = MagicMock()
        bad_api = FakeResponse(status_code=500)
        page = FakeResponse(
            status_code=200,
            text='<ul><li>File size:</li><li>2.50MB</li></ul><a id="downloadButton" href="http://download199.mediafire.com/x/y/file.rar">',
        )

        def fake_get(url, *args, **kwargs):
            if "get_info.php" in url:
                return bad_api
            return page

        session.get.side_effect = fake_get
        size = MediafireDownloader.get_expected_size(
            "https://www.mediafire.com/file/abc123/payload.rar/file", session, timeout=5
        )
        self.assertEqual(size, int(2.50 * 1024 * 1024))

    def test_stream_returns_preflight_size(self):
        session = MagicMock()

        api_resp = FakeResponse(status_code=200, json_data={"response": {"file_info": {"size": "4096"}}})
        page = FakeResponse(
            status_code=200,
            text='<a id="downloadButton" href="http://download199.mediafire.com/x/y/file.rar">',
        )
        stream = FakeResponse(
            status_code=200,
            headers={"Content-Disposition": 'attachment; filename="file.rar"', **BINARY_HEADERS},
        )

        def fake_get(url, *args, **kwargs):
            if "get_info.php" in url:
                return api_resp
            if "download199.mediafire.com" in url:
                return stream
            return page

        session.get.side_effect = fake_get
        resp, expected = MediafireDownloader.get_download_stream(
            "https://www.mediafire.com/file/abc123/file.rar/file", session, timeout=5
        )
        self.assertIs(resp, stream)
        self.assertEqual(expected, 4096)


class TestDropboxHandling(unittest.TestCase):
    def test_dropbox_429_bandwidth_cap_has_specific_message(self):
        session = make_session_mock()
        session.get.return_value = FakeResponse(status_code=429, headers={})
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=0)

        success, path, msg = mgr.download_url("https://www.dropbox.com/s/abc/chall.zip?dl=0", ".", link_type="dropbox")

        self.assertFalse(success)
        self.assertIn("429", msg)
        self.assertIn("bandwidth", msg.lower())
        # URL phải được chuyển sang ?dl=1
        requested_url = session.get.call_args[0][0]
        self.assertIn("dl=1", requested_url)

    def test_dropbox_direct_download_success_returns_stream_and_size(self):
        session = make_session_mock()
        session.get.return_value = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": "123"},
        )
        resp, expected = DropboxDownloader.get_download_stream(
            "https://www.dropbox.com/s/abc/chall.zip?dl=0", session, timeout=5
        )
        self.assertIsNotNone(resp)
        self.assertEqual(expected, 123)


class TestGDriveQuota(unittest.TestCase):
    def test_quota_exceeded_html_gives_specific_error(self):
        session = make_session_mock()
        quota_html = "<html><body>We're sorry, too many users have viewed or downloaded this file recently.</body></html>"
        session.get.return_value = FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text=quota_html,
        )
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=0)

        success, path, msg = mgr.download_url(
            "https://drive.google.com/file/d/1ABCdefGHI/view?usp=sharing", ".", link_type="gdrive"
        )

        self.assertFalse(success)
        self.assertIn("quota", msg.lower())

    def test_confirm_token_flow_still_works_and_yields_unknown_size(self):
        session = make_session_mock()
        interstitial = FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text='<form action=""><input type="hidden" name="confirm" value="t0k3n"></form>',
        )
        file_stream = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Disposition": 'attachment; filename="data.bin"'},
            chunks=(b"GDRIVE-DATA",),
        )

        session.get.side_effect = [interstitial, file_stream]
        mgr = DownloadManager(session=session, timeout=5, size_limit_bytes=0)

        tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            success, path, msg = mgr.download_url(
                "https://drive.google.com/file/d/1ABCdefGHI/view", tmp_dir, link_type="gdrive"
            )
            self.assertTrue(success, msg)
            self.assertIsNotNone(path)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"GDRIVE-DATA")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestWave4Resume416(unittest.TestCase):
    """WH3 HD-1a/HD-1h/HD-1i: resume chết vĩnh viễn + disk-full sót .part/.tmp."""

    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_resume_416_resets_part_and_redownloads_from_scratch(self):
        # .part corrupt/lớn hơn file trên server -> server trả 416 khi resume.
        # Downloader phải xoá .part và tải lại từ đầu thay vì chết vĩnh viễn.
        part_path = os.path.join(self.tmp_dir, "blob.bin.part")
        with open(part_path, "wb") as f:
            f.write(b"CORRUPT-STALE-DATA" * 1000)

        resp416 = FakeResponse(status_code=416, headers={})
        fresh = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": "6"},
            chunks=(b"FIXED!",),
        )
        session = MagicMock()
        session.head.side_effect = requests.ConnectionError("no head support")
        session.get.side_effect = [resp416, fresh]

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0,
            preferred_filename="blob.bin",
        )

        self.assertIsNotNone(saved)
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"FIXED!")
        # .part cũ phải bị xoá sau 416, và GET lần 2 KHÔNG mang Range nữa
        self.assertFalse(os.path.exists(part_path))
        self.assertEqual(session.get.call_count, 2)
        second_headers = session.get.call_args_list[1].kwargs.get("headers", {})
        self.assertIsNone(second_headers.get("Range"))

    def test_persistent_416_gives_up_cleanly_without_part_leftover(self):
        part_path = os.path.join(self.tmp_dir, "blob.bin.part")
        with open(part_path, "wb") as f:
            f.write(b"STALE" * 10)

        session = MagicMock()
        session.head.side_effect = requests.ConnectionError("no head support")
        session.get.side_effect = [
            FakeResponse(status_code=416, headers={}),
            FakeResponse(status_code=416, headers={}),  # lần sau không còn Range
        ]

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0,
            preferred_filename="blob.bin",
        )
        self.assertIsNone(saved)
        self.assertFalse(os.path.exists(part_path))

    def test_disk_full_midstream_cleans_part_file(self):
        first = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS, "Content-Length": "100"},
            chunks=(b"A" * 50, b"B" * 50),
            error_after_chunks=1,
            error=OSError("No space left on device"),
        )
        session = MagicMock()
        session.get.return_value = first

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0
        )
        self.assertIsNone(saved)
        # .part nửa chừng phải được dọn, không resume từ dữ liệu không flush
        self.assertEqual(os.listdir(self.tmp_dir), [])

    def test_save_response_stream_disk_full_cleans_tmp_file(self):
        resp = FakeResponse(
            status_code=200,
            headers={**BINARY_HEADERS},
            chunks=(b"x" * 10, b"y" * 10),
            error_after_chunks=1,
            error=OSError("No space left on device"),
        )
        saved = HttpDownloader.save_response_stream(resp, self.tmp_dir, "f.bin")
        self.assertIsNone(saved)
        # .tmp nửa chừng phải được dọn
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.tmp_dir)))


class TestWave4GDriveTokenFirst(unittest.TestCase):
    """WH3 GD-quota-fp: token TRƯỚC quota + 2 biến thể form bị bỏ sót."""

    def _run_interstitial(self, html_text):
        from ctf_downloader.downloaders.gdrive import GDriveDownloader

        interstitial = FakeResponse(
            status_code=200, headers={"Content-Type": "text/html"}, text=html_text
        )
        file_stream = FakeResponse(
            status_code=200,
            headers={
                **BINARY_HEADERS,
                "Content-Disposition": 'attachment; filename="data.bin"',
            },
            chunks=(b"GDRIVE-OK",),
        )
        session = MagicMock()
        session.get.side_effect = [interstitial, file_stream]
        resp, size = GDriveDownloader.get_download_stream(
            "https://drive.google.com/file/d/1ABCdefGHI/view", session, timeout=5
        )
        return resp, size, session

    def test_confirm_form_reversed_attribute_order(self):
        resp, _, session = self._run_interstitial(
            '<form action=""><input type="hidden" value="rvTok9" name="confirm"></form>'
        )
        self.assertIsNotNone(resp)
        second_url = session.get.call_args_list[1].args[0]
        self.assertIn("confirm=rvTok9", second_url)

    def test_confirm_form_single_quotes(self):
        resp, _, session = self._run_interstitial("<input name='confirm' value='sqTok7'>")
        self.assertIsNotNone(resp)
        second_url = session.get.call_args_list[1].args[0]
        self.assertIn("confirm=sqTok7", second_url)

    def test_quota_marker_with_valid_token_is_not_false_positive(self):
        # Trang interstitial hợp lệ chứa cụm "permission denied" trong help-text
        # nhưng vẫn có confirm token -> phải đi tiếp theo luồng token, KHÔNG
        # bị kết tội quota.
        resp, _, session = self._run_interstitial(
            "<html><body>If you see permission denied, read the help page."
            '<input type="hidden" name="confirm" value="tokFP1"></body></html>'
        )
        self.assertIsNotNone(resp)
        second_url = session.get.call_args_list[1].args[0]
        self.assertIn("confirm=tokFP1", second_url)


class TestWave4WorkspaceBuilderDefensive(unittest.TestCase):
    """WH3 WB-hints/cost/category: challenge dị dạng không được sập workspace."""

    def _build(self, **challenge_kwargs):
        import tempfile
        from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
        from ctf_downloader.models import Challenge

        kwargs = dict(id=1, name="Weird Chall", category="Web")
        kwargs.update(challenge_kwargs)
        ch = Challenge(**kwargs)
        out = tempfile.mkdtemp(prefix="wave4_ws_")
        return WorkspaceBuilder.create_challenge_workspace(out, ch, [], [], [], True)

    def tearDown(self):
        import glob
        for d in glob.glob("/tmp/wave4_ws_*"):
            shutil.rmtree(d, ignore_errors=True)

    def test_hints_as_list_of_strings_no_crash(self):
        path = self._build(hints=["plain string hint", {"content": "dict hint", "cost": 5}])
        readme = open(os.path.join(path, "challenge", "README.md"), encoding="utf-8").read()
        self.assertIn("plain string hint", readme)
        self.assertIn("dict hint", readme)

    def test_hint_with_null_cost_no_crash(self):
        path = self._build(hints=[{"content": "free hint", "cost": None}])
        readme = open(os.path.join(path, "challenge", "README.md"), encoding="utf-8").read()
        self.assertIn("free hint", readme)
        self.assertIn("Hint 1", readme)

    def test_category_none_no_crash_and_falls_back_to_default(self):
        path = self._build(category=None)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("Misc", path)  # fallback DEFAULT_CATEGORY
        self.assertTrue(os.path.exists(os.path.join(path, "solver", "solve.py")))
        self.assertTrue(os.path.exists(os.path.join(path, "writeup", "README.md")))


class TestWave4LinkExtractorSuffixMatch(unittest.TestCase):
    """WH3 prefix-match misroute: domain lookalike không được nhận nhầm."""

    LOOKALIKE_URLS = (
        "https://drive.google.com.evil.io/file/d/abc123/view",
        "https://dropbox.com.evil.pt/s/abc/chall",
        "https://mediafire.com.attacker.net/file/xyz/payload",
        "https://mega.nz.phish.io/#F!abc!def",
    )
    SERVICE_TYPES = ("gdrive", "dropbox", "mediafire", "mega")

    def test_lookalike_domains_not_misrouted_to_service_downloaders(self):
        for url in self.LOOKALIKE_URLS:
            link = LinkExtractor.classify_link(url)
            self.assertNotIn(link.link_type, self.SERVICE_TYPES, msg=url)
            self.assertFalse(link.is_downloadable, msg=url)

    def test_real_domains_and_subdomains_still_route_correctly(self):
        cases = {
            "https://www.dropbox.com/s/abc/x.zip?dl=0": "dropbox",
            "https://docs.google.com/document/d/ABC/edit": "gdrive",
            "https://www.mediafire.com/file/abc/p.rar/file": "mediafire",
            "https://mega.nz/file/abc#key": "mega",
            "https://cdn.discordapp.com/attachments/1/2/f.zip": "discord",
        }
        for url, expected in cases.items():
            link = LinkExtractor.classify_link(url)
            self.assertEqual(link.link_type, expected, msg=url)


class TestWave4MediafireLocaleAndDecoy(unittest.TestCase):
    """WH3: '1,5 MB' parse nhầm thành 15MB + chọn nhầm nút decoy hidden."""

    def test_parse_size_number_european_formats(self):
        from ctf_downloader.downloaders.mediafire import _parse_size_number

        self.assertAlmostEqual(_parse_size_number("1,5"), 1.5)
        self.assertAlmostEqual(_parse_size_number("2.50"), 2.5)
        self.assertAlmostEqual(_parse_size_number("1.234,56"), 1234.56)
        self.assertAlmostEqual(_parse_size_number("1,234"), 1234.0)
        self.assertAlmostEqual(_parse_size_number("42"), 42.0)
        self.assertIsNone(_parse_size_number(""))
        self.assertIsNone(_parse_size_number("garbage"))

    def test_expected_size_scrape_decimal_comma_not_multiplied_by_ten(self):
        session = MagicMock()
        bad_api = FakeResponse(status_code=500)
        page = FakeResponse(status_code=200, text="<div>File size: 1,5 MB</div>")

        def fake_get(url, *args, **kwargs):
            if "get_info.php" in url:
                return bad_api
            return page

        session.get.side_effect = fake_get
        size = MediafireDownloader.get_expected_size(
            "https://www.mediafire.com/file/abc123/payload.rar/file", session, timeout=5
        )
        self.assertEqual(size, int(1.5 * 1024 * 1024))

    def test_hidden_decoy_button_skipped_in_favor_of_visible_one(self):
        page = FakeResponse(
            status_code=200,
            text=(
                '<a id="downloadButton" href="http://download199.mediafire.com/decoy/path" '
                'style="display:none">Download</a>'
                '<a id="downloadButton" href="http://download199.mediafire.com/real/path">Download</a>'
            ),
        )
        stream = FakeResponse(
            status_code=200,
            headers={"Content-Disposition": 'attachment; filename="p.rar"', **BINARY_HEADERS},
        )

        def fake_get(url, *args, **kwargs):
            if "get_info.php" in url:
                return FakeResponse(status_code=500)
            if "/real/path" in url:
                return stream
            return page

        session = MagicMock()
        session.get.side_effect = fake_get
        resp, expected = MediafireDownloader.get_download_stream(
            "https://www.mediafire.com/file/abc123/p.rar/file", session, timeout=5
        )
        self.assertIs(resp, stream)

    def test_all_buttons_hidden_falls_back_without_direct_link(self):
        # Nút duy nhất bị ẩn và không có direct link nào khác trong trang
        page = FakeResponse(
            status_code=200,
            text='<a id="downloadButton" href="https://decoy.example.com/x" hidden>D</a>',
        )

        def fake_get(url, *args, **kwargs):
            if "get_info.php" in url:
                return FakeResponse(status_code=500)
            return page

        session = MagicMock()
        session.get.side_effect = fake_get
        resp, expected = MediafireDownloader.get_download_stream(
            "https://www.mediafire.com/file/abc123/p.rar/file", session, timeout=5
        )
        self.assertIsNone(resp)



class TestBuilderMetadataJsonGuard(unittest.TestCase):
    """Deferred-minor: raw_data không serializable (vd chứa set()) không được
    làm crash create_challenge_workspace -> fallback default=str kèm warning."""

    def tearDown(self):
        import glob
        for d in glob.glob("/tmp/meta_guard_ws_*"):
            shutil.rmtree(d, ignore_errors=True)

    def test_raw_data_with_set_no_crash_and_metadata_written(self):
        import tempfile
        from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
        from ctf_downloader.models import Challenge

        ch = Challenge(
            id=9, name="Meta Guard", category="Web",
            raw_data={"weird": {1, 2}, "ok": "fine"},
        )
        out = tempfile.mkdtemp(prefix="meta_guard_ws_")
        path = WorkspaceBuilder.create_challenge_workspace(out, ch, [], [], [], True)
        meta_path = os.path.join(path, "metadata.json")
        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)  # phải là JSON hợp lệ
        self.assertEqual(meta["name"], "Meta Guard")
        self.assertIn("weird", meta["raw"])


class TestWave4Resume416ContentRange(unittest.TestCase):
    """416 kèm Content-Range */<total> khi .part đã đủ total bytes ->
    rename .part thành file cuối NGAY, không GET lại (file đã hoàn chỉnh)."""

    def setUp(self):
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sp2_tmp_416cr")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_416_content_range_total_matches_part_renames_without_get(self):
        part_path = os.path.join(self.tmp_dir, "full.bin.part")
        body = b"A" * 1000
        with open(part_path, "wb") as f:
            f.write(body)

        resp416 = FakeResponse(status_code=416, headers={"Content-Range": "bytes */1000"})
        session = MagicMock()
        session.head.side_effect = requests.ConnectionError("no head support")
        session.get.return_value = resp416  # nếu có GET lần nào nữa là fail

        saved = HttpDownloader.download_file(
            "https://host.com/full.bin", self.tmp_dir, session, max_size=0,
            preferred_filename="full.bin",
        )

        self.assertIsNotNone(saved)
        target = os.path.join(self.tmp_dir, "full.bin")
        self.assertTrue(os.path.exists(target))
        self.assertFalse(os.path.exists(part_path))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), body)
        # KHÔNG được tải lại: đúng 1 request duy nhất
        self.assertEqual(session.get.call_count, 1)

    def test_416_without_content_range_keeps_reset_behavior(self):
        part_path = os.path.join(self.tmp_dir, "blob.bin.part")
        with open(part_path, "wb") as f:
            f.write(b"STALE" * 10)

        session = MagicMock()
        session.head.side_effect = requests.ConnectionError("no head support")
        session.get.side_effect = [
            FakeResponse(status_code=416, headers={}),
            FakeResponse(status_code=200, headers=BINARY_HEADERS, chunks=(b"OK",)),
        ]

        saved = HttpDownloader.download_file(
            "https://host.com/blob.bin", self.tmp_dir, session, max_size=0,
            preferred_filename="blob.bin",
        )
        self.assertIsNotNone(saved)
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
