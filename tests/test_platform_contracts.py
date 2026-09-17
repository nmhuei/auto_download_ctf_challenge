"""Cross-platform submit contracts and ASIS adapter verification."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.platforms.asisctf import ASISCTFPlatform, probe_asisctf_challs
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.services.submit_service import SubmitService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def make_resp(status=200, data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text if text else (json.dumps(data) if data is not None else "")
    if data is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = data
    return r


class TestASISPlatformContract(unittest.TestCase):
    def _platform(self):
        sess = MagicMock()
        sess.cookies.get.return_value = None
        p = ASISCTFPlatform("https://asis.test", sess)
        p._extract_meta = MagicMock()
        return p, sess

    def test_probe_recognizes_challenge_list_shape(self):
        session = MagicMock()
        session.get.return_value = make_resp(
            200, [{"id": 7, "name": "Fence"}]
        )
        info = MagicMock()
        info.capabilities = {}
        info.add_signal = MagicMock()
        self.assertTrue(
            probe_asisctf_challs("https://asis.test", session, info, set())
        )
        self.assertTrue(info.capabilities["scoreboard"])

    def test_probe_accepts_valid_empty_challenge_list(self):
        session = MagicMock()
        session.get.return_value = make_resp(200, [])
        info = MagicMock()
        info.capabilities = {}
        info.add_signal = MagicMock()
        self.assertTrue(
            probe_asisctf_challs("https://asis.test", session, info, set())
        )

    def test_auth_success_and_invalid_json_failure(self):
        p, sess = self._platform()
        sess.get.return_value = make_resp(200, [])
        self.assertTrue(p.authenticate())

        sess.get.return_value = make_resp(200, {"not": "a list"})
        self.assertFalse(p.authenticate())

    def test_fetch_challenges_maps_core_fields_and_attachments(self):
        p, sess = self._platform()
        sess.get.return_value = make_resp(200, [{
            "id": 11,
            "name": "Headache",
            "categories": [{"name": "Crypto"}, {"name": "Hard"}],
            "dynamic_points": 477,
            "description": (
                '<a href="/tasks/headache.txz">download</a>\n'
                "nc chall.asis.test 31337"
            ),
            "SolvedByCurrentTeam": True,
            "solves_count": 9,
        }])
        challs = p.fetch_challenges()
        self.assertEqual(len(challs), 1)
        c = challs[0]
        self.assertEqual(c.id, 11)
        self.assertEqual(c.category, "Crypto")
        self.assertEqual(c.points, 477)
        self.assertTrue(c.solved_by_me)
        self.assertEqual(c.solves_count, 9)
        self.assertIn("Crypto", c.tags)
        self.assertTrue(any("headache.txz" in url for url, _name in c.files))
        self.assertIn("chall.asis.test", c.connection_info)

    def test_fetch_challenges_skips_malformed_entry_and_preserves_zero_dynamic_points(self):
        p, sess = self._platform()
        sess.get.return_value = make_resp(200, [
            "broken-entry",
            {
                "id": 12,
                "name": "Zero Day",
                "categories": "bad-shape",
                "dynamic_points": 0,
                "rewardable_dynamic_points": 400,
                "points": 500,
                "description": None,
                "solves_count": "7",
            },
        ])
        challs = p.fetch_challenges()
        self.assertEqual(len(challs), 1)
        self.assertEqual(challs[0].id, 12)
        self.assertEqual(challs[0].points, 0)
        self.assertEqual(challs[0].category, "Misc")
        self.assertEqual(challs[0].description, "")
        self.assertEqual(challs[0].solves_count, 7)

    def test_submit_invalid_id_fails_before_network(self):
        p, sess = self._platform()
        ok, _msg = p.submit_flag("not-an-id", "ASIS{x}")
        self.assertFalse(ok)
        self.assertEqual(p.last_verdict, "challenge_not_found")
        sess.post.assert_not_called()

    def test_submit_has_typed_transient_verdicts_and_csrf(self):
        p, sess = self._platform()
        sess.cookies.get.return_value = "csrf%3Dtoken"

        sess.post.return_value = make_resp(429, {"message": "slow down"})
        self.assertFalse(p.submit_flag(1, "ASIS{x}")[0])
        self.assertEqual(p.last_verdict, "ratelimited")

        sess.post.return_value = make_resp(419, {"message": "expired"})
        self.assertFalse(p.submit_flag(1, "ASIS{x}")[0])
        self.assertEqual(p.last_verdict, "auth_failed")

        sess.post.return_value = make_resp(
            422, {"errors": {"answer": ["Wrong Answer"]}}
        )
        self.assertFalse(p.submit_flag(1, "ASIS{bad}")[0])
        self.assertEqual(p.last_verdict, "incorrect")

        headers = sess.post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-XSRF-TOKEN"], "csrf=token")

    def test_scoreboard_contract(self):
        p, sess = self._platform()
        p.ctf_info.team_name = "blue"
        html = """
        <table>
          <tr><th>#</th><th>Team</th><th>Score</th></tr>
          <tr><td>1</td><td>red</td><td>1200</td></tr>
          <tr class="bg-theme-3"><td>2</td><td>blue</td><td>900</td></tr>
        </table>
        """
        sess.get.return_value = make_resp(200, text=html)
        board = p.fetch_scoreboard()
        self.assertEqual(board["total_teams"], 2)
        self.assertEqual(board["my_team"], "blue")
        self.assertEqual(board["my_rank"], 2)
        self.assertEqual(board["my_score"], 900)

    def test_scoreboard_preserves_displayed_rank_and_comma_score(self):
        p, sess = self._platform()
        p.ctf_info.team_name = "blue"
        html = """
        <table>
          <tr><th>#</th><th>Team</th><th>Score</th></tr>
          <tr><td>4</td><td>red</td><td>1,500</td></tr>
          <tr class="bg-theme-3"><td>7</td><td>blue</td><td>1,200</td></tr>
        </table>
        """
        sess.get.return_value = make_resp(200, text=html)
        board = p.fetch_scoreboard()
        self.assertEqual(board["standings"][0]["pos"], 4)
        self.assertEqual(board["standings"][0]["score"], 1500)
        self.assertEqual(board["my_rank"], 7)
        self.assertEqual(board["my_score"], 1200)

    def test_solve_attribution_is_team_level_and_cached(self):
        p, sess = self._platform()
        p.ctf_info.team_name = "our-team"
        p.ctf_info.user_name = "alice"
        sess.get.return_value = make_resp(200, [
            {
                "id": 21,
                "SolvedByCurrentTeam": True,
                "first_n_solves": [
                    {"team_name": "our-team",
                     "solved_at": "2026-08-29 18:05:30"},
                    {"team_name": "other",
                     "solved_at": "2026-08-29 18:06:00"},
                ],
            },
            {"id": 22, "SolvedByCurrentTeam": False},
        ])
        first = p.fetch_solve_attribution([21, 22])
        self.assertIn(21, first)
        self.assertNotIn(22, first)
        attr = first[21]
        self.assertFalse(attr.by_me)
        self.assertTrue(attr.by_team)
        self.assertEqual(attr.solver_names, ["our-team"])
        self.assertTrue(attr.first_blood)
        self.assertIsInstance(attr.solved_at, int)

        before = sess.get.call_count
        second = p.fetch_solve_attribution([21])
        self.assertEqual(sess.get.call_count, before)
        self.assertTrue(second[21].by_team)


class TestTypedPlatformVerdicts(unittest.TestCase):
    def test_rctf_distinguishes_already_event_and_auth(self):
        p = RCTFPlatform("https://rctf.test", MagicMock())
        cases = [
            ({"kind": "alreadySolved"}, 200, "already_solved"),
            ({"kind": "badStarted"}, 401, "event_not_started"),
            ({"kind": "badEnded"}, 401, "event_closed"),
            ({"kind": "badToken"}, 401, "auth_failed"),
            ({"kind": "badRateLimit"}, 429, "ratelimited"),
        ]
        for data, status, expected in cases:
            with self.subTest(expected=expected):
                p.session.post.return_value = make_resp(status, data)
                p.submit_flag("x", "flag{x}")
                self.assertEqual(p.last_verdict, expected)

    def test_ctfd_distinguishes_already_auth_rate_and_pause(self):
        p = CTFdPlatform("https://ctfd.test", MagicMock())
        p.nonce = "n"

        p.session.post.return_value = make_resp(
            200, {"success": True, "data": {"status": "already_solved"}}
        )
        p.submit_flag(1, "FLAG{x}")
        self.assertEqual(p.last_verdict, "already_solved")

        p.session.post.return_value = make_resp(401, {"success": False})
        p.submit_flag(1, "FLAG{x}")
        self.assertEqual(p.last_verdict, "auth_failed")

        p.session.post.return_value = make_resp(429, {"success": False})
        p.submit_flag(1, "FLAG{x}")
        self.assertEqual(p.last_verdict, "ratelimited")

        p.session.post.return_value = make_resp(
            200, {"success": True, "data": {"status": "paused"}}
        )
        p.submit_flag(1, "FLAG{x}")
        self.assertEqual(p.last_verdict, "event_paused")

    def test_gzctf_distinguishes_cheat_and_auth(self):
        p = GZCTFPlatform("https://gz.test/games/5/challenges", MagicMock())
        p.session.get.return_value = make_resp(200, {})
        p.session.post.return_value = make_resp(400, text="CheatDetected")
        p.submit_flag(1, "GZCTF{x}")
        self.assertEqual(p.last_verdict, "cheat_detected")

        p.session.post.return_value = make_resp(401, text="expired")
        p.submit_flag(1, "GZCTF{x}")
        self.assertEqual(p.last_verdict, "auth_failed")


class TestSubmitServiceVerdictSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        with open(os.path.join(root, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ctf_info": {
                    "url": "https://ctfd.test",
                    "flag_format": "^FLAG\\{.+\\}$",
                    "flag_format_source": "test",
                },
                "challenges": [{"id": 1, "name": "One", "category": "Web"}],
            }, f)
        chall_dir = os.path.join(root, "Web", "One")
        os.makedirs(chall_dir)
        self.meta_path = os.path.join(chall_dir, "metadata.json")
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump({"id": 1, "name": "One", "category": "Web"}, f)

    def _service(self, verdict, success=False, message="blocked"):
        platform = MagicMock()
        platform.ctf_info.platform_type = "ctfd"
        platform.authenticate.return_value = True
        platform.last_verdict = verdict
        platform.submit_flag.return_value = (success, message)
        with patch(
            "ctf_downloader.services.submit_service.create_session",
            return_value=MagicMock(),
        ), patch(
            "ctf_downloader.services.submit_service.PlatformDetector.detect_platform",
            return_value=platform,
        ):
            svc = SubmitService(
                url="https://ctfd.test",
                workspace_dir=self.tmp.name,
                flag_format=r"^FLAG\{.+\}$",
            )
        return svc, platform

    def test_transient_verdict_is_not_written_to_history(self):
        svc, _ = self._service("auth_failed")
        ok, _ = svc.submit(1, "FLAG{x}")
        self.assertFalse(ok)
        hist = WorkspaceRepo(self.tmp.name).load_submit_history()
        self.assertEqual(hist["entries"], [])

    def test_already_solved_marks_solve_but_not_flag_as_correct(self):
        svc, _ = self._service("already_solved", success=True, message="already")
        ok, _ = svc.submit(1, "FLAG{maybe_not_checked}")
        self.assertTrue(ok)

        repo = WorkspaceRepo(self.tmp.name)
        status = repo.read_status(self.meta_path)
        self.assertEqual(status["solve"], "solved_by_me")
        self.assertNotEqual(status["flag"]["state"], "submitted_correct")
        self.assertEqual(repo.load_submit_history()["entries"], [])


if __name__ == "__main__":
    unittest.main()
