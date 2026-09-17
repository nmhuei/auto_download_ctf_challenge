import os
import tempfile
import unittest
from unittest.mock import patch

from ctf_downloader.utils.sanitize import sanitize_cookie_input
from ctf_downloader.utils.http_client import parse_cookie_string
from ctf_downloader.config import DownloaderConfig
from ctf_downloader.services.auth_service import AuthService
from ctf_downloader.storage.global_config import resolve_workspace_root


class TestCookieSanitizationAndWorkspace(unittest.TestCase):
    def test_sanitize_cookie_input_prefixes(self):
        # Header prefix with spaces
        raw = "Cookie: session=12345; user=admin"
        self.assertEqual(sanitize_cookie_input(raw), "session=12345; user=admin")

        raw_lower = "cookie: session=12345"
        self.assertEqual(sanitize_cookie_input(raw_lower), "session=12345")

        raw_space = "Cookie :  token=xyz"
        self.assertEqual(sanitize_cookie_input(raw_space), "token=xyz")

    def test_sanitize_cookie_input_curl_flags(self):
        raw_h = '-H "Cookie: a=1; b=2"'
        self.assertEqual(sanitize_cookie_input(raw_h), "a=1; b=2")

        raw_header = "--header 'cookie: a=1; b=2'"
        self.assertEqual(sanitize_cookie_input(raw_header), "a=1; b=2")

        raw_b = '-b "session=abc"'
        self.assertEqual(sanitize_cookie_input(raw_b), "session=abc")

        raw_cookie_flag = '--cookie "session=abc"'
        self.assertEqual(sanitize_cookie_input(raw_cookie_flag), "session=abc")

    def test_sanitize_cookie_input_multiline(self):
        devtools_paste = (
            "GET /challenges HTTP/1.1\r\n"
            "Host: asisctf.com\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "Cookie: XSRF-TOKEN=tok1; asis_ctf_quals_2026_session=tok2\r\n"
            "Connection: close\r\n"
        )
        self.assertEqual(
            sanitize_cookie_input(devtools_paste),
            "XSRF-TOKEN=tok1; asis_ctf_quals_2026_session=tok2"
        )

    def test_sanitize_cookie_input_quotes_and_none(self):
        self.assertIsNone(sanitize_cookie_input(None))
        self.assertEqual(sanitize_cookie_input(""), "")
        self.assertEqual(sanitize_cookie_input('"session=xyz"'), "session=xyz")
        self.assertEqual(sanitize_cookie_input("'session=xyz'"), "session=xyz")

    def test_parse_cookie_string_with_cookie_prefix(self):
        raw = "Cookie: XSRF-TOKEN=token_val; asis_ctf_quals_2026_session=session_val"
        parsed = parse_cookie_string(raw)
        self.assertIn("XSRF-TOKEN", parsed)
        self.assertIn("asis_ctf_quals_2026_session", parsed)
        self.assertNotIn("Cookie: XSRF-TOKEN", parsed)
        self.assertEqual(parsed["XSRF-TOKEN"], "token_val")
        self.assertEqual(parsed["asis_ctf_quals_2026_session"], "session_val")

    def test_downloader_config_anchors_relative_paths(self):
        root = resolve_workspace_root()

        # Relative folder name
        cfg1 = DownloaderConfig(url="https://asisctf.com", output_dir="ASIS_CTF_2026")
        cfg1.validate()
        self.assertEqual(cfg1.output_dir, os.path.join(root, "ASIS_CTF_2026"))

        # Relative ./ folder name
        cfg2 = DownloaderConfig(url="https://asisctf.com", output_dir="./ASIS_CTF_2026")
        cfg2.validate()
        self.assertEqual(cfg2.output_dir, os.path.join(root, "ASIS_CTF_2026"))

        # Absolute paths are preserved (e.g. unit test tmp paths)
        tmp_dir = tempfile.gettempdir()
        custom_abs = os.path.join(tmp_dir, "custom_test_ctf")
        cfg3 = DownloaderConfig(url="https://asisctf.com", output_dir=custom_abs)
        cfg3.validate()
        self.assertEqual(cfg3.output_dir, custom_abs)

    def test_downloader_config_sanitizes_cookie(self):
        cfg = DownloaderConfig(
            url="https://asisctf.com",
            cookie="Cookie: foo=bar; baz=qux"
        )
        cfg.validate()
        self.assertEqual(cfg.cookie, "foo=bar; baz=qux")

    def test_auth_service_save_and_resolve(self):
        tmp_ws = tempfile.mkdtemp(prefix="test_ctf_ws_")
        test_url = "https://testctf-auth-save.example.com"
        raw_cookie = "Cookie: session=secret123; user=hacker"

        ok = AuthService.save_auth(
            workspace=tmp_ws,
            url=test_url,
            cookie=raw_cookie
        )
        self.assertTrue(ok)

        # Resolve by workspace
        c_ws, _ = AuthService.resolve(tmp_ws)
        self.assertEqual(c_ws, "session=secret123; user=hacker")

        # Resolve by URL
        c_url, _ = AuthService.resolve(test_url)
        self.assertEqual(c_url, "session=secret123; user=hacker")


if __name__ == "__main__":
    unittest.main()
