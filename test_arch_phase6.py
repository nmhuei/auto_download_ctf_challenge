"""Phase 6 — downloader registry + dispatch.

Kiểm tra:
- registry.DOWNLOADERS đăng ký đúng 5 handler thật (gdrive/dropbox/mediafire/mega/direct_file).
- DownloadManager.download_url() tra bảng DOWNLOADERS theo link_type;
  link_type lạ rơi về default HttpDownloader.
- Cắt lazy-import extractor→downloader: LinkExtractor không còn import module
  ctf_downloader.downloaders.*, nhưng vẫn đánh dấu mega downloadable theo megatools.
"""
import os
import shutil
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.extractors.link_extractor import LinkExtractor


class FakeStreamHandler:
    """Handler giả trả stream như GDrive/Dropbox/Mediafire."""

    calls = []

    @staticmethod
    def get_download_stream(url, session=None, timeout=30):
        FakeStreamHandler.calls.append(url)
        return None, None  # không lấy được stream -> nhánh lỗi handler


class FakeSaveHandler:
    """Handler giả trả stream hợp lệ để đi tới save_response_stream."""

    @staticmethod
    def get_download_stream(url, session=None, timeout=30):
        return object(), 1024


class TestRegistryTable(unittest.TestCase):
    def test_registry_module_exports(self):
        from ctf_downloader.downloaders import registry
        self.assertTrue(hasattr(registry, "DOWNLOADERS"))
        self.assertTrue(callable(registry.register_downloader))

    def test_five_real_handlers_registered(self):
        import ctf_downloader.downloaders.manager  # noqa: F401 — trigger đăng ký
        from ctf_downloader.downloaders.registry import DOWNLOADERS
        from ctf_downloader.downloaders.http_downloader import HttpDownloader
        from ctf_downloader.downloaders.gdrive import GDriveDownloader
        from ctf_downloader.downloaders.dropbox import DropboxDownloader
        from ctf_downloader.downloaders.mediafire import MediafireDownloader
        from ctf_downloader.downloaders.mega import MegaDownloader

        self.assertIs(DOWNLOADERS["gdrive"], GDriveDownloader)
        self.assertIs(DOWNLOADERS["dropbox"], DropboxDownloader)
        self.assertIs(DOWNLOADERS["mediafire"], MediafireDownloader)
        self.assertIs(DOWNLOADERS["mega"], MegaDownloader)
        self.assertIs(DOWNLOADERS["direct_file"], HttpDownloader)

    def test_decorator_registers_and_attaches_metadata(self):
        from ctf_downloader.downloaders.registry import register_downloader

        @register_downloader("fake_svc", domains=("fake.example",), extensions=(".fk",))
        class FakeSvc:
            pass

        try:
            from ctf_downloader.downloaders.registry import DOWNLOADERS
            self.assertIs(DOWNLOADERS["fake_svc"], FakeSvc)
            self.assertEqual(FakeSvc.domains, ("fake.example",))
            self.assertEqual(FakeSvc.extensions, (".fk",))
        finally:
            from ctf_downloader.downloaders.registry import DOWNLOADERS
            DOWNLOADERS.pop("fake_svc", None)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        import ctf_downloader.downloaders.manager as mgr_mod
        self.mgr_mod = mgr_mod
        self._saved = dict(mgr_mod.DOWNLOADERS)
        self.tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_arch_p6_tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    def tearDown(self):
        self.mgr_mod.DOWNLOADERS.clear()
        self.mgr_mod.DOWNLOADERS.update(self._saved)
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def _mgr(self):
        return self.mgr_mod.DownloadManager(session=MagicMock(), timeout=5, size_limit_bytes=0)

    def test_unknown_link_type_routes_to_stream_handler_from_table(self):
        self.mgr_mod.DOWNLOADERS["fakecloud"] = FakeStreamHandler
        ok, path, msg = self._mgr().download_url(
            "https://fake.example/a.bin", self.tmp_dir, link_type="fakecloud"
        )
        self.assertEqual(FakeStreamHandler.calls[-1], "https://fake.example/a.bin")
        self.assertFalse(ok)
        self.assertIsNone(path)
        self.assertIn("fakecloud", msg)

    def test_stream_handler_success_goes_through_save_response_stream(self):
        self.mgr_mod.DOWNLOADERS["fakesave"] = FakeSaveHandler
        with patch.object(
            self.mgr_mod.HttpDownloader, "save_response_stream",
            return_value=os.path.join(self.tmp_dir, "out.bin"),
        ) as mock_save:
            ok, path, msg = self._mgr().download_url(
                "https://fake.example/b.bin", self.tmp_dir, link_type="fakesave",
                preferred_name="out.bin",
            )
        self.assertTrue(ok, msg)
        self.assertEqual(path, os.path.join(self.tmp_dir, "out.bin"))
        mock_save.assert_called_once()

    def test_unknown_link_type_defaults_to_http_downloader(self):
        with patch.object(self.mgr_mod.HttpDownloader, "probe_content_length", return_value=None) as mock_probe, \
             patch.object(
                 self.mgr_mod.HttpDownloader, "download_file",
                 return_value=os.path.join(self.tmp_dir, "c.bin"),
             ) as mock_dl:
            ok, path, msg = self._mgr().download_url(
                "https://example.com/c.bin", self.tmp_dir, link_type="tot_nhat_dau_tien"
            )
        mock_probe.assert_called_once()
        mock_dl.assert_called_once()
        self.assertTrue(ok, msg)

    def test_mega_routes_to_megatools_without_tool(self):
        with patch("ctf_downloader.downloaders.mega.shutil.which", return_value=None):
            ok, path, msg = self._mgr().download_url(
                "https://mega.nz/file/xyz", self.tmp_dir, link_type="mega"
            )
        self.assertFalse(ok)
        self.assertIsNone(path)
        self.assertIn("megatools", msg)
        self.assertIn("megadl", msg)

    def test_no_extractors_import_downloaders_layer(self):
        # R4: extractors không import downloaders (kể cả lazy trong hàm)
        import ast
        import ctf_downloader.extractors.link_extractor as le
        tree = ast.parse(open(le.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                self.assertNotIn("downloaders", mod.split("."))
                self.assertFalse(any("downloaders" in n.split(".") for n in names))


if __name__ == "__main__":
    unittest.main()
