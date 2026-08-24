"""
SP3 — Recon & capability map: unit test cho detector pipeline 4 tầng + PlatformInfo.

Toàn bộ HTTP được giả lập qua MockSession (định tuyến theo path, mặc định 404)
— KHÔNG có request mạng nào.
Chạy: python3 -m unittest test_sp3_recon.py -v
"""

import json
import unittest
import urllib.parse

from requests.cookies import RequestsCookieJar

from ctf_downloader.platforms.capabilities import PlatformInfo
from ctf_downloader.platforms.detector import PlatformDetector
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.platforms.custom_rest import CustomRESTPlatform
from ctf_downloader.platforms.generic_html import GenericHTMLPlatform


# --------------------------------------------------------------------- #
# Giả lập HTTP
# --------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("Response không phải JSON")
        return self._json


class MockSession:
    """Session giả lập: route theo path của URL, hỗ trợ route '*' (catch-all)."""

    def __init__(self, routes=None, cookies=None):
        self.routes = dict(routes or {})
        self.cookies = cookies if cookies is not None else RequestsCookieJar()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        path = urllib.parse.urlparse(url).path or "/"
        for key in (path, "*"):
            if key in self.routes:
                resp = self.routes[key]
                return resp(url) if callable(resp) else resp
        return FakeResponse(status_code=404, text="Not Found")

    def post(self, url, **kwargs):
        self.calls.append(url)
        return FakeResponse(status_code=404, text="Not Found")


# --------------------------------------------------------------------- #
# Fixture HTML / JSON dựng từ source code thật của từng platform
# --------------------------------------------------------------------- #
GZCTF_HTML = (
    "<!DOCTYPE html><html><head><title>GZ::CTF</title>"
    '<meta content="Example CTF,GZCTF" name="keywords">'
    "<link rel=\"icon\" href=\"icon.png\"></head>"
    "<body><div id=root></div></body></html>"
)


def gz_config(api_key=None, port_mapping="", rules="# Luật lệ"):
    """Mô phỏng ClientConfig trả về từ GET /api/config của GZCTF."""
    return {
        "Title": "Example CTF",
        "Slogan": "Break, Learn, Secure",
        "Description": "",
        "Rules": rules,
        "PortMapping": port_mapping,
        "DefaultLifetime": 1800,
        "FlagPattern": "flag{.*}",
        "ApiPublicKey": api_key,
    }


CTFD_HTML = (
    "<!DOCTYPE html><html><head><title>CTFd</title>"
    "<script>var csrfNonce' = \"8f14e45fceea167a\";</script>"
    "</head><body>Powered by CTFd</body></html>"
)

CTFD_CHALLS_WHALE = {
    "success": True,
    "data": [
        {
            "id": 1,
            "name": "pwn-box",
            "type": "container",
            "template": "/plugins/ctfd-whale/templates/container.html",
            "script": "/plugins/ctfd-whale/assets/view.js",
        },
        {"id": 2, "name": "web-legacy", "type": "standard"},
    ],
}

RCTF_PLAIN_HTML = "<!DOCTYPE html><html><head><title>Competition</title></head><body></body></html>"


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #
class TestPlatformInfo(unittest.TestCase):
    def test_to_dict_serializable(self):
        info = PlatformInfo(platform_type="gzctf", base_url="https://x.example", game_id=3,
                            confidence="high")
        info.add_signal("sig-a")
        info.add_signal("sig-a")  # trùng -> bỏ qua
        dumped = json.dumps(info.to_dict())
        restored = json.loads(dumped)
        self.assertEqual(restored["platform_type"], "gzctf")
        self.assertEqual(restored["game_id"], 3)
        self.assertEqual(restored["signals"], ["sig-a"])
        self.assertIsNone(restored["capabilities"]["api_encryption"])
        self.assertFalse(restored["capabilities"]["container"])


