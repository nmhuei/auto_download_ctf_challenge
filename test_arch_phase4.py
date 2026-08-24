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


if __name__ == "__main__":
    unittest.main(verbosity=2)
