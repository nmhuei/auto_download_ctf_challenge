"""
SP2 — Large-file consent + hạ tầng download.
Chạy: python3 -m unittest test_sp2_download.py -v
Toàn bộ test dùng mock, KHÔNG gọi mạng tới server thật.
"""
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


if __name__ == "__main__":
    unittest.main()
