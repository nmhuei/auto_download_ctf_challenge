"""Dynamic-container contract matrix for CTFd plugins and GZCTF."""

import unittest
from unittest.mock import MagicMock

from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform


def resp(status=200, data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if data is None:
        r.json.return_value = {}
    else:
        r.json.return_value = data
    return r


class TestCTFdInstanceContract(unittest.TestCase):
    def platform(self):
        session = MagicMock()
        p = CTFdPlatform("https://ctfd.test", session)
        p.nonce = "n"
        return p, session

    def test_v1_start_normalizes_html_user_access(self):
        p, s = self.platform()
        s.post.return_value = resp(200, {
            "success": True,
            "data": {
                "user_access": '<a href="https://box.test:443/">connect</a>',
                "remaining_time": 900,
            },
        })
        ok, info = p.start_instance(7)
        self.assertTrue(ok)
        self.assertEqual(info["entry"], "https://box.test:443/")
        self.assertEqual(info["time_left"], 900)

    def test_legacy_start_uses_same_normalization(self):
        p, s = self.platform()
        s.post.side_effect = [
            resp(404),
            resp(200, {
                "success": True,
                "data": {
                    "user_access": '<a href="https://legacy.test/">go</a>',
                    "remaining_time": 500,
                },
            }),
        ]
        ok, info = p.start_instance(7)
        self.assertTrue(ok)
        self.assertEqual(info["entry"], "https://legacy.test/")
        self.assertEqual(info["time_left"], 500)

    def test_generic_start_normalizes_entry_and_time(self):
        p, s = self.platform()
        s.post.side_effect = [
            resp(404),
            resp(404),
            resp(201, {"data": {
                "host": "box.test",
                "port": 31337,
                "time_left": 321,
            }}),
        ]
        ok, info = p.start_instance(7)
        self.assertTrue(ok)
        self.assertEqual(info["entry"], "box.test:31337")
        self.assertEqual(info["time_left"], 321)

    def test_stop_and_extend_accept_204(self):
        p, s = self.platform()
        s.delete.return_value = resp(204)
        self.assertTrue(p.stop_instance(7)[0])
        s.patch.return_value = resp(204)
        self.assertTrue(p.extend_instance(7)[0])

    def test_status_network_failure_is_unknown_not_stopped(self):
        p, s = self.platform()
        s.get.side_effect = ConnectionError("down")
        st = p.get_instance_status(7)
        self.assertEqual(st["status"], "unknown")
        self.assertEqual(st["reason"], "unreachable_or_unsupported")

    def test_status_auth_failure_is_unknown_with_reason(self):
        p, s = self.platform()
        s.get.side_effect = [resp(401), resp(403)]
        st = p.get_instance_status(7)
        self.assertEqual(st["status"], "unknown")
        self.assertEqual(st["reason"], "auth_failed")
        self.assertIn(st["http_status"], (401, 403))

    def test_successful_empty_v1_status_is_explicit_stopped(self):
        p, s = self.platform()
        s.get.return_value = resp(200, {"success": True, "data": {}})
        st = p.get_instance_status(7)
        self.assertEqual(st["status"], "stopped")


class TestGZCTFInstanceContract(unittest.TestCase):
    def platform(self, game_id=42):
        s = MagicMock()
        p = GZCTFPlatform("https://gz.test/games/42/challenges", s)
        p.game_id = game_id
        return p, s

    def test_missing_game_id_never_builds_none_url(self):
        p, s = self.platform(game_id=None)
        self.assertFalse(p.start_instance(3)[0])
        self.assertFalse(p.stop_instance(3)[0])
        self.assertFalse(p.extend_instance(3)[0])
        self.assertEqual(p.get_instance_status(3)["reason"], "missing_game_id")
        s.get.assert_not_called()
        s.post.assert_not_called()
        s.delete.assert_not_called()

    def test_start_uses_status_entry_when_post_has_no_entry(self):
        p, s = self.platform()
        s.post.return_value = resp(200, {"foo": "bar"})
        s.get.return_value = resp(200, {
            "type": "dynamic",
            "context": {
                "instanceEntry": "box.test:1234",
                "closeTime": 123456,
            },
        })
        ok, info = p.start_instance(3)
        self.assertTrue(ok)
        self.assertEqual(info["entry"], "box.test:1234")

    def test_status_401_is_unknown_auth_not_stopped(self):
        p, s = self.platform()
        s.get.return_value = resp(401)
        st = p.get_instance_status(3)
        self.assertEqual(st["status"], "unknown")
        self.assertEqual(st["reason"], "auth_failed")
        self.assertEqual(st["http_status"], 401)

    def test_status_transport_failure_is_unknown(self):
        p, s = self.platform()
        s.get.side_effect = TimeoutError("timeout")
        st = p.get_instance_status(3)
        self.assertEqual(st["status"], "unknown")
        self.assertTrue(st["reason"].startswith("transport:"))


if __name__ == "__main__":
    unittest.main()
