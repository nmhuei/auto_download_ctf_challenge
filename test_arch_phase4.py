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


if __name__ == "__main__":
    unittest.main(verbosity=2)
