"""
Regression tests for CTF-PLATFORM-D01: rCTF instancer HTTP 405 drift.

The rCTF v2 instancer API (/api/v2/integrations/challs/{id}/instance) is
documented to accept PUT for start on older deployments.  Some newer or
custom rCTF deployments reject PUT with HTTP 405 (Method Not Allowed) and
expect POST instead.

The fix: when PUT returns 405, transparently fall back to POST on the same
URL with the same payload, preserving backward compatibility for deployments
that only accept PUT.

These tests MUST FAIL before the fix and PASS after.
"""

import unittest
from unittest.mock import MagicMock, call


def _resp(status: int, data=None, text: str = "") -> MagicMock:
    """Build a minimal mock HTTP response."""
    r = MagicMock()
    r.status_code = status
    r.text = text or ("" if data is None else "")
    if data is not None:
        r.json.return_value = data
    else:
        r.json.side_effect = ValueError("no json")
    return r


def _good_instance_data() -> dict:
    return {
        "kind": "goodStartInstance",
        "message": "Container started",
        "data": {
            "status": "running",
            "endpoints": [{"host": "chall.example.com", "port": 31337}],
            "timeLeftMilliseconds": 1200000,
        },
    }


class TestRCTFInstanceStart405Fallback(unittest.TestCase):
    """CTF-PLATFORM-D01 regression: 405 on PUT must fall back to POST."""

    def _platform(self):
        from ctf_downloader.platforms.rctf import RCTFPlatform
        sess = MagicMock()
        p = RCTFPlatform("https://rctf.example.com", sess)
        return p, sess

    # ------------------------------------------------------------------ #
    # Primary regression: PUT → 405 → POST success                        #
    # ------------------------------------------------------------------ #

    def test_start_instance_falls_back_to_post_on_405(self):
        """When PUT returns 405, start_instance must retry with POST."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(200, _good_instance_data())

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertTrue(ok, f"Expected success after POST fallback; got info={info}")
        self.assertEqual(info.get("entry"), "chall.example.com:31337")
        self.assertGreater(info.get("time_left", 0), 0)

        # PUT must have been tried first
        sess.put.assert_called_once()
        # POST must have been tried exactly once as fallback
        sess.post.assert_called_once()

    def test_fallback_post_uses_same_url_and_empty_json_body(self):
        """The POST fallback must hit the same instancer URL."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(200, _good_instance_data())

        p.start_instance("boot2root_Omniscient")

        put_url = sess.put.call_args[0][0] if sess.put.call_args[0] else sess.put.call_args.args[0]
        post_url = sess.post.call_args[0][0] if sess.post.call_args[0] else sess.post.call_args.args[0]
        self.assertEqual(put_url, post_url, "PUT and POST must target the same URL")

    def test_no_post_attempted_when_put_succeeds(self):
        """When PUT succeeds (200), POST must never be called."""
        p, sess = self._platform()
        sess.put.return_value = _resp(200, _good_instance_data())

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertTrue(ok)
        sess.post.assert_not_called()

    def test_no_post_fallback_on_non_405_errors(self):
        """Non-405 errors (400, 500, 503) must NOT trigger POST fallback."""
        for status in (400, 500, 502, 503):
            with self.subTest(status=status):
                p, sess = self._platform()
                sess.put.return_value = _resp(status, text=f"error {status}")
                ok, _info = p.start_instance("boot2root_Omniscient")
                self.assertFalse(ok, f"Expected failure on PUT {status}")
                sess.post.assert_not_called()

    def test_post_fallback_failure_returns_false(self):
        """When PUT returns 405 AND POST also fails, start_instance must return False."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(500, text="Internal Server Error")

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertFalse(ok)
        sess.post.assert_called_once()
        self.assertIn("500", info.get("message", ""))

    def test_post_fallback_respects_bad_kind_envelope(self):
        """POST fallback that returns a bad* kind envelope must still return False."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(200, {
            "kind": "badClientToken",
            "message": "Invalid auth token",
        })

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertFalse(ok)
        self.assertIn("Invalid auth token", info.get("message", ""))

    def test_post_fallback_handles_good_kind_without_endpoints(self):
        """POST returns goodStartInstance but no endpoints → True with None entry."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(200, {
            "kind": "goodStartInstance",
            "message": "OK",
            "data": {
                "status": "running",
                "endpoints": [],
                "timeLeftMilliseconds": 900000,
            },
        })

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertTrue(ok, f"Expected success even without endpoints; info={info}")
        self.assertIsNone(info.get("entry"))
        self.assertGreater(info.get("time_left", 0), 0)

    def test_fallback_post_targets_correct_instancer_path(self):
        """The POST fallback URL must contain the encoded challenge ID segment."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.return_value = _resp(200, _good_instance_data())

        p.start_instance("boot2root_Omniscient")

        post_url = sess.post.call_args[0][0] if sess.post.call_args[0] else sess.post.call_args.args[0]
        # The instancer URL must embed the challenge ID in the path
        self.assertIn("boot2root_Omniscient", post_url)
        self.assertIn("/api/v2/integrations/challs/", post_url)
        self.assertIn("/instance", post_url)


    def test_transport_exception_during_fallback_returns_false(self):
        """If POST fallback raises a network exception, return False cleanly."""
        p, sess = self._platform()
        sess.put.return_value = _resp(405, text="Method Not Allowed")
        sess.post.side_effect = ConnectionError("network failure")

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertFalse(ok)
        self.assertIsInstance(info.get("message"), str)

    def test_put_transport_exception_does_not_trigger_post(self):
        """A network exception on PUT must not silently fall back to POST."""
        p, sess = self._platform()
        sess.put.side_effect = TimeoutError("timed out")

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertFalse(ok)
        sess.post.assert_not_called()


class TestRCTFInstanceStart405FallbackEncoding(unittest.TestCase):
    """Verify the PUT→POST fallback with the exact challenge ID from the incident."""

    def test_incident_challenge_id_omniscient(self):
        """boot2root_Omniscient (the incident challenge ID) must succeed via POST."""
        from ctf_downloader.platforms.rctf import RCTFPlatform
        sess = MagicMock()
        p = RCTFPlatform("https://rctf.example.com", sess)

        sess.put.return_value = _resp(405, text="")
        sess.post.return_value = _resp(200, {
            "kind": "goodStartInstance",
            "data": {
                "status": "running",
                "endpoints": [{"host": "10.0.0.1", "port": 9999}],
                "timeLeftMilliseconds": 600000,
            },
        })

        ok, info = p.start_instance("boot2root_Omniscient")

        self.assertTrue(ok)
        self.assertEqual(info["entry"], "10.0.0.1:9999")
        self.assertAlmostEqual(info["time_left"], 600, delta=2)


if __name__ == "__main__":
    unittest.main()
