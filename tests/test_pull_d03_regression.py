"""Regression tests for CTF-PULL-D03: URL normalization, probe demotion, and REST CTF challenge shapes."""
import unittest
import urllib.parse
from unittest.mock import MagicMock

from ctf_downloader.utils.urlnorm import normalize_base_url, parse_normalized
from ctf_downloader.platforms.detection import detect_platform_info
from ctf_downloader.platforms.custom_rest import CustomRESTPlatform


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("Not JSON")
        return self._json


class MockRoutingSession:
    """Mock session dispatching responses based on URL path."""

    def __init__(self, routes=None, cookies=None):
        self.routes = dict(routes or {})
        self.cookies = dict(cookies or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        path = urllib.parse.urlparse(url).path or "/"
        resp = self.routes.get(path, self.routes.get("*"))
        if resp is None:
            return FakeResponse(status_code=404, text="Not Found")
        return resp(url) if callable(resp) else resp


class TestUrlNormPageExtensions(unittest.TestCase):
    def test_strips_rules_html(self):
        self.assertEqual(
            normalize_base_url("https://pointeroverflowctf.com/rules.html"),
            "https://pointeroverflowctf.com",
        )

    def test_strips_nested_path_with_page_extension(self):
        self.assertEqual(
            normalize_base_url("https://ctf.example.com/contest/rules.html"),
            "https://ctf.example.com/contest",
        )
        self.assertEqual(
            normalize_base_url("https://ctf.example.com/challenges.htm"),
            "https://ctf.example.com",
        )
        self.assertEqual(
            normalize_base_url("https://ctf.example.com/index.php"),
            "https://ctf.example.com",
        )

    def test_preserves_plain_base_url(self):
        self.assertEqual(
            normalize_base_url("https://ctf.example.com"),
            "https://ctf.example.com",
        )


class TestPlatformDetectionRegressionD03(unittest.TestCase):
    def test_session_cookie_with_failed_ctfd_probe_detects_custom_rest(self):
        """A session cookie must not lock detection to CTFd when CTFd probe 404s and /api/challenges exists."""
        routes = {
            "/": FakeResponse(status_code=200, text="<html><title>POCTF</title></html>"),
            "/api/v1/challenges": FakeResponse(status_code=404, text="Not Found"),
            "/api/challenges": FakeResponse(
                status_code=200,
                json_data={
                    "challenges": [
                        {
                            "id": 2,
                            "name": "Letters Never Sent",
                            "category": "Cryptography",
                            "points": 95,
                        }
                    ]
                },
            ),
            "/api/me": FakeResponse(
                status_code=200,
                json_data={"logged_in": True, "user": {"username": "tester"}},
            ),
        }
        session = MockRoutingSession(routes=routes, cookies={"session": "dummy_cookie"})
        platform, info = detect_platform_info(
            "https://pointeroverflowctf.com/rules.html",
            session,
            cookie_hint="session=dummy_cookie",
            quiet=True,
        )
        self.assertEqual(info.platform_type, "custom_rest")
        self.assertEqual(info.confidence, "high")
        self.assertEqual(platform.base_url, "https://pointeroverflowctf.com")

    def test_failed_probe_demotes_medium_confidence(self):
        """When Tầng 2 guesses a platform via cookie hint, a failing probe demotes the candidate."""
        routes = {
            "/": FakeResponse(status_code=200, text="<html><title>Generic</title></html>"),
            "/api/v1/challenges": FakeResponse(status_code=404, text="Not Found"),
            "/api/challenges": FakeResponse(status_code=404, text="Not Found"),
            "/api/auth/me": FakeResponse(status_code=404, text="Not Found"),
            "/api/me": FakeResponse(status_code=404, text="Not Found"),
        }
        session = MockRoutingSession(routes=routes, cookies={"session": "dummy_cookie"})
        platform, info = detect_platform_info(
            "https://example.com",
            session,
            cookie_hint="session=dummy_cookie",
            quiet=True,
        )
        self.assertNotEqual(info.platform_type, "ctfd")


class TestCustomRESTPlatformPointerOverflow(unittest.TestCase):
    def test_auth_and_fetch_challenges(self):
        routes = {
            "/api/me": FakeResponse(
                status_code=200,
                json_data={"logged_in": True, "user": {"username": "ctf_player"}},
            ),
            "/api/challenges": FakeResponse(
                status_code=200,
                json_data={
                    "challenges": [
                        {
                            "id": 42,
                            "name": "Secret Message",
                            "category": "Crypto",
                            "points": 100,
                            "solved": False,
                        }
                    ]
                },
            ),
            "/api/challenges/42": FakeResponse(
                status_code=200,
                json_data={
                    "challenge": {
                        "id": 42,
                        "name": "Secret Message",
                        "prompt_text": "Decrypt this message.",
                        "target_url": "/challenges/secret-message/",
                    }
                },
            ),
        }
        session = MockRoutingSession(routes=routes)
        platform = CustomRESTPlatform("https://pointeroverflowctf.com/rules.html", session)
        self.assertTrue(platform.authenticate())
        self.assertEqual(platform.ctf_info.user_name, "ctf_player")

        challs = platform.fetch_challenges()
        self.assertEqual(len(challs), 1)
        self.assertEqual(challs[0].id, 42)
        self.assertEqual(challs[0].name, "Secret Message")
        self.assertEqual(challs[0].category, "Crypto")
        self.assertEqual(challs[0].points, 100)
        self.assertEqual(challs[0].description, "Decrypt this message.")
        self.assertEqual(challs[0].connection_info, "/challenges/secret-message/")

    def test_direct_data_in_detail_envelope(self):
        """A detail response with {success: true, data: {id: ..., description: ...}} must not be lost."""
        routes = {
            "/api/challenges": FakeResponse(
                status_code=200,
                json_data={"data": [{"id": 10, "name": "Direct Test", "category": "Web"}]},
            ),
            "/api/challenges/10": FakeResponse(
                status_code=200,
                json_data={
                    "success": True,
                    "data": {
                        "id": 10,
                        "name": "Direct Test",
                        "description": "Direct data body",
                        "target_url": "http://direct.test:8080",
                    },
                },
            ),
        }
        session = MockRoutingSession(routes=routes)
        platform = CustomRESTPlatform("https://example.com", session)
        challs = platform.fetch_challenges()
        self.assertEqual(len(challs), 1)
        self.assertEqual(challs[0].description, "Direct data body")
        self.assertEqual(challs[0].connection_info, "http://direct.test:8080")

    def test_empty_challenge_list_returns_empty_without_error(self):
        """Empty challenge list on REST CTF returns empty list cleanly."""
        routes = {
            "/api/challenges": FakeResponse(
                status_code=200,
                json_data={"challenges": []},
            ),
        }
        session = MockRoutingSession(routes=routes)
        platform = CustomRESTPlatform("https://example.com", session)
        challs = platform.fetch_challenges()
        self.assertEqual(challs, [])


if __name__ == "__main__":
    unittest.main()
