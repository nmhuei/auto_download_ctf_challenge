"""Dynamic-container contract matrix for CTFd plugins and GZCTF."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.services.instance_keepalive import (
    GIVE_UP,
    InstanceKeepAlive,
    InstanceTracker,
    RESTARTING,
)
from ctf_downloader.services import instance_service as instance_service_module
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


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
        self.assertEqual(s.post.call_args_list[1].kwargs["params"], {"challenge_id": 7})

    def test_start_does_not_fallback_after_non_not_found_http_error(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                p, s = self.platform()
                s.post.return_value = resp(status, text="upstream failure")
                ok, _info = p.start_instance(7)
                self.assertFalse(ok)
                self.assertEqual(s.post.call_count, 1)

    def test_start_does_not_fallback_after_transport_error(self):
        p, s = self.platform()
        s.post.side_effect = TimeoutError("timed out")
        ok, _info = p.start_instance(7)
        self.assertFalse(ok)
        self.assertEqual(s.post.call_count, 1)

    def test_start_rejects_success_envelope_without_instance_data(self):
        p, s = self.platform()
        s.post.return_value = resp(200, {"success": True, "data": {}})
        ok, _info = p.start_instance(7)
        self.assertFalse(ok)
        self.assertEqual(s.post.call_count, 1)

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
        s.post.return_value = resp(200, {"success": True})
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

    def test_start_rejects_malformed_success_response(self):
        p, s = self.platform()
        s.post.return_value = resp(200, {"foo": "bar"})
        ok, _info = p.start_instance(3)
        self.assertFalse(ok)
        s.get.assert_not_called()

    def test_start_normalizes_expect_stop_at_epoch_ms(self):
        p, s = self.platform()
        deadline_ms = int(time.time() * 1000) + 120_000
        s.post.return_value = resp(200, {
            "success": True,
            "context": {
                "instanceEntry": "[2001:db8::7]:31337",
                "expectStopAt": deadline_ms,
            },
        })
        ok, info = p.start_instance(3)
        self.assertTrue(ok)
        self.assertEqual(info["entry"], "[2001:db8::7]:31337")
        self.assertEqual(info["close_time"], deadline_ms)
        self.assertGreater(info["time_left"], 118)
        self.assertLessEqual(info["time_left"], 121)

    def test_extend_rejects_malformed_success_response(self):
        p, s = self.platform()
        s.post.return_value = resp(200, {"foo": "bar"})
        ok, _msg = p.extend_instance(3)
        self.assertFalse(ok)

    def test_status_normalizes_expect_stop_at_epoch_ms_to_time_left(self):
        p, s = self.platform()
        deadline_ms = int(time.time() * 1000) + 90_000
        s.get.return_value = resp(200, {
            "type": "dynamic",
            "context": {
                "instanceEntry": "box.test:1234",
                "expectStopAt": deadline_ms,
            },
        })
        status = p.get_instance_status(3)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["close_time"], deadline_ms)
        self.assertGreater(status["time_left"], 88)
        self.assertLessEqual(status["time_left"], 91)

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


class TestInstanceEndpointParser(unittest.TestCase):
    def test_parses_supported_endpoint_forms(self):
        parse_host_port = getattr(instance_service_module, "parse_host_port", None)
        self.assertIsNotNone(parse_host_port)
        cases = {
            "127.0.0.1:31337": ("127.0.0.1", 31337),
            "box.example:443": ("box.example", 443),
            "[2001:db8::7]:31337": ("2001:db8::7", 31337),
            "https://box.example:8443/path": ("box.example", 8443),
            "https://[2001:db8::7]:8443/path": ("2001:db8::7", 8443),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_host_port(value), expected)

    def test_rejects_ambiguous_or_unsafe_endpoint(self):
        parse_host_port = getattr(instance_service_module, "parse_host_port", None)
        self.assertIsNotNone(parse_host_port)
        for value in ("", "box.example", "2001:db8::7:31337", "box.example:0",
                      "box.example:65536", "http://box.example/no-port",
                      "http://user:pass@box.example:80"):
            with self.subTest(value=value):
                self.assertIsNone(parse_host_port(value))


class TestInstanceServiceDualWrite(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="instance-contract-"))
        self.challenge_dir = self.root / "Web" / "box"
        self.challenge_dir.mkdir(parents=True)
        challenge = {
            "id": 7,
            "name": "box",
            "instance_info": {
                "is_container": True,
                "status": "running",
                "active_instance": "box.example:31337",
                "remaining_time": 120,
            },
        }
        (self.root / "challenges.json").write_text(
            json.dumps({"challenges": [dict(challenge)]}), encoding="utf-8")
        (self.challenge_dir / "metadata.json").write_text(
            json.dumps(challenge), encoding="utf-8")
        self.repo = WorkspaceRepo(self.root)
        self.service = instance_service_module.InstanceService.__new__(
            instance_service_module.InstanceService)
        self.service.workspace_path = str(self.root)
        self.service.repo = self.repo

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_stop_clears_active_instance_and_remaining_time_in_both_files(self):
        self.service._update_local_instance_info(
            7, entry=None, time_left=0, status="stopped")
        metadata = json.loads(
            (self.challenge_dir / "metadata.json").read_text(encoding="utf-8"))
        challenges = json.loads(
            (self.root / "challenges.json").read_text(encoding="utf-8"))
        for payload in (metadata, challenges["challenges"][0]):
            instance = payload["instance_info"]
            self.assertEqual(instance["status"], "stopped")
            self.assertNotIn("active_instance", instance)
            self.assertNotIn("remaining_time", instance)


class _KeepalivePlatform:
    def __init__(self, kind="gzctf", extend_result=(False, "HTTP 503")):
        self.kind = kind
        self.extend_result = extend_result
        self.extends = 0
        self.starts = 0
        self.stops = 0
        self.status = {"status": "running", "entry": "box.example:31337",
                       "time_left": 120}

    def get_instance_status(self, _challenge_id):
        return dict(self.status)

    def extend_instance(self, _challenge_id):
        self.extends += 1
        return self.extend_result

    def start_instance(self, _challenge_id):
        self.starts += 1
        return True, {"entry": "box.example:31337"}

    def stop_instance(self, _challenge_id):
        self.stops += 1
        return True, "stopped"


class TestInstanceKeepaliveContracts(unittest.TestCase):
    def make_keepalive(self, platform):
        svc = SimpleNamespace(platform=platform, repo=None,
                              list_containers=lambda: [])
        return InstanceKeepAlive(svc, repo=None)

    def test_failed_gzctf_renew_waits_for_jitter_deadline(self):
        platform = _KeepalivePlatform(extend_result=(False, "HTTP 503"))
        keepalive = self.make_keepalive(platform)
        tracker = InstanceTracker(7, "box", platform_kind="gzctf")
        keepalive.tick_one(tracker)
        self.assertEqual(platform.extends, 1)
        self.assertIsNotNone(tracker.phase_deadline)
        keepalive.tick_one(tracker)
        self.assertEqual(platform.extends, 1)

    def test_429_retry_after_sets_renew_deadline(self):
        platform = _KeepalivePlatform(
            extend_result=(False, "HTTP 429: Retry-After=90"))
        keepalive = self.make_keepalive(platform)
        tracker = InstanceTracker(7, "box", platform_kind="gzctf")
        before = time.monotonic()
        keepalive.tick_one(tracker)
        self.assertGreaterEqual(tracker.phase_deadline - before, 89)
        keepalive.tick_one(tracker)
        self.assertEqual(platform.extends, 1)

    def test_whale_restart_applies_gap_to_stop_and_start(self):
        platform = _KeepalivePlatform(kind="whale")
        keepalive = self.make_keepalive(platform)
        tracker = InstanceTracker(7, "box", platform_kind="whale")
        tracker.last_op_mono = time.monotonic()
        keepalive._begin_restart(tracker)
        self.assertEqual(tracker.state, RESTARTING)
        self.assertEqual(platform.stops, 0)

        tracker.last_op_mono = time.monotonic() - 62
        tracker.phase_deadline = 0
        keepalive.tick_one(tracker)
        self.assertEqual(platform.stops, 1)
        self.assertEqual(platform.starts, 0)

        tracker.phase_deadline = 0
        keepalive.tick_one(tracker)
        self.assertEqual(platform.starts, 0)


if __name__ == "__main__":
    unittest.main()
