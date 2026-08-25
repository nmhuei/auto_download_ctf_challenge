"""
SP1 — Submit an toàn: unit tests (unittest + unittest.mock, không có network thật).

Chạy: python3 -m unittest test_sp1_submit.py -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.submitter import FlagSubmitter, NO_FORMAT_MESSAGE
from ctf_downloader.utils.flag_format import extract_flag_format, validate_flag
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def make_resp(status_code=200, json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no json")
    r.text = text if text != "" else (json.dumps(json_data) if json_data is not None else "")
    r.headers = headers or {}
    return r


def make_mock_platform(platform_type="ctfd"):
    p = MagicMock()
    p.ctf_info.platform_type = platform_type
    p.authenticate.return_value = True
    p.fetch_challenges.return_value = []
    p.fetch_rules.return_value = None
    p.submit_flag.return_value = (True, "ok")
    p.last_verdict = "correct"
    return p


def make_submitter(workspace_dir, platform=None, **kwargs):
    """Tạo FlagSubmitter với mọi network/detection được mock."""
    platform = platform or make_mock_platform()
    with patch("ctf_downloader.submitter.create_session", return_value=MagicMock()), \
         patch("ctf_downloader.submitter.PlatformDetector.detect_platform", return_value=platform):
        fs = FlagSubmitter(url="http://ctf.test", workspace_dir=workspace_dir, **kwargs)
    return fs, platform


class TempWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="sp1_ws_")
        # Workspace tối thiểu: challenges.json + 1 challenge (id=1)
        with open(os.path.join(self.ws, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "platform_url": "http://ctf.test",
                "ctf_info": {"url": "http://ctf.test"},
                "challenges": [{"id": 1, "name": "Chall One", "category": "Web"}],
            }, f)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_history(self, entries):
        with open(os.path.join(self.ws, "submit_history.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)

    def read_history(self):
        path = os.path.join(self.ws, "submit_history.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


# ----------------------------------------------------------------------
# utils/flag_format.py
# ----------------------------------------------------------------------

class TestExtractFlagFormat(unittest.TestCase):
    def test_explicit_regex_escaped(self):
        text = "All flags must match /^PTITCTF\\{.*\\}$/ exactly."
        self.assertEqual(extract_flag_format(text), "^PTITCTF\\{.+\\}$")

    def test_explicit_regex_plain_braces(self):
        text = "Flag format: /^FLAG{.*}$/g for every challenge."
        self.assertEqual(extract_flag_format(text), "^FLAG\\{.+\\}$")

    def test_code_span(self):
        text = "Mỗi flag có dạng `PTITCTF{abc_xyz}` nhé."
        self.assertEqual(extract_flag_format(text), "^PTITCTF\\{.+\\}$")

    def test_code_span_placeholder_returns_none(self):
        text = "Template: `FLAG{...}`"
        self.assertIsNone(extract_flag_format(text))

    def test_fallback_near_hint_word(self):
        text = "The flag looks like ptitctf{some_value} and is case sensitive."
        self.assertEqual(extract_flag_format(text), "^ptitctf\\{.+\\}$")

    def test_fallback_vietnamese_hint(self):
        text = "Định dạng flag của giải là ptitctf{hello_world}, viết thường."
        self.assertEqual(extract_flag_format(text), "^ptitctf\\{.+\\}$")

    def test_fallback_most_common_prefix(self):
        text = (
            "Every flag such as FLAG{abc123} counts.\n"
            "Another example FLAG{def456} works.\n"
            "Do not confuse it with CTF{x}.\n"
        )
        self.assertEqual(extract_flag_format(text), "^FLAG\\{.+\\}$")

    def test_no_format_returns_none(self):
        text = "This document contains absolutely nothing relevant at all."
        self.assertIsNone(extract_flag_format(text))

    def test_empty_returns_none(self):
        self.assertIsNone(extract_flag_format(""))
        self.assertIsNone(extract_flag_format(None))


class TestValidateFlag(unittest.TestCase):
    def test_valid_match(self):
        self.assertTrue(validate_flag("PTITCTF{abc_123}", "^PTITCTF\\{.+\\}$"))

    def test_wrong_prefix(self):
        self.assertFalse(validate_flag("FLAG{abc}", "^PTITCTF\\{.+\\}$"))

    def test_missing_braces(self):
        self.assertFalse(validate_flag("PTITCTFabc", "^PTITCTF\\{.+\\}$"))

    def test_empty_inputs(self):
        self.assertFalse(validate_flag("", "^X\\{.+\\}$"))
        self.assertFalse(validate_flag("X{y}", ""))

    def test_invalid_regex_returns_false(self):
        self.assertFalse(validate_flag("anything", "[invalid"))


# ----------------------------------------------------------------------
# Platform verdicts + fetch_rules
# ----------------------------------------------------------------------

class TestCTFdPlatformVerdicts(unittest.TestCase):
    def _platform(self):
        sess = MagicMock()
        sess.headers = {}
        p = CTFdPlatform("http://ctfd.test", sess)
        p.nonce = "nonce"  # bỏ qua extract nonce (network)
        return p

    def test_correct_verdict(self):
        p = self._platform()
        p.session.post.return_value = make_resp(200, {"success": True, "data": {"status": "correct"}})
        ok, msg = p.submit_flag(1, "FLAG{x}")
        self.assertTrue(ok)
        self.assertEqual(p.last_verdict, "correct")

    def test_incorrect_verdict(self):
        p = self._platform()
        p.session.post.return_value = make_resp(200, {"success": True, "data": {"status": "incorrect"}})
        ok, msg = p.submit_flag(1, "FLAG{x}")
        self.assertFalse(ok)
        self.assertEqual(p.last_verdict, "incorrect")

    def test_ratelimited_verdict(self):
        p = self._platform()
        p.session.post.return_value = make_resp(200, {"success": True, "data": {"status": "ratelimited"}})
        ok, msg = p.submit_flag(1, "FLAG{x}")
        self.assertFalse(ok)
        self.assertIn("Rate limited", msg)
        self.assertEqual(p.last_verdict, "ratelimited")

    def test_content_type_header_with_token(self):
        sess = MagicMock()
        sess.headers = {"Authorization": "Token ctfd_secret"}
        CTFdPlatform("http://ctfd.test", sess)
        self.assertEqual(sess.headers.get("Content-Type"), "application/json")

    def test_no_token_no_content_type_forced(self):
        sess = MagicMock()
        sess.headers = {}
        CTFdPlatform("http://ctfd.test", sess)
        self.assertNotIn("Content-Type", sess.headers)

    def test_fetch_rules_skips_404_then_finds_page(self):
        sess = MagicMock()
        p = CTFdPlatform("http://ctfd.test", sess)
        page_404 = make_resp(404, text="")
        rules_html = "<html><title>Rules</title><body>" + ("x" * 300) + "</body></html>"
        rules_resp = make_resp(200, text=rules_html, headers={"content-type": "text/html"})
        sess.get.side_effect = [page_404, rules_resp]
        result = p.fetch_rules()
        self.assertEqual(result, rules_html)
        self.assertEqual(sess.get.call_count, 2)

    def test_fetch_rules_ignores_theme_404_pages(self):
        sess = MagicMock()
        p = CTFdPlatform("http://ctfd.test", sess)
        fake_404 = "<html><title>Page Not Found - CTFd</title><body>" + ("y" * 300) + "</body></html>"
        sess.get.side_effect = [
            make_resp(200, text=fake_404, headers={"content-type": "text/html"})
            for _ in CTFdPlatform.RULE_PAGE_SLUGS
        ]
        self.assertIsNone(p.fetch_rules())
        self.assertEqual(sess.get.call_count, len(CTFdPlatform.RULE_PAGE_SLUGS))


class TestGZCTFPlatform(unittest.TestCase):
    def test_game_id_from_path(self):
        p = GZCTFPlatform("http://gz.test/games/6/challenges", MagicMock())
        self.assertEqual(p.game_id, 6)
        self.assertEqual(p.base_url, "http://gz.test")

    def test_game_id_from_gid_query(self):
        p = GZCTFPlatform("https://gz.test/?gid=7", MagicMock())
        self.assertEqual(p.game_id, 7)

    def test_game_id_none_when_unknown(self):
        p = GZCTFPlatform("https://gz.test/some/other/page", MagicMock())
        self.assertIsNone(p.game_id)

    def test_authenticate_never_probes_game_ids(self):
        sess = MagicMock()
        sess.get.return_value = make_resp(200, {"userName": "player1"})
        p = GZCTFPlatform("https://gz.test", sess)
        self.assertTrue(p.authenticate())
        for call in sess.get.call_args_list:
            self.assertNotIn("/api/game/", str(call))

    def test_fetch_rules_from_game_content(self):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test/games/3/challenges", sess)
        sess.get.return_value = make_resp(200, {"title": "Game", "content": "# Rules\nGG{...}"})
        self.assertEqual(p.fetch_rules(), "# Rules\nGG{...}")
        sess.get.assert_called_once_with("http://gz.test/api/game/3", timeout=15)

    def test_fetch_rules_none_when_no_game_id(self):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test", sess)
        self.assertIsNone(p.fetch_rules())
        sess.get.assert_not_called()

    def test_submit_poll_accepted(self):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test/games/6/challenges", sess)
        sess.post.return_value = make_resp(200, text="123")
        sess.get.return_value = make_resp(200, text='"Accepted"')
        ok, msg = p.submit_flag(10, "GG{flag}")
        self.assertTrue(ok)
        self.assertEqual(p.last_verdict, "correct")

    @patch("ctf_downloader.platforms.gzctf.time.sleep")
    def test_submit_poll_wrong_answer(self, mock_sleep):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test/games/6/challenges", sess)
        sess.post.return_value = make_resp(200, text="123")
        sess.get.side_effect = [make_resp(200, text='"Grading"'), make_resp(200, text='"WrongAnswer"')]
        ok, msg = p.submit_flag(10, "GG{wrong}")
        self.assertFalse(ok)
        self.assertEqual(p.last_verdict, "incorrect")
        mock_sleep.assert_called_once()

    @patch("ctf_downloader.platforms.gzctf.time.sleep")
    def test_submit_unknown_when_poll_exhausted(self, mock_sleep):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test/games/6/challenges", sess)
        sess.post.return_value = make_resp(200, text="999")
        sess.get.return_value = make_resp(200, text='"Grading"')
        ok, msg = p.submit_flag(10, "GG{mystery}")
        self.assertFalse(ok)
        self.assertEqual(p.last_verdict, "unknown")
        self.assertEqual(mock_sleep.call_count, GZCTFPlatform.SUBMISSION_POLL_ATTEMPTS)

    def test_submit_ratelimited(self):
        sess = MagicMock()
        p = GZCTFPlatform("http://gz.test/games/6/challenges", sess)
        sess.post.return_value = make_resp(429, text="too fast")
        ok, msg = p.submit_flag(10, "GG{x}")
        self.assertEqual(p.last_verdict, "ratelimited")


class TestRCTFPlatformVerdicts(unittest.TestCase):
    def _platform(self):
        return RCTFPlatform("http://rctf.test", MagicMock())

    def test_good_flag(self):
        p = self._platform()
        p.session.post.return_value = make_resp(200, {"kind": "goodFlag"})
        ok, msg = p.submit_flag("chal1", "flag{x}")
        self.assertTrue(ok)
        self.assertEqual(p.last_verdict, "correct")

    def test_bad_flag(self):
        p = self._platform()
        p.session.post.return_value = make_resp(200, {"kind": "badFlag", "message": "nope"})
        ok, msg = p.submit_flag("chal1", "flag{bad}")
        self.assertFalse(ok)
        self.assertEqual(p.last_verdict, "incorrect")

    def test_fetch_rules_returns_none(self):
        p = self._platform()
        self.assertIsNone(p.fetch_rules())
        p.session.get.assert_not_called()


# ----------------------------------------------------------------------
# FlagSubmitter: format gate, cache, blacklist, throttle
# ----------------------------------------------------------------------

class TestSubmitFormatGate(TempWorkspaceCase):
    def test_lock_when_no_format_and_not_tty(self):
        fs, platform = make_submitter(self.ws)
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")):
            succ, msg = fs.submit(1, "ANYTHING{abc}")
        self.assertFalse(succ)
        self.assertEqual(msg, NO_FORMAT_MESSAGE)
        platform.submit_flag.assert_not_called()

    def test_flag_must_match_format(self):
        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        succ, msg = fs.submit(1, "WRONGPREFIX{abc}")
        self.assertFalse(succ)
        self.assertIn("không khớp định dạng", msg)
        platform.submit_flag.assert_not_called()

    def test_cache_round_trip_from_rules(self):
        platform = make_mock_platform()
        platform.fetch_rules.return_value = (
            "# Rules\nEvery flag must match /^TEST\\{.*\\}$/ to be accepted."
        )
        platform.submit_flag.return_value = (True, "🎉 Correct!")
        platform.last_verdict = "correct"
        fs, _ = make_submitter(self.ws, platform=platform)

        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ, msg = fs.submit(1, "TEST{hello}")

        self.assertTrue(succ)
        # Cache đã được ghi vào challenges.json
        with open(os.path.join(self.ws, "challenges.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["ctf_info"]["flag_format"], "^TEST\\{.+\\}$")
        self.assertEqual(data["ctf_info"]["flag_format_source"], "rules")

        # Instance mới đọc từ cache, không cần gọi fetch_rules nữa
        platform2 = make_mock_platform()
        platform2.fetch_rules.side_effect = AssertionError("fetch_rules must not be called (cache hit)")
        platform2.submit_flag.return_value = (True, "ok")
        platform2.last_verdict = "correct"
        fs2, _ = make_submitter(self.ws, platform=platform2)
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ2, _ = fs2.submit(1, "TEST{world}")
        self.assertTrue(succ2)
        platform2.fetch_rules.assert_not_called()


class TestBlacklist(TempWorkspaceCase):
    def test_incorrect_flag_blocked_and_force_overrides(self):
        self.write_history([{
            "flag": "TEST{bad}", "challenge_id": 99,
            "result": "incorrect", "timestamp": "2026-08-24T00:00:00Z",
        }])
        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        platform.submit_flag.return_value = (True, "ok")
        platform.last_verdict = "correct"

        # Bị chặn vì flag đã SAI trước đó
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ, msg = fs.submit(1, "TEST{bad}")
        self.assertFalse(succ)
        self.assertIn("blacklist", msg)
        platform.submit_flag.assert_not_called()

        # force=True vượt blacklist và cập nhật lịch sử theo kết quả mới
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ, msg = fs.submit(1, "TEST{bad}", force=True)
        self.assertTrue(succ)
        platform.submit_flag.assert_called_once()
        hist = self.read_history()
        entry = [e for e in hist["entries"] if e["flag"] == "TEST{bad}"][0]
        self.assertEqual(entry["result"], "correct")
        self.assertEqual(str(entry["challenge_id"]), "1")

    def test_already_solved_skipped(self):
        self.write_history([{
            "flag": "TEST{good}", "challenge_id": 1,
            "result": "correct", "timestamp": "2026-08-24T00:00:00Z",
        }])
        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ, msg = fs.submit(1, "TEST{good}")
        self.assertFalse(succ)
        self.assertIn("Đã solved", msg)
        platform.submit_flag.assert_not_called()

    def test_ratelimited_not_recorded(self):
        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        platform.submit_flag.return_value = (False, "⏳ Rate limited!")
        platform.last_verdict = "ratelimited"
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            succ, msg = fs.submit(1, "TEST{rl}")
        self.assertEqual(platform.last_verdict, "ratelimited")
        hist = self.read_history()
        self.assertTrue(hist is None or hist["entries"] == [])

    def test_corrupt_history_backed_up(self):
        hist_path = os.path.join(self.ws, "submit_history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            f.write("{corrupt!!! json")
        fs, _ = make_submitter(self.ws)
        self.assertEqual(fs.submit_history, [])
        self.assertTrue(os.path.exists(hist_path + ".bak"))


class TestThrottle(TempWorkspaceCase):
    @patch("ctf_downloader.submitter.time.sleep")
    def test_sleep_only_between_submits_and_bounded(self, mock_sleep):
        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        platform.ctf_info.platform_type = "ctfd"  # min gap 6s
        platform.submit_flag.return_value = (True, "ok")
        platform.last_verdict = "correct"

        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False):
            fs.submit(1, "TEST{aaa}")
            fs.submit(1, "TEST{bbb}")

        # Chỉ sleep giữa 2 lần submit (không sleep lần đầu)
        self.assertEqual(mock_sleep.call_count, 1)
        waited = mock_sleep.call_args[0][0]
        self.assertGreater(waited, 0)
        self.assertLess(waited, 7)


class TestAutoScan(TempWorkspaceCase):
    def test_tightened_candidates_and_stats(self):
        # Challenge chưa solve với README chứa cả rác lẫn flag thật
        chall_dir = os.path.join(self.ws, "Web", "Chall_Two")
        os.makedirs(chall_dir, exist_ok=True)
        with open(os.path.join(chall_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump({"id": 2, "name": "Chall Two", "solved_by_me": False}, f)
        with open(os.path.join(chall_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write("randomjunk{should_be_ignored}\nreal: TEST{goodone}\n")

        # Blacklist một flag xuất hiện trong README
        hist_path = os.path.join(self.ws, "submit_history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump({"entries": [{
                "flag": "TEST{blacklisted}", "challenge_id": 42,
                "result": "incorrect", "timestamp": "t",
            }]}, f)
        with open(os.path.join(chall_dir, "flag.txt"), "w", encoding="utf-8") as f:
            f.write("TEST{blacklisted}\n")

        fs, platform = make_submitter(self.ws, flag_format="^TEST\\{.+\\}$")
        platform.submit_flag.return_value = (True, "ok")
        platform.last_verdict = "correct"

        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"):
            results = fs.auto_scan_and_submit()

        # Prefix lạ bị loại khỏi candidate; chỉ TEST{blacklisted} (chặn trước) và TEST{goodone}
        flags_tried = [r["flag"] for r in results if r["message"] not in ("unprocessed",)]
        self.assertNotIn("randomjunk{should_be_ignored}", flags_tried)
        submitted = [r for r in results if r["category"] == "submitted_ok"]
        blacklisted = [r for r in results if r["category"] == "skipped_blacklisted"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["flag"], "TEST{goodone}")
        self.assertEqual(len(blacklisted), 1)
        platform.submit_flag.assert_called_once()


if __name__ == "__main__":
    unittest.main()
