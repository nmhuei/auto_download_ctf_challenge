"""
Status Model đa chiều — unit/integration tests (spec challenge-status-model §8).

Chạy: python3 -m pytest test_status_model.py -q
Toàn bộ HTTP được mock — KHÔNG gọi mạng thật.
"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from ctf_downloader.platforms.base import SolveAttribution
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.services.status_service import StatusService
from ctf_downloader.storage.constants import STATUS_ICONS
from ctf_downloader.storage.workspace_repo import WorkspaceRepo, normalize_status


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


def make_mock_session(routes):
    """routes: list của (substring_url, resp). Khớp cái đầu tiên chứa URL."""
    s = MagicMock()

    def get(url, *a, **kw):
        for frag, resp in routes:
            if frag in url:
                return resp
        return make_resp(404)

    s.get.side_effect = get
    return s


def make_workspace(root: pathlib.Path):
    """Workspace tối thiểu: 1 challenge Web id=1, chưa giải."""
    d = root / "Web" / "chall_a"
    d.mkdir(parents=True, exist_ok=True)
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "StatusCTF", "url": "https://s.example.com",
                     "platform": "gzctf"},
        "challenges": [{"id": 1, "name": "Chall A", "category": "Web", "points": 100}],
    }), encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps({
        "id": 1, "name": "Chall A", "category": "Web", "points": 100,
        "solved_by_me": False,
    }), encoding="utf-8")
    (d / "writeup").mkdir(exist_ok=True)
    (d / "writeup" / "README.md").write_text(
        "# Chall A\n- [ ] Solved\nFlag: `FLAG{...}`\n", encoding="utf-8")
    return root


class TempWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="status_model_")
        self.root = pathlib.Path(self._tmp) / "ws"
        self.root.mkdir()
        make_workspace(self.root)
        self.repo = WorkspaceRepo(self.root)
        self.meta_path = self.root / "Web" / "chall_a" / "metadata.json"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# 1. Normalize / migrate-on-read
# ----------------------------------------------------------------------

class TestNormalize(unittest.TestCase):
    def test_defaults_on_garbage(self):
        for garbage in (None, {}, "oops", 42, {"solve": "bogus", "flag": "notadict"}):
            st = normalize_status(garbage)
            self.assertEqual(st["schema_version"], 2)
            self.assertEqual(st["solve"], "unsolved")
            self.assertEqual(st["flag"], {"value": None, "state": "none"})
            self.assertEqual(st["writeup"], "none")
            self.assertTrue(st["writeup_auto"])
            self.assertEqual(st["container"], "none")

    def test_valid_values_preserved(self):
        st = normalize_status({"solve": "working", "notes": "đang bypass",
                               "labels": ["todo", "hard"]})
        self.assertEqual(st["solve"], "working")
        self.assertEqual(st["notes"], "đang bypass")
        self.assertEqual(st["labels"], ["todo", "hard"])


class TestMigrateOnRead(TempWorkspaceCase):
    def test_legacy_bool_solved(self):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["solved_by_me"] = True
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["solve"], "solved_by_me")
        # migrate-on-read không ghi file (không convert batch)
        self.assertNotIn("status", json.loads(self.meta_path.read_text(encoding="utf-8")))

    def test_marker_readme_solved(self):
        wp = self.meta_path.parent / "writeup" / "README.md"
        wp.write_text("# Chall A\n- [x] Solved\n", encoding="utf-8")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["solve"], "solved_by_me")

    def test_placeholder_replaced_found_unverified(self):
        wp = self.meta_path.parent / "writeup" / "README.md"
        wp.write_text("# Chall A\n- [ ] Solved\nFlag: `PTITCTF{real_one}`\n", encoding="utf-8")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["flag"]["state"], "found_unverified")
        self.assertEqual(st["flag"]["value"], "PTITCTF{real_one}")

    def test_instance_info_container_stopped(self):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["instance_info"] = {"is_container": True}
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["container"], "stopped")

    def test_running_not_downgraded(self):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["instance_info"] = {"is_container": True, "status": "running"}
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["container"], "running")


# ----------------------------------------------------------------------
# 2. update_status — mirror, stamp, toggle marker, lock đa tiến trình
# ----------------------------------------------------------------------

class TestUpdateStatus(TempWorkspaceCase):
    def test_mutate_and_mirror(self):
        def mut(st):
            st["solve"] = "solved_by_me"
            st["notes"] = "SSTI escape"
            return st

        out = self.repo.update_status(self.meta_path, mut)
        self.assertEqual(out["solve"], "solved_by_me")
        self.assertEqual(out["notes"], "SSTI escape")
        self.assertTrue(out["updated_at"])

        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertTrue(meta["solved_by_me"])          # mirror legacy
        self.assertEqual(meta["status"]["schema_version"], 2)

        wp = (self.meta_path.parent / "writeup" / "README.md").read_text(encoding="utf-8")
        self.assertIn("- [x] Solved", wp)              # marker được tick

    def test_unsolve_unticks_marker(self):
        self.repo.update_status(self.meta_path, lambda st: {**st, "solve": "solved_by_me"})
        self.repo.update_status(self.meta_path, lambda st: {**st, "solve": "unsolved"})
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertFalse(meta["solved_by_me"])
        wp = (self.meta_path.parent / "writeup" / "README.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] Solved", wp)

    def test_multiprocess_lock_no_lost_update(self):
        worker = (
            "import sys\n"
            f"sys.path.insert(0, {os.getcwd()!r})\n"
            "from ctf_downloader.storage.workspace_repo import WorkspaceRepo\n"
            f"repo = WorkspaceRepo({str(self.root)!r})\n"
            f"meta = {str(self.meta_path)!r}\n"
            "for _ in range(15):\n"
            "    repo.update_status(meta, lambda st: {**st, 'labels': st['labels'] + ['x']})\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", worker]) for _ in range(2)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(len(meta["status"]["labels"]), 30)


# ----------------------------------------------------------------------
# 3. WriteupAssessor
# ----------------------------------------------------------------------

TEMPLATE = """# Writeup: Chall A

