"""
Phase 4 — Unit test cho platform registry + decorator.

Task 6: registry, decorator 5 platform, PLATFORM_TYPES sinh tự.
Task 7: detection registry-driven, urlnorm, PlatformResolver.

Toàn bộ HTTP được giả lập — KHÔNG có request mạng nào.
Chạy: python3 -m unittest test_arch_phase4.py -v
"""

import inspect
import unittest

from ctf_downloader.platforms import capabilities
from ctf_downloader.platforms.registry import (
    PLATFORMS,
    PlatformSpec,
    UnknownPlatformError,
    get_spec,
    register,
)
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.platforms.custom_rest import CustomRESTPlatform
from ctf_downloader.platforms.generic_html import GenericHTMLPlatform


REAL_KEYS = {"gzctf", "ctfd", "rctf", "custom_rest", "generic_html"}


class TestRegistryBasics(unittest.TestCase):
    def test_five_real_platforms_registered(self):
        self.assertTrue(REAL_KEYS.issubset(PLATFORMS))

    def test_spec_fields(self):
        spec = get_spec("gzctf")
        self.assertIsInstance(spec, PlatformSpec)
        self.assertIs(spec.cls, GZCTFPlatform)
        self.assertEqual(spec.label, "GZ::CTF")

    def test_cls_spec_wired_by_decorator(self):
        for key in REAL_KEYS:
            spec = get_spec(key)
            self.assertIs(getattr(spec.cls, "spec", None), spec,
                          f"{key}.spec phải trùng spec trong registry")

    def test_get_spec_unknown_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_spec("no_such_platform")
        self.assertIsInstance(ctx.exception, UnknownPlatformError)

    def test_register_decorator_fake_platform(self):
        @register("zz_test_fake", label="Fake CTF", throttle=9.0)
        class FakePlatform(GenericHTMLPlatform):
            pass

        try:
            self.assertIn("zz_test_fake", PLATFORMS)
            spec = get_spec("zz_test_fake")
            self.assertIs(spec.cls, FakePlatform)
            self.assertEqual(spec.throttle, 9.0)
            self.assertIs(FakePlatform.spec, spec)
        finally:
            del PLATFORMS["zz_test_fake"]
        self.assertNotIn("zz_test_fake", PLATFORMS)

    def test_throttle_values_match_submitter(self):
        # Giá trị copy từ THROTTLE_BY_PLATFORM hiện tại của submitter.py
        self.assertEqual(get_spec("gzctf").throttle, 2.0)
        self.assertEqual(get_spec("ctfd").throttle, 6.0)
        self.assertEqual(get_spec("rctf").throttle, 5.0)

    def test_detection_metadata_present(self):
        gz = get_spec("gzctf")
        self.assertIn("GZCTF_Token", gz.cookie_hints)
        self.assertTrue(any("gz" in m.lower() for m in gz.html_markers))
        self.assertTrue(gz.supports_container and gz.supports_scoreboard)
        self.assertTrue(gz.rules_via_api)

        ctfd = get_spec("ctfd")
        self.assertTrue(ctfd.supports_scoreboard)
        self.assertFalse(ctfd.supports_container)  # whale là động, do probe quyết định

        rctf = get_spec("rctf")
        self.assertTrue(rctf.supports_scoreboard)
        self.assertFalse(rctf.supports_container)

        self.assertEqual(get_spec("generic_html").supports_scoreboard, False)

    def test_probes_are_module_level_callables(self):
        for key in ("gzctf", "ctfd", "rctf"):
            probes = get_spec(key).probes
            self.assertTrue(len(probes) > 0, f"{key} phải có ít nhất 1 probe")
            for p in probes:
                self.assertTrue(callable(p))
                params = list(inspect.signature(p).parameters)
                self.assertEqual(params, ["origin", "session", "info", "done"],
                                 f"probe của {key} phải nhận (origin, session, info, done)")


class TestPlatformTypes(unittest.TestCase):
    def test_unknown_first_then_sorted_keys(self):
        self.assertEqual(capabilities.PLATFORM_TYPES,
                         ("unknown", *sorted(PLATFORMS)))

    def test_contains_all_real_keys(self):
        self.assertTrue(REAL_KEYS.issubset(set(capabilities.PLATFORM_TYPES)))

    def test_not_hardcoded_in_capabilities_source(self):
        source = inspect.getsource(capabilities)
        self.assertNotIn('"gzctf"', source)
        self.assertNotIn("'custom_rest'", source)


# --------------------------------------------------------------------- #
# Task 7 — urlnorm + detection registry-driven + PlatformResolver
# --------------------------------------------------------------------- #
import urllib.parse

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.utils.urlnorm import normalize_base_url


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("không phải JSON")
        return self._json


