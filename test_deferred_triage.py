"""DEFERRED_TRIAGE (§2 OPEN-CODE) — batch test cho các mục sửa trực tiếp.

Nguồn: ~/Downloads/ctf_toolkit_artifacts/DEFERRED_TRIAGE.md, nhánh
rebuild/architecture. Các mục trong file này:

  #3 [S]   rctf.py — ``last_verdict``/``_last_verdict`` dùng Literal
           ``Verdict`` (models) thay vì ``str`` tự do.
  #4 [S-M] detection.py — cookie-hint chuỗi ``-c`` được parse thành TÊN
           cookie (cặp name=value tách theo ';' và newline), hết substring
           nguyên blob; không parse được cặp nào -> fallback hành vi cũ.
  #5 [S]   registry throttle: khóa default 5.0 của custom_rest/generic_html
           khớp DEFAULT_THROTTLE submit_service (pin literal đã có sẵn ở
           test_arch_phase5.py::TestRegistryThrottlePins — phần này khóa
           thêm nguồn mặc định để hai bên không drift).

Chạy: python3 -m pytest test_deferred_triage.py -q
Mọi HTTP được giả lập — KHÔNG request mạng.
"""
import unittest
import urllib.parse
from dataclasses import fields as dataclass_fields

from requests.cookies import RequestsCookieJar

from ctf_downloader.models import Verdict
from ctf_downloader.platforms.detector import PlatformDetector
from ctf_downloader.platforms.generic_html import GenericHTMLPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform


# --------------------------------------------------------------------- #
# Giả lập HTTP tối giản (mẫu theo test_sp3_recon.py)
# --------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("Response không phải JSON")
        return self._json