## 🔍 Reconnaissance & Vulnerability Analysis
*(Document reverse engineering here)*

## 💻 Exploitation Strategy & PoC
Exploit script is located at [`../solver/solve.py`](../solver/solve.py).

```bash
python3 ../solver/solve.py
```

## 🚩 Flag

- Status: `- [ ] Solved`
- Flag: `FLAG{...}`
"""

FILLED_WRITEUP = """# Writeup: Chall A

## 🔍 Reconnaissance & Vulnerability Analysis
Ứng dụng dùng Jinja2 render template từ tham số người dùng. Sau khi fuzz tham
 số page, ta thấy lỗi SSTI khi payload {{7*7}} trả về 49. Bộ lọc chặn dấu ngoặc
 nhọn kép nhưng không chặn {% raw %} block, nên có thể bypass bằng chuỗi khác.

## 💻 Exploitation Strategy & PoC

Dùng payload {%raw%} để đọc biến config và lấy secret key:

```python
import requests
url = "http://target/render"
payload = "{%raw%}{{config}}{%endraw%}"
r = requests.get(url, params={"page": payload})
print(r.text)
```

$ curl http://target/render?page=test

## 🚩 Flag

- Status: `- [x] Solved`
- Flag: `FLAG{ssti_escape_done}`
"""


class TestWriteupAssessor(unittest.TestCase):
    def _assess(self, md, **kw):
        from ctf_downloader.utils.writeup_assessor import assess_writeup
        return assess_writeup(md, flag_format=r"^FLAG\{.+\}$", **kw)

    def test_intact_template_is_skeleton(self):
        res = self._assess(TEMPLATE, reference_template=TEMPLATE)
        self.assertEqual(res["status"], "skeleton")
        self.assertEqual(res["score"], 0)

    def test_filled_writeup_is_complete(self):
        res = self._assess(FILLED_WRITEUP, reference_template=TEMPLATE)
        self.assertEqual(res["status"], "complete")
        self.assertGreaterEqual(res["score"], 70)
        self.assertTrue(res["signals"].get("has_real_flag"))

    def test_missing_flag_stays_draft(self):
        no_flag = FILLED_WRITEUP.replace("FLAG{ssti_escape_done}", "FLAG{...}")
        res = self._assess(no_flag)
        self.assertEqual(res["status"], "draft")
        self.assertFalse(res["signals"].get("has_real_flag"))
        self.assertTrue(res["missing"])   # có gợi ý tiếng Việt

    def test_handwritten_without_reference_not_skeleton(self):
        hand = "# Writeup tay hoàn toàn mới\n\nNội dung tự viết, không theo template.\n"
        res = self._assess(hand)
        self.assertNotEqual(res["status"], "skeleton")

    def test_anchored_format_matches_flag_mid_document(self):
        # Fix review: pattern anchored ^...$ phải khớp flag nằm giữa tài liệu
        # nhiều dòng (mốc +30 flag_format không được "chết").
        mid_doc = TEMPLATE.replace(
            "- Flag: `FLAG{...}`",
            "- Flag: `FLAG{...}`\nFlag thật bắt được: `FLAG{mid_document_hit}` ngay giữa bài.")
        res = self._assess(mid_doc, reference_template=TEMPLATE)
        self.assertTrue(res["signals"].get("flag_format_matched"),
                        "anchored flag_format phải match flag giữa văn bản")
        self.assertTrue(res["signals"].get("has_real_flag"))


# ----------------------------------------------------------------------
# 4. Solve attribution parsers (mock JSON shape đã verify trong spec §4)
# ----------------------------------------------------------------------

class TestGZCTFAttribution(TempWorkspaceCase):
    def _platform(self, extra_routes=()):
        routes = list(extra_routes) + [
            ("/api/team/42", make_resp(json_data={
                "members": [{"userName": "me_user"}, {"userName": "memberB"}]}),
            ),
            ("/api/game/6/scoreboard", make_resp(json_data={
                "items": [
                    {"rank": 1, "id": 777, "name": "OtherTeam", "score": 9000,
                     "solvedChallenges": [
                         {"id": 1, "title": "Chall A", "userName": "someone_else",
                          "time": "2026-08-24T10:00:00+07:00"}]},
                    {"rank": 2, "id": 42, "name": "TeamX", "score": 500,
                     "solvedChallenges": [
                         {"id": 1, "title": "Chall A", "userName": "memberB",
                          "time": "2026-08-24T11:30:00Z"},
                         {"id": 9, "title": "Solo", "userName": "me_user",
                          "time": "2026-08-24T12:00:00Z"}]},
                ]}),
            ),
        ]
        plat = GZCTFPlatform("https://gz.example.com/games/6/challenges",
                             make_mock_session(routes))
        plat.ctf_info.user_name = "me_user"
        plat.ctf_info.team_name = "TeamX"
        return plat

    def test_by_me_vs_by_team_and_membership_confirm(self):
        plat = self._platform()
        result = plat.fetch_solve_attribution([1, 9])
        self.assertTrue(result[1].by_team)
        self.assertFalse(result[1].by_me)         # memberB submit, không phải mình
        self.assertIn("memberB", result[1].solver_names)
        self.assertTrue(result[9].by_me)
        self.assertIsNotNone(result[1].solved_at)  # epoch-ms

    def test_same_name_team_rejected_via_members(self):
        # TeamX trên scoreboard chỉ chứa solve của memberB (không có solve
        # của mình) -> match thuần theo tên đội; membership qua /team/42
        # phải loại trường hợp trùng tên của người khác.
        routes = [
            ("/api/team/42", make_resp(json_data={
                "members": [{"userName": "stranger"}]})),
            ("/api/game/6/scoreboard", make_resp(json_data={
                "items": [
                    {"rank": 2, "id": 42, "name": "TeamX", "score": 500,
                     "solvedChallenges": [
                         {"id": 1, "title": "Chall A", "userName": "memberB",
                          "time": "2026-08-24T11:30:00Z"}]},
                ]})),
        ]
        plat = GZCTFPlatform("https://gz.example.com/games/6/challenges",
                             make_mock_session(routes))
        plat.ctf_info.user_name = "me_user"
        plat.ctf_info.team_name = "TeamX"
        result = plat.fetch_solve_attribution([1])
        self.assertEqual(result, {})   # đội trùng tên nhưng mình không ở trong

    def test_scoreboard_error_falls_back_to_details(self):
        plat = self._platform(extra_routes=[
            ("/api/game/6/details", make_resp(json_data={
                "challenges": {"Web": [{"id": 1, "solvedByMe": True}]}})),
        ])
        plat.session.get.side_effect = None
        routes = [
            ("/api/game/6/scoreboard", make_resp(400, json_data={})),
            ("/api/game/6/details", make_resp(json_data={
                "challenges": {"Web": [{"id": 1, "solvedByMe": True}]}})),
        ]
        plat.session = make_mock_session(routes)
        result = plat.fetch_solve_attribution([1])
        self.assertTrue(result[1].by_team)
        self.assertFalse(result[1].by_me)


class TestCTFdAttribution(TempWorkspaceCase):
    def _routes_teams_mode(self):
        return [
            ("/api/v1/users/me", make_resp(json_data={
                "success": True, "data": {"id": 7, "name": "me_user"}})),
            ("/api/v1/teams/me/solves", make_resp(json_data={
                "success": True, "data": [
                    {"challenge_id": 1, "user": {"id": 99, "name": "teammate"},
                     "date": "2026-08-24T09:00:00+00:00"},
                    {"challenge_id": 2, "user": {"id": 7, "name": "me_user"},
                     "date": "2026-08-24T09:05:00+00:00"},
                ]})),
            ("/api/v1/teams/me", make_resp(json_data={
                "success": True, "data": {"id": 5, "name": "TeamX"}})),
        ]

    def test_teams_mode_row_user_attribution(self):
        plat = CTFdPlatform("https://ctfd.example.com",
                            make_mock_session(self._routes_teams_mode()))
        plat.ctf_info.user_name = "me_user"
        result = plat.fetch_solve_attribution([1, 2])
        self.assertFalse(result[1].by_me)
        self.assertTrue(result[1].by_team)
        self.assertEqual(result[1].solver_names, ["teammate"])
        self.assertTrue(result[2].by_me)

    def test_users_mode_fallback(self):
        routes = [
            ("/api/v1/users/me/solves", make_resp(json_data={
                "success": True, "data": [{"challenge_id": 3}]})),
            ("/api/v1/users/me", make_resp(json_data={
                "success": True, "data": {"id": 7, "name": "me_user"}})),
            ("/api/v1/teams/me", make_resp(404, json_data={})),
        ]
        plat = CTFdPlatform("https://ctfd.example.com", make_mock_session(routes))
        result = plat.fetch_solve_attribution([3])
        self.assertTrue(result[3].by_me)
        self.assertTrue(result[3].by_team)   # users mode: 1 account = 1 team

    def test_exception_returns_empty(self):
        sess = MagicMock()
        sess.get.side_effect = RuntimeError("boom")
        plat = CTFdPlatform("https://ctfd.example.com", sess)
        self.assertEqual(plat.fetch_solve_attribution([1]), {})

    def test_me_id_unknown_is_fail_safe_not_by_me(self):
        # Fix review: /users/me lỗi (me_id=None) mà teams mode còn sống ->
        # solve của đồng đội KHÔNG được đánh dấu by_me (tránh kẹt solved_by_me sai).
        routes = [
            ("/api/v1/users/me", make_resp(500, json_data={})),
            ("/api/v1/teams/me/solves", make_resp(json_data={
                "success": True, "data": [
                    {"challenge_id": 1, "user": {"id": 99, "name": "teammate"}}]})),
            ("/api/v1/teams/me", make_resp(json_data={
                "success": True, "data": {"id": 5, "name": "TeamX"}})),
        ]
        plat = CTFdPlatform("https://ctfd.example.com", make_mock_session(routes))
        plat.ctf_info.user_name = "me_user"
        result = plat.fetch_solve_attribution([1])
        self.assertFalse(result[1].by_me)
        self.assertTrue(result[1].by_team)          # team vẫn ăn solved_by_team
        self.assertEqual(result[1].solver_names, ["teammate"])


class TestRCTFAttribution(TempWorkspaceCase):
    def test_by_team_equals_by_me_and_first_blood(self):
        routes = [
            ("/api/v1/users/me", make_resp(json_data={
                "kind": "goodUserData",
                "data": {"name": "me_user", "solves": [{"chalId": 1}]}})),
            ("/api/v1/challs/1/solves", make_resp(json_data={
                "kind": "goodSolves",
                "data": [
                    {"user": {"name": "me_user"}, "ts": "2026-08-24T08:00:00Z"},
                    {"user": {"name": "other"}, "ts": "2026-08-24T08:10:00Z"},
                ]})),
        ]
        plat = RCTFPlatform("https://rctf.example.com", make_mock_session(routes))
        result = plat.fetch_solve_attribution([1])
        self.assertTrue(result[1].by_me)
        self.assertTrue(result[1].by_team)
        self.assertTrue(result[1].first_blood)   # mình là solver sớm nhất
        self.assertIn("other", result[1].solver_names)

    def test_duplicate_solve_keeps_earliest_timestamp(self):
        # Fix review: nhiều dòng solve cùng challId -> giữ mốc SỚM NHẤT.
        routes = [
            ("/api/v1/users/me", make_resp(json_data={
                "kind": "goodUserData",
                "data": {"name": "me_user",
                         "solves": [
                             {"chalId": 1, "ts": "2026-08-24T10:00:00Z"},
                             {"chalId": 1, "ts": "2026-08-24T08:30:00Z"},
                         ]}})),
            ("/api/v1/challs/1/solves", make_resp(404, json_data={})),
        ]
        plat = RCTFPlatform("https://rctf.example.com", make_mock_session(routes))
        result = plat.fetch_solve_attribution([1])
        from ctf_downloader.platforms.base import epoch_ms
        self.assertEqual(result[1].solved_at, epoch_ms("2026-08-24T08:30:00Z"))


class TestGZCTFFailSafeMembership(TempWorkspaceCase):
    def test_unverifiable_team_is_rejected_not_fail_open(self):
        # Fix review: /api/team/{id} lỗi cả retry -> KHÔNG chấp nhận đội
        # trùng tên (fail-safe), fallback /details cũng không có -> {}.
        routes = [
            ("/api/team/42", make_resp(500, json_data={})),
            ("/api/game/6/scoreboard", make_resp(json_data={
                "items": [
                    {"rank": 2, "id": 42, "name": "TeamX", "score": 500,
                     "solvedChallenges": [
                         {"id": 1, "title": "Chall A", "userName": "memberB"}]},
                ]})),
        ]
        plat = GZCTFPlatform("https://gz.example.com/games/6/challenges",
                             make_mock_session(routes))
        plat.ctf_info.user_name = "me_user"
        plat.ctf_info.team_name = "TeamX"
        self.assertEqual(plat.fetch_solve_attribution([1]), {})


# ----------------------------------------------------------------------
# 5. Integration: submit verdict -> status chain
# ----------------------------------------------------------------------

def make_submitter(workspace_dir, platform=None):
    from ctf_downloader.submitter import FlagSubmitter
    platform = platform or MagicMock()
    platform.ctf_info.platform_type = "gzctf"
    platform.authenticate.return_value = True
    platform.fetch_challenges.return_value = []
    platform.submit_flag.return_value = (True, "ok")
    platform.last_verdict = "correct"
    with patch("ctf_downloader.submitter.create_session", return_value=MagicMock()), \
         patch("ctf_downloader.submitter.PlatformDetector.detect_platform",
               return_value=platform):
        fs = FlagSubmitter(url="http://ctf.test", workspace_dir=str(workspace_dir),
                           flag_format=r"^FLAG\{.+\}$")
    return fs, platform


class TestSubmitVerdictChain(TempWorkspaceCase):
    def _submitter_with_cache(self, verdict):
        fs, platform = make_submitter(self.root)
        platform.last_verdict = verdict
        platform.submit_flag.return_value = (verdict == "correct", "msg")
        fs.challenges_cache = {
            "1": {"id": 1, "name": "Chall A"},
            "chall a": {"id": 1, "name": "Chall A"},
        }
        return fs, platform

    def test_correct_updates_full_chain(self):
        fs, _p = self._submitter_with_cache("correct")
        ok, _msg = fs.submit(1, "FLAG{good}")
        self.assertTrue(ok)
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["flag"]["state"], "submitted_correct")
        self.assertEqual(st["flag"]["value"], "FLAG{good}")
        self.assertEqual(st["solve"], "solved_by_me")
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertTrue(meta["solved_by_me"])
        hist = self.repo.load_submit_history()["entries"]
        self.assertEqual(hist[-1]["result"], "correct")

    def test_incorrect_marks_wrong_keeps_value(self):
        fs, _p = self._submitter_with_cache("incorrect")
        fs.submit(1, "FLAG{bad}")
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["flag"]["state"], "submitted_wrong")
        self.assertEqual(st["flag"]["value"], "FLAG{bad}")
        self.assertEqual(st["solve"], "unsolved")   # không nâng

    def test_unknown_touches_nothing(self):
        before = self.repo.read_status(self.meta_path)
        fs, _p = self._submitter_with_cache("unknown")
        fs.submit(1, "FLAG{meh}")
        after = self.repo.read_status(self.meta_path)
        self.assertEqual(before, after)

    def test_hoard_flag_ready_for_cli(self):
        fs, _p = self._submitter_with_cache("correct")
        ok, _msg = fs.hoard_flag(1, "FLAG{stashed}")
        self.assertTrue(ok)
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{stashed}")


# ----------------------------------------------------------------------
# 6. Pull attribution sync — chỉ nâng không hạ
# ----------------------------------------------------------------------

class TestPullAttributionSync(TempWorkspaceCase):
    def _sync(self, attr):
        platform = MagicMock()
        platform.fetch_solve_attribution.return_value = attr
        return PullService.sync_solve_attribution(platform, str(self.root))

    def test_raise_to_solved_by_team(self):
        n = self._sync({1: SolveAttribution(by_team=True, solver_names=["mate"])})
        self.assertEqual(n, 1)
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["solve"], "solved_by_team")
        self.assertTrue(st["synced_at"])

    def test_never_downgrades_higher_local_state(self):
        self.repo.update_status(self.meta_path,
                                lambda st: {**st, "solve": "solved_by_me"})
        n = self._sync({1: SolveAttribution(by_team=True)})
        self.assertEqual(n, 0)   # không hạ solved_by_me -> solved_by_team
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["solve"], "solved_by_me")

    def test_platform_without_support_is_noop(self):
        class NoAttrPlatform:
            pass   # không có fetch_solve_attribution

        self.assertEqual(
            PullService.sync_solve_attribution(NoAttrPlatform(), str(self.root)), 0)

    def test_fetcher_exception_is_swallowed(self):
        platform = MagicMock()
        platform.fetch_solve_attribution.side_effect = RuntimeError("boom")
        self.assertEqual(
            PullService.sync_solve_attribution(platform, str(self.root)), 0)


# ----------------------------------------------------------------------
# 7. Render icon đa chiều (snapshot test)
# ----------------------------------------------------------------------

class TestRenderIcons(TempWorkspaceCase):
    def _render(self, **kw) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            StatusService.render_tree(self.repo, **kw)
        return buf.getvalue()

    def test_badges_and_header_stats_present(self):
        self.repo.update_status(self.meta_path, lambda st: {
            **st, "solve": "solved_by_me", "writeup": "draft",
            "flag": {"value": "FLAG{x}", "state": "hoarded"},
            "notes": "SSTI sandbox escape — đang bypass",
        })
        out = self._render()
        # Badge đa chiều theo bảng icon spec §6
        self.assertIn(f"[{STATUS_ICONS['solve']['solved_by_me']}]", out)
        self.assertIn(f"[{STATUS_ICONS['flag']['hoarded']}]", out)
        self.assertIn(f"[{STATUS_ICONS['writeup']['draft']}]", out)
        # Header thống kê đầy đủ
        self.assertIn("📊 Progress:", out)
        self.assertIn("💰 Points:", out)
        self.assertIn("🏴 Hoarded:", out)
        self.assertIn("📝 Drafts:", out)
        self.assertIn("📦 Files:", out)
        # Dòng note
        self.assertIn('"SSTI sandbox escape — đang bypass"', out)

    def test_unsolved_default_icons(self):
        out = self._render()
        self.assertIn(f"[{STATUS_ICONS['solve']['unsolved']}]", out)
        self.assertIn(f"[{STATUS_ICONS['flag']['none']}]", out)
        self.assertIn(f"[{STATUS_ICONS['writeup']['none']}]", out)

    def test_container_icon_for_container_chall(self):
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["instance_info"] = {"is_container": True}
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        out = self._render()
        self.assertIn(STATUS_ICONS["container"]["stopped"], out)


class TestInstanceContainerMirror(TempWorkspaceCase):
    def _service(self):
        from ctf_downloader.services.instance_service import InstanceService
        with patch("ctf_downloader.services.platform_resolver.PlatformResolver.for_workspace",
                   return_value=(None, MagicMock(), {})):
            return InstanceService(str(self.root))

    def test_unknown_status_leaves_container_axis_untouched(self):
        # Fix review: status lạ ('unknown') không được map thành 'stopped'.
        svc = self._service()
        svc._update_local_instance_info(1, entry=None, time_left=0, status='unknown')
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st['container'], 'none')

    def test_running_and_stopped_still_mirror(self):
        svc = self._service()
        svc._update_local_instance_info(1, entry='h:1', time_left=9, status='running')
        svc._update_local_instance_info(1, entry=None, time_left=0, status='stopped')
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st['container'], 'stopped')


# ----------------------------------------------------------------------
# 8. compute_status + summary stats mở rộng
# ----------------------------------------------------------------------
class TestComputeStatusAndStats(TempWorkspaceCase):
    def test_compute_status_matches_read_status(self):
        st = StatusService.compute_status(self.repo, self.meta_path)
        self.assertEqual(st["schema_version"], 2)
        self.assertEqual(st["solve"], "unsolved")

    def test_summary_stats_counts_team_solves_and_hoarded(self):
        self.repo.update_status(self.meta_path, lambda st: {
            **st, "solve": "solved_by_team",
            "flag": {"value": None, "state": "hoarded"},
            "writeup": "draft",
        })
        stats = StatusService.summary_stats(self.repo)
        self.assertEqual(stats["solved_challenges"], 1)      # team solve ăn điểm
        self.assertEqual(stats["earned_points"], 100)
        self.assertEqual(stats["hoarded_flags"], 1)
        self.assertEqual(stats["writeup_drafts"], 1)


# ----------------------------------------------------------------------
# 9. Fix wave cycle-1: GAP-02 (CLI hoard) + GAP-03 (assessor wiring) + smoke
# ----------------------------------------------------------------------
class TestHoardCLI(TempWorkspaceCase):
    """GAP-02: ``ctf hoard <chal> <FLAG>`` -> SubmitService.hoard_flag.

    Tên lệnh là `hoard` (alias `flag-stash`) vì `flag` đã là alias của
    `submit` — xung đột parser, quyết định ghi trong handle_hoard docstring.
    """

    def _parse(self, argv):
        from ctf_downloader.cli import build_unified_parser
        return build_unified_parser().parse_args(argv)

    def _offline_submit_service(self):
        platform = MagicMock()
        platform.authenticate.return_value = True
        platform.fetch_challenges.return_value = []
        return patch("ctf_downloader.services.submit_service.create_session"), \
            patch("ctf_downloader.services.submit_service.PlatformDetector.detect_platform",
                  return_value=platform)

    def test_hoard_command_sets_hoarded_status(self):
        p1, p2 = self._offline_submit_service()
        with p1, p2:
            args = self._parse(["hoard", "1", "FLAG{stashed}", "-w", str(self.root)])
            from ctf_downloader.cli_commands import handle_hoard
            handle_hoard(args)
        st = self.repo.read_status(self.meta_path)
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{stashed}")
        self.assertEqual(st["solve"], "working")   # nâng từ unsolved

    def test_flag_stash_alias_parses(self):
        args = self._parse(["flag-stash", "-n", "Chall A", "-f", "FLAG{x}",
                            "-w", "/tmp"])
        self.assertEqual(args.flag, "FLAG{x}")
        self.assertEqual(args.name, "Chall A")

    def test_alias_flag_still_belongs_to_submit(self):
        # `flag` KHÔNG bị giành làm alias của hoard — vẫn dispatch về submit.
        args = self._parse(["flag", "--auto", "-w", "/tmp"])
        self.assertEqual(args.auto, True)

    def test_hoard_without_flag_exits_2(self):
        p1, p2 = self._offline_submit_service()
        with p1, p2:
            args = self._parse(["hoard", "1", "-w", str(self.root)])
            from ctf_downloader.cli_commands import handle_hoard
            with self.assertRaises(SystemExit) as cm:
                handle_hoard(args)
        self.assertEqual(cm.exception.code, 2)


class TestComputeStatusAssessorWiring(TempWorkspaceCase):
    """GAP-03: compute_status gọi assess_writeup và chỉ nâng trục writeup."""

    def _fill_writeup(self, with_placeholder=False):
        wp = self.meta_path.parent / "writeup" / "README.md"
        tail = "Flag: `FLAG{...}`\n" if with_placeholder else \
            "- [x] Solved\nFlag: `FLAG{real_flag_here}`\n"
        wp.write_text(
            "# Chall A\n\n## Reconnaissance\n" + ("analysis word " * 40) + "\n\n"
            "## Exploitation\n" + ("exploit word " * 40) + "\n\n"
            "```python\nprint('pwn')\n```\n\n" + tail,
            encoding="utf-8")

    def test_compute_status_calls_assessor(self):
        # Text không còn placeholder → kết quả mock được áp (none -> draft).
        self._fill_writeup(with_placeholder=False)
        with patch("ctf_downloader.services.status_service.assess_writeup",
                   return_value={"status": "draft", "score": 42,
                                 "signals": {}, "missing": []}) as mock:
            st = StatusService.compute_status(self.repo, self.meta_path)
        mock.assert_called_once()
        # Đủ 3 tham số (md_text, flag_format, reference_template) — template
        # sinh lại từ metadata được truyền vào guard-skeleton.
        _args, kwargs = mock.call_args
        self.assertEqual(len(_args) + len(kwargs), 3)
        self.assertEqual(st["writeup"], "draft")   # none -> draft (chỉ nâng)

    def test_only_raises_never_lowers(self):
        self.repo.update_status(self.meta_path,
                                lambda st: {**st, "writeup": "complete"})
        with patch("ctf_downloader.services.status_service.assess_writeup",
                   return_value={"status": "skeleton", "score": 0,
                                 "signals": {"template_similarity": 0.99},
                                 "missing": []}):
            st = StatusService.compute_status(self.repo, self.meta_path)
        self.assertEqual(st["writeup"], "complete")

    def test_writeup_auto_false_is_respected(self):
        self.repo.update_status(self.meta_path,
                                lambda st: {**st, "writeup_auto": False})
        with patch("ctf_downloader.services.status_service.assess_writeup") as mock:
            StatusService.compute_status(self.repo, self.meta_path)
        mock.assert_not_called()

    def test_filled_writeup_raises_to_complete_end_to_end(self):
        self._fill_writeup()
        st = StatusService.compute_status(self.repo, self.meta_path)
        self.assertEqual(st["writeup"], "complete")

    def test_intact_template_stays_none_without_guard_match(self):
        # Fixture README còn nguyên placeholder + không khớp guard skeleton
        # (metadata không đủ để tái tạo đúng template) -> KHÔNG nâng.
        st = StatusService.compute_status(self.repo, self.meta_path)
        self.assertEqual(st["writeup"], "none")


class TestStatusSmokePTIT(unittest.TestCase):
    """Smoke: `main.py status -w PTIT_CTF_2026` chạy thật, exit 0."""

    def test_main_py_status_smoke(self):
        repo_root = pathlib.Path(__file__).resolve().parent
        if not (repo_root / "PTIT_CTF_2026").is_dir():
            self.skipTest("PTIT_CTF_2026 workspace không tồn tại")
        result = subprocess.run(
            [sys.executable, "main.py", "status", "-w", "PTIT_CTF_2026"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CTF WORKSPACE", result.stdout)


if __name__ == "__main__":
    unittest.main()