class RoutingSession:
    """Session giả lập định tuyến theo path, mặc định 404."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        path = urllib.parse.urlparse(url).path or "/"
        resp = self.routes.get(path, self.routes.get("*"))
        if resp is None:
            return FakeResponse(status_code=404, text="Not Found")
        return resp(url) if callable(resp) else resp


GZ_CONFIG_JSON = {
    "Title": "Example CTF", "Slogan": "x", "Rules": "# rules",
    "PortMapping": "", "DefaultLifetime": 1800,
}


class TestUrlNorm(unittest.TestCase):
    SUFFIXES = ["/challenges", "/scoreboard", "/login", "/register",
                "/users", "/teams", "/rules", "/notifications"]

    def test_strips_all_8_suffixes(self):
        for suffix in self.SUFFIXES:
            self.assertEqual(
                normalize_base_url(f"https://ctf.example.com{suffix}"),
                "https://ctf.example.com", msg=suffix)

    def test_strips_suffix_keeps_deeper_path(self):
        self.assertEqual(
            normalize_base_url("https://ctf.example.com/games/6/challenges"),
            "https://ctf.example.com/games/6")

    def test_fragment_and_trailing_slash(self):
        self.assertEqual(
            normalize_base_url("https://ctf.example.com/scoreboard/#top"),
            "https://ctf.example.com")

    def test_plain_url_untouched(self):
        self.assertEqual(
            normalize_base_url("https://ctf.example.com"),
            "https://ctf.example.com")


class TestConfigValidateUsesUrlNorm(unittest.TestCase):
    def test_validate_strips_notifications_suffix(self):
        cfg = DownloaderConfig(url="https://ctf.example.com/notifications")
        cfg.validate()
        self.assertEqual(cfg.url, "https://ctf.example.com")

    def test_validate_token_extraction_preserved(self):
        cfg = DownloaderConfig(url="https://ctf.example.com/challenges?token=TOK")
        cfg.validate()
        self.assertEqual(cfg.token, "TOK")
        self.assertEqual(cfg.url, "https://ctf.example.com")


class FakeWorkspaceRepo:
    """Repo giả lập đúng interface WorkspaceRepo mà PlatformResolver cần."""

    def __init__(self, ctf_info, url):
        self._data = {"ctf_info": ctf_info}
        self._url = url

    def read_challenges(self):
        return dict(self._data)

    def resolve_platform_url(self):
        return self._url


class TestPlatformResolver(unittest.TestCase):
    def _patch_session(self, session):
        from unittest.mock import patch
        return patch("ctf_downloader.services.platform_resolver.create_session",
                     return_value=session)

    def test_resolves_declared_gzctf_without_network(self):
        from ctf_downloader.services.platform_resolver import PlatformResolver
        from ctf_downloader.platforms.gzctf import GZCTFPlatform

        session = RoutingSession()  # không route nào — mọi request đều là bug
        repo = FakeWorkspaceRepo(
            {"platform": "gzctf", "url": "https://gz.example.com", "game_id": 7},
            "https://gz.example.com")
        with self._patch_session(session):
            sess, platform, info = PlatformResolver.for_workspace(repo)

        self.assertIs(sess, session)
        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(platform.game_id, 7)
        self.assertEqual(info.game_id, 7)
        self.assertEqual(info.platform_type, "gzctf")
        self.assertEqual(session.calls, [])  # KHÔNG gọi mạng khi đã khai báo

    def test_falls_back_to_detection_when_unknown(self):
        from ctf_downloader.services.platform_resolver import PlatformResolver
        from ctf_downloader.platforms.generic_html import GenericHTMLPlatform

        session = RoutingSession()  # mọi probe -> 404
        repo = FakeWorkspaceRepo({"platform": "generic"}, "https://mystery.example.com")
        with self._patch_session(session):
            _, platform, info = PlatformResolver.for_workspace(repo)

        self.assertIsInstance(platform, GenericHTMLPlatform)
        self.assertEqual(info.platform_type, "generic_html")
        self.assertTrue(len(session.calls) > 0)  # đã chạy pipeline recon

    def test_missing_url_raises_value_error(self):
        from ctf_downloader.services.platform_resolver import PlatformResolver

        class EmptyRepo(FakeWorkspaceRepo):
            def resolve_platform_url(self):
                return None

        with self.assertRaises(ValueError):
            PlatformResolver.for_workspace(EmptyRepo({}, None))


class TestDetectionRegistryDriven(unittest.TestCase):
    def test_markers_sourced_from_registry(self):
        """Marker tầng 1 phải đọc từ registry: thêm marker vào spec -> dò được."""
        from ctf_downloader.platforms import detection

        session = RoutingSession(routes={
            "*": FakeResponse(text="<html>totally custom marker XYZQUIX</html>")})
        spec = PLATFORMS["gzctf"]
        original_markers = spec.html_markers
        try:
            object.__setattr__(spec, "html_markers",
                               original_markers + ("XYZQUIX",))
            platform, info = detection.detect_platform_info(
                "https://mystery.example.com/", session)
            self.assertEqual(info.platform_type, "gzctf")
            self.assertEqual(info.confidence, "high")
        finally:
            object.__setattr__(spec, "html_markers", original_markers)


# --------------------------------------------------------------------- #
# Task 12 — Fixture DoD: "Thêm platform mới = 1 file"
# --------------------------------------------------------------------- #
class TestOneFilePlatformFixture(unittest.TestCase):
    """Chứng minh: để thêm 1 platform mới, chỉ cần ĐÚNG 1 module chứa
    ``@register(...)`` ngay tại định nghĩa class — không phải sửa registry,
    capabilities hay bất kỳ danh sách hardcode nào.

    Module test này chính là "1 file" đó. Sau khi decorator chạy:
      - registry biết key (get_spec / spec.cls / throttle),
      - capabilities.PLATFORM_TYPES tự sinh ra key,
      - PlatformResolver dựng adapter khi workspace khai báo platform,
      - pipeline detection nhận diện qua marker đọc từ spec (chỉ cần key
        xuất hiện trong tuple ưu tiên tầng 1 `_MARKER_PRIORITY` của
        detection — chính sách thứ tự, không phải dữ liệu nhận diện).
    """

    FAKE_KEY = "zz_one_file_fixture"

    @classmethod
    def setUpClass(cls):
        key = cls.FAKE_KEY

        @register(key, label="One-File Fixture", throttle=7.5,
                  html_markers=("ONEFILEFIXTURE2026",), supports_container=True)
        class OneFilePlatform(GenericHTMLPlatform):
            # Một platform file thật tự khai báo platform_type của mình
            # (như GZCTFPlatform gán "gzctf") để detection phản chiếu đúng.
            def __init__(self, base_url, session):
                super().__init__(base_url, session)
                self.ctf_info.platform_type = key

        cls.platform_cls = OneFilePlatform

    @classmethod
    def tearDownClass(cls):
        del PLATFORMS[cls.FAKE_KEY]
        # Reload lại capabilities sau khi xoá key -> snapshot trở về 5 platform thật
        import importlib
        importlib.reload(capabilities)

    def test_registry_knows_new_key(self):
        spec = get_spec(self.FAKE_KEY)
        self.assertIs(spec.cls, self.platform_cls)
        self.assertIs(self.platform_cls.spec, spec)
        self.assertEqual(spec.throttle, 7.5)
        self.assertEqual(spec.label, "One-File Fixture")

    def test_capabilities_auto_includes_new_key(self):
        """PLATFORM_TYPES sinh tự từ registry LÚC IMPORT (không hardcode):
        đăng ký xong, chỉ cần reload capabilities là key mới xuất hiện."""
        import importlib

        # Snapshot cũ (trước khi module platform này được import thật) chưa có key
        self.assertNotIn(self.FAKE_KEY, set(capabilities.PLATFORM_TYPES))

        importlib.reload(capabilities)
        self.assertIn(self.FAKE_KEY, set(capabilities.PLATFORM_TYPES))
        # Khôi phục trong tearDownClass (sau khi xoá key khỏi PLATFORMS)

    def test_resolver_builds_declared_platform_without_network(self):
        """Workspace khai báo `ctf_info.platform` = key mới -> adapter được
        dựng thẳng từ registry, KHÔNG gọi mạng (session không route nào)."""
        from unittest.mock import patch as _patch

        from ctf_downloader.services.platform_resolver import PlatformResolver

        repo = FakeWorkspaceRepo(
            {"platform": self.FAKE_KEY}, "https://onefile.example.com")
        with _patch("ctf_downloader.services.platform_resolver.create_session",
                    return_value=RoutingSession()):
            _, platform, info = PlatformResolver.for_workspace(repo)

        self.assertIsInstance(platform, self.platform_cls)
        self.assertEqual(info.platform_type, self.FAKE_KEY)

    def test_detection_tier1_matches_marker_from_spec(self):
        """Marker tầng 1 đọc từ spec vừa đăng ký — chỉ cần key có trong tuple
        ưu tiên là detection nhận diện, KHÔNG sửa thêm dữ liệu nào."""
        from unittest.mock import patch as _patch

        from ctf_downloader.platforms import detection

        session = RoutingSession(routes={
            "*": FakeResponse(text="<body>welcome ONEFILEFIXTURE2026</body>")})
        with _patch.object(detection, "_MARKER_PRIORITY",
                           (self.FAKE_KEY,) + detection._MARKER_PRIORITY):
            platform, info = detection.detect_platform_info(
                "https://onefile.example.com/", session)

        self.assertIsInstance(platform, self.platform_cls)
        self.assertEqual(info.platform_type, self.FAKE_KEY)
        self.assertEqual(info.confidence, "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