class _RoutedSession:
    """GET định tuyến theo path, mặc định 404 — đủ cho pipeline offline."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.cookies = RequestsCookieJar()

    def get(self, url, **kwargs):
        path = urllib.parse.urlparse(url).path or "/"
        resp = self.routes.get(path, self.routes.get("*"))
        if resp is None:
            return _FakeResponse(status_code=404, text="Not Found")
        return resp() if callable(resp) else resp

    def post(self, url, **kwargs):
        return _FakeResponse(status_code=404, text="")


# HTML trung tính: không chứa marker nhận diện của platform nào.
_PLAIN_HTML = ("<!DOCTYPE html><html><head><title>Some CTF</title></head>"
               "<body>round-based</body></html>")


def _gz_config():
    """ClientConfig tối giản của GZCTF (GET /api/config)."""
    return {
        "Title": "Example CTF",
        "Slogan": "Break, Learn, Secure",
        "Description": "",
        "Rules": "# Luật lệ",
        "PortMapping": "",
        "DefaultLifetime": 1800,
        "FlagPattern": "flag{.*}",
        "ApiPublicKey": None,
    }


def _detect(cookie_hint=None, routes=None):
    session = _RoutedSession(routes=routes or {})
    return PlatformDetector.detect_platform_info(
        "https://x.example.com/", session, cookie_hint=cookie_hint)


# --------------------------------------------------------------------- #
# OPEN-CODE #3 — rctf last_verdict: Literal Verdict thay vì str
# --------------------------------------------------------------------- #
class TestRctfVerdictType(unittest.TestCase):
    def test_annotations_are_verdict_literal(self):
        ann_cls = RCTFPlatform.__annotations__["_last_verdict"]
        self.assertIs(ann_cls, Verdict)
        prop = RCTFPlatform.last_verdict
        self.assertIs(prop.fget.__annotations__["return"], Verdict)
        self.assertIs(prop.fset.__annotations__["value"], Verdict)

    def test_default_and_roundtrip_over_all_literals(self):
        platform = RCTFPlatform("https://rctf.example.com/", _RoutedSession())
        self.assertEqual(platform.last_verdict, "unknown")     # default
        for verdict in ("correct", "incorrect", "unknown", "ratelimited"):
            platform.last_verdict = verdict
            self.assertEqual(platform.last_verdict, verdict)


# --------------------------------------------------------------------- #
# OPEN-CODE #4 — cookie-hint: parse TÊN thay vì substring cả blob
# --------------------------------------------------------------------- #
class TestParseCookieHintNames(unittest.TestCase):
    def setUp(self):
        from ctf_downloader.platforms.detection import \
            _parse_cookie_hint_names as parse
        self.parse = parse

    def test_pairs_semicolon_separated(self):
        self.assertEqual(self.parse("GZCTF_Token=abc123; other=x"),
                         {"gzctf_token", "other"})

    def test_pairs_newline_and_cr_separated(self):
        self.assertEqual(self.parse("a=1\nb=2\r\nc=3"), {"a", "b", "c"})

    def test_quotes_stripped_from_name(self):
        self.assertEqual(self.parse("'GZCTF_Token'=v; \"k\"=w"),
                         {"gzctf_token", "k"})

    def test_empty_name_chunk_skipped(self):
        self.assertEqual(self.parse("=novalue; a=b"), {"a"})

    def test_no_pair_returns_none_for_fallback(self):
        self.assertIsNone(self.parse("just-a-name"))
        self.assertIsNone(self.parse(""))
        self.assertIsNone(self.parse("   \n  "))


class TestCookieHintDetectionTier2(unittest.TestCase):
    def _signals_text(self, info):
        return " ".join(info.signals)

    def test_name_inside_other_cookies_value_no_longer_false_matches_gzctf(self):
        # "GZCTF_Token" chỉ xuất hiện GIỮA giá trị của cookie 'foo' —
        # substring cả blob (hành vi cũ) từng nhận diện sai GZ::CTF.
        platform, info = _detect(cookie_hint="foo=xxGZCTF_Tokenyy; bar=1")
        self.assertIsInstance(platform, GenericHTMLPlatform)
        self.assertNotIn("GZ::CTF", self._signals_text(info))
        self.assertNotIn("GZCTF_Token", self._signals_text(info))

    def test_ctfd_session_prefix_cookie_no_longer_false_matches(self):
        # hint ctfd là 'session' — cookie 'sessionless' phải bị loại.
        platform, info = _detect(cookie_hint="sessionless=abc; other=1")
        self.assertIsInstance(platform, GenericHTMLPlatform)
        self.assertNotIn("Flask", self._signals_text(info))

    def test_real_pair_still_detects_gzctf(self):
        routes = {"/api/config": _FakeResponse(json_data=_gz_config())}
        platform, _info = _detect(cookie_hint="GZCTF_Token=abc123; other=x",
                                  routes=routes)
        self.assertIsInstance(platform, GZCTFPlatform)

    def test_newline_separated_pairs_detected(self):
        routes = {"/api/config": _FakeResponse(json_data=_gz_config())}
        platform, _info = _detect(cookie_hint="a=1\nGZCTF_Token=xyz",
                                  routes=routes)
        self.assertIsInstance(platform, GZCTFPlatform)

    def test_bare_token_without_any_pair_falls_back_to_substring(self):
        # Không parse được cặp name=value nào -> giữ hành vi cũ.
        routes = {"/api/config": _FakeResponse(json_data=_gz_config())}
        platform, _info = _detect(cookie_hint="GZCTF_Token", routes=routes)
        self.assertIsInstance(platform, GZCTFPlatform)

    def test_case_insensitive_name_match(self):
        routes = {"/api/config": _FakeResponse(json_data=_gz_config())}
        platform, _info = _detect(cookie_hint="gzctf_token=abc", routes=routes)
        self.assertIsInstance(platform, GZCTFPlatform)


# --------------------------------------------------------------------- #
# OPEN-CODE #5 — registry default throttle = 5.0 (custom_rest/generic_html)
# --------------------------------------------------------------------- #
class TestRegistryDefaultThrottlePin(unittest.TestCase):
    def test_platformspec_default_matches_submit_service_default(self):
        from ctf_downloader.platforms.registry import PlatformSpec, get_spec
        from ctf_downloader.services.submit_service import DEFAULT_THROTTLE

        spec_default = next(f.default for f in dataclass_fields(PlatformSpec)
                            if f.name == "throttle")
        self.assertEqual(spec_default, 5.0)
        self.assertEqual(DEFAULT_THROTTLE, 5.0)
        # custom_rest & generic_html đăng ký đúng mức default của service.
        self.assertEqual(get_spec("custom_rest").throttle, DEFAULT_THROTTLE)
        self.assertEqual(get_spec("generic_html").throttle, DEFAULT_THROTTLE)

    def test_every_registered_throttle_is_positive_seconds(self):
        from ctf_downloader.platforms.registry import PLATFORMS
        for key, spec in PLATFORMS.items():
            self.assertIsInstance(spec.throttle, float, key)
            self.assertGreater(spec.throttle, 0.0, key)


if __name__ == "__main__":
    unittest.main()