class TestGZCTFRecon(unittest.TestCase):
    def _routes(self, config):
        return {
            "/api/config": FakeResponse(json_data=config),
            "*": FakeResponse(text=GZCTF_HTML),
        }

    def test_detect_via_html_config_plain(self):
        session = MockSession(routes=self._routes(gz_config()))
        url = "https://gz.example.com/games/6/challenges"
        platform, info = PlatformDetector.detect_platform_info(url, session)

        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(platform.info, info)
        self.assertEqual(info.platform_type, "gzctf")
        self.assertEqual(info.confidence, "high")
        self.assertEqual(info.game_id, 6)  # parse từ /games/<digits>
        # ApiPublicKey null -> api_encryption False; PortMapping rỗng -> False
        self.assertFalse(info.capabilities["api_encryption"])
        self.assertFalse(info.capabilities["port_mapping_proxy"])
        self.assertTrue(info.capabilities["rules_via_api"])   # Rules != rỗng trong config
        self.assertTrue(info.capabilities["scoreboard"])
        self.assertTrue(any("ClientConfig GZCTF" in s for s in info.signals))
        self.assertTrue(any("HTML marker" in s for s in info.signals))

    def test_detect_api_encryption_and_platform_proxy(self):
        config = gz_config(api_key="AQAB-public-key-b64", port_mapping="PlatformProxy")
        session = MockSession(routes=self._routes(config))
        platform, info = PlatformDetector.detect_platform_info(
            "https://gz.example.com/games/6/challenges", session)

        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(platform.game_id, 6)
        self.assertEqual(info.game_id, 6)
        self.assertTrue(info.capabilities["api_encryption"])       # ApiPublicKey != null
        self.assertTrue(info.capabilities["port_mapping_proxy"])   # PortMapping == PlatformProxy

    def test_detect_via_cookie_only(self):
        jar = RequestsCookieJar()
        jar.set("GZCTF_Token", "dummy-token")
        session = MockSession(routes={"/api/config": FakeResponse(json_data=gz_config())},
                              cookies=jar)
        platform, info = PlatformDetector.detect_platform_info("https://gz.example.com/", session)

        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(info.confidence, "high")  # medium (cookie) -> high (probe /api/config)
        self.assertTrue(any("GZCTF_Token" in s for s in info.signals))

    def test_detect_via_cookie_hint(self):
        session = MockSession(routes={"/api/config": FakeResponse(json_data=gz_config())})
        platform, info = PlatformDetector.detect_platform_info(
            "https://gz.example.com/", session, cookie_hint="GZCTF_Token=abc123; other=x")

        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(info.confidence, "high")

    def test_detect_via_game_recent_array_response(self):
        session = MockSession(routes={
            "/api/game/recent": FakeResponse(json_data={"data": [], "length": 0, "total": 0}),
        })
        platform, info = PlatformDetector.detect_platform_info("https://gz.example.com/", session)

        self.assertIsInstance(platform, GZCTFPlatform)
        self.assertEqual(info.confidence, "high")
        self.assertTrue(any("/api/game/recent" in s for s in info.signals))


class TestCTFdRecon(unittest.TestCase):
    def test_detect_and_whale_container(self):
        session = MockSession(routes={
            "/": FakeResponse(text=CTFD_HTML),
            "/api/v1/challenges": FakeResponse(json_data=CTFD_CHALLS_WHALE),
        })
        platform, info = PlatformDetector.detect_platform_info("https://ctfd.example.com/", session)

        self.assertIsInstance(platform, CTFdPlatform)
        self.assertEqual(platform.info, info)
        self.assertEqual(info.platform_type, "ctfd")
        self.assertEqual(info.confidence, "high")
        self.assertTrue(info.capabilities["container"])  # whale frankli0324 fork
        self.assertTrue(any("ctfd-whale" in s for s in info.signals))

    def test_ctfd_without_whale_has_no_container(self):
        session = MockSession(routes={
            "/": FakeResponse(text=CTFD_HTML),
            "/api/v1/challenges": FakeResponse(
                json_data={"success": True, "data": [{"id": 1, "type": "standard"}]}),
        })
        _, info = PlatformDetector.detect_platform_info("https://ctfd.example.com/", session)
        self.assertFalse(info.capabilities["container"])

    def test_detect_via_flask_session_cookie(self):
        jar = RequestsCookieJar()
        jar.set("session", ".eJwFlE...")
        session = MockSession(routes={
            "/api/v1/challenges": FakeResponse(
                json_data={"success": True, "data": []}),
        }, cookies=jar)
        platform, info = PlatformDetector.detect_platform_info("https://ctfd.example.com/", session)

        self.assertIsInstance(platform, CTFdPlatform)
        self.assertEqual(info.confidence, "high")
        self.assertTrue(any("session" in s.lower() for s in info.signals))


class TestRCTFRecon(unittest.TestCase):
    def test_detect_via_good_challenges(self):
        session = MockSession(routes={
            "/": FakeResponse(text=RCTF_PLAIN_HTML),
            "/api/v1/challs": FakeResponse(
                json_data={"kind": "goodChallenges", "message": "", "data": []}),
        })
        platform, info = PlatformDetector.detect_platform_info("https://rctf.example.com/", session)

        self.assertIsInstance(platform, RCTFPlatform)
        self.assertEqual(platform.info, info)
        self.assertEqual(info.platform_type, "rctf")
        self.assertEqual(info.confidence, "high")
        self.assertTrue(any("goodChallenges" in s for s in info.signals))

    def test_detect_via_bad_endpoint_envelope(self):
        # Bất kỳ URL lạ nào trên rCTF cũng trả envelope kind="badEndpoint"
        session = MockSession(routes={
            "/api/v1/challs": FakeResponse(
                json_data={"kind": "badEndpoint", "message": "Unknown endpoint.", "data": None}),
        })
        platform, info = PlatformDetector.detect_platform_info("https://rctf.example.com/", session)

        self.assertIsInstance(platform, RCTFPlatform)
        self.assertEqual(info.confidence, "high")
        self.assertTrue(any("badEndpoint" in s for s in info.signals))


class TestFallbackRecon(unittest.TestCase):
    def test_all_probes_404_falls_back_to_generic(self):
        session = MockSession()  # mọi route -> 404
        platform, info = PlatformDetector.detect_platform_info("https://mystery.example.com/", session)

        self.assertIsInstance(platform, GenericHTMLPlatform)
        self.assertEqual(platform.info, info)
        self.assertEqual(info.platform_type, "generic_html")
        self.assertEqual(info.confidence, "low")
        self.assertFalse(platform.info.capabilities["scoreboard"])

    def test_unknown_html_no_markers_falls_back_to_generic(self):
        session = MockSession(routes={
            "/": FakeResponse(text="<html><body>Hello world, nothing to see.</body></html>"),
        })
        platform, info = PlatformDetector.detect_platform_info("https://mystery.example.com/", session)

        self.assertIsInstance(platform, GenericHTMLPlatform)
        self.assertEqual(info.confidence, "low")
        self.assertEqual(info.platform_type, "generic_html")

    def test_legacy_custom_rest_probe_still_works(self):
        session = MockSession(routes={
            "/api/challenges": FakeResponse(
                json_data={"success": True, "data": {"challenges": []}}),
        })
        platform, info = PlatformDetector.detect_platform_info("https://next.example.com/", session)

        self.assertIsInstance(platform, CustomRESTPlatform)
        self.assertEqual(info.platform_type, "custom_rest")
        self.assertEqual(info.confidence, "high")


class TestBackwardCompatibility(unittest.TestCase):
    def test_detect_platform_returns_full_interface(self):
        session = MockSession(routes={
            "/api/config": FakeResponse(json_data=gz_config()),
            "*": FakeResponse(text=GZCTF_HTML),
        })
        platform = PlatformDetector.detect_platform(
            "https://gz.example.com/games/6/challenges", session)

        # Interface bắt buộc của BasePlatform vẫn callable
        self.assertTrue(callable(getattr(platform, "authenticate")))
        self.assertTrue(callable(getattr(platform, "fetch_challenges")))
        self.assertTrue(callable(getattr(platform, "submit_flag")))
        self.assertTrue(callable(getattr(platform, "get_full_file_url")))
        # setattr mềm: instance mang theo PlatformInfo mà class không khai báo sẵn
        self.assertIsInstance(getattr(platform, "info", None), PlatformInfo)
        self.assertEqual(platform.info.game_id, 6)
        self.assertEqual(platform.ctf_info.platform_type, "gzctf")

    def test_normalize_url_suffixes(self):
        parsed, origin, clean = PlatformDetector._normalize(
            "https://ctf.example.com/scoreboard#top")
        self.assertEqual(origin, "https://ctf.example.com")
        self.assertEqual(clean, "https://ctf.example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
