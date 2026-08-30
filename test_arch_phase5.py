"""Phase 5 tests: storage.workspace_repo (WorkspaceRepo) + chuyển 4 module dùng repo.

Task 3: class TestWorkspaceRepo / TestPatchSummaryLiveRank.
Task 4: characterization tests cho dashboard/instance_manager/ranking/submitter
        + pin-test throttle registry + multiprocess fileio (carry items).
"""
import json
import multiprocessing
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.storage.constants import STATUS_ICONS
from ctf_downloader.storage.fileio import atomic_write_json, locked_update_json
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def _make_workspace(d: str) -> pathlib.Path:
    """Workspace giả lập tối thiểu: challenges.json + 1 challenge có
    metadata.json / writeup/README.md chứa marker `- [ ] Solved`."""
    root = pathlib.Path(d) / "ws_ctf_x"
    (root / "Web" / "web_basics" / "writeup").mkdir(parents=True, exist_ok=True)

    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {
            "title": "TestCTF",
            "url": "https://gz.example.com",
            "platform": "gzctf",
        },
        "challenges": [
            {"id": 1, "name": "Web Basics", "category": "Web", "points": 100},
            {"id": 2, "name": "Pwn Intro", "category": "Pwn", "points": 200},
        ],
    }), encoding="utf-8")

    meta = {
        "id": 1,
        "name": "Web Basics",
        "category": "Web",
        "points": 100,
        "solved_by_me": False,
        "raw": None,  # cố tình để raw=None: predicate container không được crash
    }
    folder = root / "Web" / "web_basics"
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "writeup" / "README.md").write_text(
        "# Web Basics\n- [ ] Solved\nFLAG{...}\n", encoding="utf-8")
    return root


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5_ws_")
        self.root = _make_workspace(self._tmp)
        self.repo = WorkspaceRepo(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestWorkspaceRepo(WorkspaceCase):
    # ---- challenges.json ----

    def test_read_challenges_roundtrip_and_update_ctf_info_merge(self):
        data = self.repo.read_challenges()
        self.assertEqual(data["ctf_info"]["title"], "TestCTF")
        self.assertEqual(len(data["challenges"]), 2)

        self.repo.update_ctf_info(flag_format="^TEST\\{.+\\}$", flag_format_source="rules")
        merged = json.loads((self.root / "challenges.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["ctf_info"]["flag_format"], "^TEST\\{.+\\}$")
        self.assertEqual(merged["ctf_info"]["flag_format_source"], "rules")
        self.assertEqual(merged["ctf_info"]["url"], "https://gz.example.com")  # field cũ giữ nguyên
        self.assertEqual(len(merged["challenges"]), 2)

    def test_read_challenges_missing_file_returns_empty_dict(self):
        repo = WorkspaceRepo(pathlib.Path(self._tmp) / "nope")
        self.assertEqual(repo.read_challenges(), {})

    def test_write_challenges_roundtrip(self):
        data = self.repo.read_challenges()
        data["challenges"].append({"id": 3, "name": "Extra"})
        self.repo.write_challenges(data)
        again = self.repo.read_challenges()
        self.assertEqual([c["id"] for c in again["challenges"]], [1, 2, 3])

    def test_resolve_platform_url_from_ctf_info(self):
        self.assertEqual(self.repo.resolve_platform_url(), "https://gz.example.com")

    def test_resolve_platform_url_fallback_submit_endpoint(self):
        # Không có ctf_info.url -> rơi xuống submit_endpoint trong metadata.json
        data = self.repo.read_challenges()
        data["ctf_info"].pop("url")
        (self.root / "challenges.json").write_text(json.dumps(data), encoding="utf-8")
        meta_p = self.root / "Web" / "web_basics" / "metadata.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["submit_endpoint"] = "https://gz.example.com/games/7/challenges/1/submit"
        meta_p.write_text(json.dumps(meta), encoding="utf-8")

        self.assertEqual(self.repo.resolve_platform_url(), "https://gz.example.com")

    def test_resolve_platform_url_none_when_nothing_found(self):
        data = self.repo.read_challenges()
        data["ctf_info"].pop("url")
        (self.root / "challenges.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.repo.resolve_platform_url())

    # ---- find_challenge ----

    def test_find_challenge_partial_name_match(self):
        c = self.repo.find_challenge("web")
        self.assertIsNotNone(c)
        self.assertEqual(c.get("id"), 1)

    def test_find_challenge_tiers_exact_id_beats_partial_name(self):
        # id=2 tên "Pwn Intro"; query "2" phải khớp exact id, KHÔNG substring name
        self.assertEqual(self.repo.find_challenge(2)["name"], "Pwn Intro")
        # exact name
        self.assertEqual(self.repo.find_challenge("Pwn Intro")["id"], 2)
        # không khớp gì
        self.assertIsNone(self.repo.find_challenge("khong-tontai"))

    def test_find_challenge_from_metadata_sets_local_path(self):
        c = self.repo.find_challenge(999)  # chỉ tồn tại trong metadata.json
        meta_p = self.root / "Web" / "web_basics" / "metadata.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["id"] = 999
        meta_p.write_text(json.dumps(meta), encoding="utf-8")
        found = self.repo.find_challenge(999)
        self.assertIsNotNone(found)
        self.assertIn("_local_path", found)
        self.assertTrue(found["_local_path"].endswith("web_basics"))

    # ---- iter/read/write metadata ----

    def test_iter_challenges_yields_metadata_paths(self):
        paths = list(self.repo.iter_challenges())
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "metadata.json")

    def test_read_metadata_corrupt_returns_empty_dict(self):
        bad_dir = self.root / "Misc" / "broken"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "metadata.json").write_text("{oops", encoding="utf-8")
        found = [p for p in self.repo.iter_challenges() if p.parent == bad_dir]
        self.assertEqual(self.repo.read_metadata(found[0]), {})

    # ---- is_container predicate ----

    def test_is_container_raw_none_no_crash_and_false(self):
        meta = {"id": 1, "raw": None}
        self.assertFalse(WorkspaceRepo.is_container(meta))

    def test_is_container_variants(self):
        self.assertTrue(WorkspaceRepo.is_container({"instance_info": {"is_container": True}}))
        self.assertTrue(WorkspaceRepo.is_container({"type": "DynamicContainer"}))
        self.assertTrue(WorkspaceRepo.is_container({"raw": {"type": "dynamic_docker"}}))
        self.assertTrue(WorkspaceRepo.is_container({"raw": {"type": "DynamicContainer"}}))
        self.assertTrue(WorkspaceRepo.is_container({"tags": ["Container", "web"]}))
        self.assertFalse(WorkspaceRepo.is_container({"raw": {"type": "static"}, "tags": []}))

    # ---- submit history ----

    def test_submit_history_roundtrip(self):
        hist = {"entries": [{"flag": "T{x}", "result": "correct"}]}
        self.repo.save_submit_history(hist)
        loaded = self.repo.load_submit_history()
        self.assertEqual(loaded, hist)

    def test_load_submit_history_corrupt_backed_up(self):
        p = self.root / "submit_history.json"
        p.write_text("{corrupt!!! json", encoding="utf-8")
        self.assertEqual(self.repo.load_submit_history(), {"entries": []})
        self.assertTrue(pathlib.Path(str(p) + ".bak").exists())
        self.assertIn("corrupt", pathlib.Path(str(p) + ".bak").read_text(encoding="utf-8"))

    def test_load_submit_history_valid_non_dict_treated_as_corrupt(self):
        # Carry item: JSON hợp lệ nhưng KHÔNG phải dict -> corrupt (.bak + {})
        p = self.root / "submit_history.json"
        p.write_text('[1, 2, 3]', encoding="utf-8")
        self.assertEqual(self.repo.load_submit_history(), {"entries": []})
        self.assertTrue(pathlib.Path(str(p) + ".bak").exists())

    def test_load_submit_history_filters_non_dict_entries(self):
        p = self.root / "submit_history.json"
        p.write_text(json.dumps({"entries": [{"flag": "a"}, "junk", 42]}), encoding="utf-8")
        self.assertEqual(
            self.repo.load_submit_history(),
            {"entries": [{"flag": "a"}]},
        )

    def test_load_submit_history_missing_file(self):
        self.assertEqual(self.repo.load_submit_history(), {"entries": []})

    # ---- solved state ----

    def test_read_solved_state_detects_done_marker(self):
        readme = self.root / "Web" / "web_basics" / "writeup" / "README.md"
        readmes = [readme]
        self.assertFalse(self.repo.read_solved_state(readmes))
        readme.write_text("# Web Basics\n- [X] Solved\n", encoding="utf-8")
        self.assertTrue(self.repo.read_solved_state(readmes))

    def test_read_solved_state_skips_missing_files(self):
        ghost = self.root / "no" / "such" / "README.md"
        self.assertFalse(self.repo.read_solved_state([ghost]))

    def test_write_solved_state_flips_marker_and_counts(self):
        readme = self.root / "Web" / "web_basics" / "writeup" / "README.md"
        n = self.repo.write_solved_state([readme], solved=True)
        self.assertEqual(n, 1)
        text = readme.read_text(encoding="utf-8")
        self.assertIn("- [x] Solved", text)
        self.assertNotIn("- [ ] Solved", text)

        # Đổi ngược lại
        n_back = self.repo.write_solved_state([readme], solved=False)
        self.assertEqual(n_back, 1)
        self.assertIn("- [ ] Solved", readme.read_text(encoding="utf-8"))

    def test_write_solved_state_no_change_returns_zero(self):
        readme = self.root / "Web" / "web_basics" / "writeup" / "README.md"
        # Đã là [x] rồi, gọi solved=True lần nữa -> 0 file đổi
        readme.write_text("- [x] Solved\n", encoding="utf-8")
        self.assertEqual(self.repo.write_solved_state([readme], solved=True), 0)


class TestPatchSummaryLiveRank(WorkspaceCase):
    def _summary_path(self) -> pathlib.Path:
        return self.root / "SUMMARY.md"

    def _make_summary(self):
        self._summary_path().write_text(
            "# Summary\n\n"
            "| Bài | Điểm |\n"
            "| --- | --- |\n"
            "| Web Basics | 100 |\n\n"
            "- **Total Files Downloaded**: 12\n",
            encoding="utf-8",
        )

    def test_patch_inserts_before_total_files_line(self):
        self._make_summary()
        rank_line = "- **Live Rank**: `#3` / `20` (Team: `team_x`)"
        self.assertTrue(self.repo.patch_summary_live_rank(rank_line))
        lines = self._summary_path().read_text(encoding="utf-8").splitlines()
        idx_rank = next(i for i, l in enumerate(lines) if l.startswith("- **Live Rank**"))
        idx_files = next(i for i, l in enumerate(lines) if l.startswith("- **Total Files Downloaded**"))
        self.assertEqual(idx_rank, idx_files - 1)

    def test_patch_twice_replaces_not_duplicates(self):
        self._make_summary()
        first = "- **Live Rank**: `#3` / `20` (Team: `team_x`)"
        second = "- **Live Rank**: `#1` / `20` (Team: `team_x`)"
        self.assertTrue(self.repo.patch_summary_live_rank(first))
        self.assertTrue(self.repo.patch_summary_live_rank(second))
        text = self._summary_path().read_text(encoding="utf-8")
        self.assertEqual(text.count("- **Live Rank**:"), 1)
        self.assertIn("`#1`", text)
        self.assertNotIn("`#3`", text)

    def test_patch_missing_summary_returns_false(self):
        self.assertFalse(self.repo.patch_summary_live_rank("- **Live Rank**: `#1`"))

    def test_patch_without_anchor_line_returns_false(self):
        (self.root / "SUMMARY.md").write_text("no anchor here\n", encoding="utf-8")
        self.assertFalse(self.repo.patch_summary_live_rank("- **Live Rank**: `#1`"))

    def test_patch_rank_line_with_backslash_team(self):
        """Carry item: team chứa ký tự ``\\`` — với repl là chuỗi thuần,
        re.sub từng coi ``\\x`` là escape và vỡ output. Sau khi đổi sang
        lambda repl, rank_line phải được chèn NGUYÊN VĂN."""
        self._make_summary()
        rank_line = "- **Live Rank**: `#2` / `20` (Team: `team\\slash`)"
        self.assertTrue(self.repo.patch_summary_live_rank(rank_line))
        text = self._summary_path().read_text(encoding="utf-8")
        self.assertEqual(text.count("- **Live Rank**:"), 1)
        self.assertIn("(Team: `team\\slash`)", text)


# ---------------------------------------------------------------------------
# Carry item: test multiprocess lockfile của fileio — file duy nhất trong
# test_arch_phase2.py CHƯA được copy sang test_arch_phase3.py (7/8 test còn
# lại đã copy nguyên văn). Được di chuyển về đây trước khi xoá phase2.
# ---------------------------------------------------------------------------

def _increment_worker(path_str: str, key: str, n: int, errors):
    """Multiprocessing worker: tăng `key` lên n lần qua locked_update_json."""
    try:
        for _ in range(n):
            locked_update_json(
                path_str,
                lambda d, k=key: {**(d or {}), k: (d or {}).get(k, 0) + 1},
            )
    except Exception as exc:  # noqa: BLE001 - báo lỗi về parent để assert
        errors.put(exc)


class TestFileIOMultiprocess(unittest.TestCase):
    def test_multiprocess_locked_increments(self):
        """4 process x 50 increments: mỗi key phải đúng 50, không exception.

        Chứng minh lockfile riêng (<name>.lock) + tmp unique (mkstemp)
        giữ đúng thứ tự read-modify-write giữa các process.
        """
        n_procs, n_incr = 4, 50
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "counter.json"
            atomic_write_json(p, {})

            # Python 3.13 warns (and may deadlock) when fork() is called
            # from a multi-threaded xdist worker. Spawn still exercises true
            # cross-process flock semantics without inheriting pytest threads.
            ctx = multiprocessing.get_context("spawn")
            errors = ctx.Queue()
            procs = [
                ctx.Process(
                    target=_increment_worker,
                    args=(str(p), str(i), n_incr, errors),
                )
                for i in range(n_procs)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=60)

            for proc in procs:
                self.assertEqual(proc.exitcode, 0, f"process crashed: {proc.name}")

            worker_errors = []
            while not errors.empty():
                worker_errors.append(errors.get())
            self.assertEqual(worker_errors, [], "worker raised exceptions")

            final = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(
                final,
                {str(i): 50 for i in range(n_procs)},
                f"lost-update detected: {final}",
            )
            # Không còn tmp file sót lại
            self.assertEqual(list(pathlib.Path(d).glob("*.tmp")), [])


# ---------------------------------------------------------------------------
# Task 4 — Characterization tests (viết TRƯỚC khi sửa 4 module)
# ---------------------------------------------------------------------------

class TestDashboardStats(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5_dash_")
        root = pathlib.Path(self._tmp) / "ws"
        folder_solved = root / "Web" / "web_basics" / "writeup"
        folder_unsolved = root / "Pwn" / "pwn_intro"
        folder_solved.mkdir(parents=True, exist_ok=True)
        folder_unsolved.mkdir(parents=True, exist_ok=True)

        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"title": "DashCTF", "url": "https://d.test",
                         "platform": "gzctf", "user": "me", "team": "us"},
            "challenges": [
                {"id": 1, "name": "Web Basics", "category": "Web", "points": 100},
                {"id": 2, "name": "Pwn Intro", "category": "Pwn", "points": 300},
            ],
        }), encoding="utf-8")

        (folder_solved.parent / "metadata.json").write_text(
            json.dumps({"id": 1, "name": "Web Basics", "category": "Web", "points": 100}),
            encoding="utf-8")
        (folder_solved / "README.md").write_text(
            "# Web Basics\n- [x] Solved\nFLAG{got_it}\n", encoding="utf-8")
        (folder_unsolved / "metadata.json").write_text(
            json.dumps({"id": 2, "name": "Pwn Intro", "category": "Pwn", "points": 300}),
            encoding="utf-8")
        (folder_unsolved / "README.md").write_text(
            "# Pwn Intro\n- [ ] Solved\nFLAG{...}\n", encoding="utf-8")
        self.ws = str(root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_summary_stats_numbers(self):
        from ctf_downloader.dashboard import CTFDashboard

        dash = CTFDashboard(self.ws)
        stats = dash.get_summary_stats()
        self.assertEqual(stats["title"], "DashCTF")
        self.assertEqual(stats["platform"], "gzctf")
        self.assertEqual(stats["total_challenges"], 2)
        self.assertEqual(stats["solved_challenges"], 1)
        self.assertEqual(stats["unsolved_challenges"], 1)
        self.assertEqual(stats["total_points"], 400)
        self.assertEqual(stats["earned_points"], 100)
        self.assertAlmostEqual(stats["completion_rate"], 50.0)
        self.assertEqual(set(stats["categories"]), {"Web", "Pwn"})
        self.assertEqual(stats["categories"]["Web"]["solved"], 1)
        self.assertEqual(stats["categories"]["Pwn"]["earned"], 0)

    def test_local_challenges_scan_fields_and_container_flag(self):
        from ctf_downloader.dashboard import CTFDashboard

        dash = CTFDashboard(self.ws)
        by_id = {c["id"]: c for c in dash.local_challenges}
        self.assertEqual(by_id[1]["solved_by_me"], True)   # marker README
        self.assertEqual(by_id[2]["solved_by_me"], False)  # TODO marker
        self.assertTrue(by_id[1]["_folder"].endswith("web_basics"))
        self.assertEqual(by_id[1]["_rel_folder"], os.path.join("Web", "web_basics"))
        self.assertIn("_local_files_count", by_id[1])


class TestInstanceListContainers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5_inst_")
        root = pathlib.Path(self._tmp) / "ws"
        c1 = root / "Web" / "dyn_chall"
        c2 = root / "Misc" / "static_chall"
        c1.mkdir(parents=True, exist_ok=True)
        c2.mkdir(parents=True, exist_ok=True)

        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"url": "https://gz.example.com", "platform": "gzctf"},
        }), encoding="utf-8")
        (c1 / "metadata.json").write_text(json.dumps({
            "id": 10, "name": "Dyn Chall",
            "raw": {"type": "dynamic_docker"},
        }), encoding="utf-8")
        (c2 / "metadata.json").write_text(json.dumps({
            "id": 11, "name": "Static Chall",
            "raw": {"type": "static"},
        }), encoding="utf-8")
        self.ws = str(root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_list_containers_includes_dynamic_docker_only(self):
        from ctf_downloader.instance_manager import InstanceManager

        with mock.patch.object(InstanceManager, "_init_platform",
                               return_value=mock.MagicMock()):
            mgr = InstanceManager(self.ws)
        results = mgr.list_containers()
        ids = sorted(m["id"] for m in results)
        self.assertEqual(ids, [10])
        self.assertTrue(results[0]["_local_path"].endswith("dyn_chall"))


class TestRankingPatchSummaryIdempotent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5_rank_")
        root = pathlib.Path(self._tmp) / "ws"
        root.mkdir(parents=True)
        (root / "SUMMARY.md").write_text(
            "# Summary\n- **Total Files Downloaded**: 7\n", encoding="utf-8")
        self.root = root

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_manager(self):
        from ctf_downloader.ranking import RankingManager

        with mock.patch("ctf_downloader.ranking.create_session", return_value=mock.MagicMock()), \
             mock.patch("ctf_downloader.ranking.PlatformDetector.detect_platform",
                        return_value=mock.MagicMock()):
            return RankingManager(workspace_path=str(self.root), url="https://r.test")

    def test_save_ranking_docs_patch_is_idempotent(self):
        mgr = self._make_manager()
        data = {
            "title": "RCTF Live", "my_team": "team_x", "my_user": None,
            "my_rank": 3, "my_score": 1500, "total_teams": 25,
            "standings": [
                {"pos": 1, "name": "top_guys", "score": 3000},
                {"pos": 3, "name": "team_x", "score": 1500},
            ],
        }
        mgr._save_ranking_docs(data)
        text_once = (self.root / "SUMMARY.md").read_text(encoding="utf-8")
        mgr._save_ranking_docs(data)
        text_twice = (self.root / "SUMMARY.md").read_text(encoding="utf-8")

        self.assertEqual(text_once.count("- **Live Rank**:"), 1)
        self.assertEqual(text_once, text_twice)
        # Dòng Live Rank đứng ngay trên dòng Total Files
        lines = text_twice.splitlines()
        idx_rank = next(i for i, l in enumerate(lines) if l.startswith("- **Live Rank**"))
        idx_files = next(i for i, l in enumerate(lines)
                         if l.startswith("- **Total Files Downloaded**"))
        self.assertEqual(idx_rank, idx_files - 1)
        # RANKING.md được ghi
        self.assertTrue((self.root / "RANKING.md").exists())


class TestSubmitterResolveUrlFromWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5_sub_")
        root = pathlib.Path(self._tmp) / "ws"
        meta_dir = root / "Web" / "some_chall"
        meta_dir.mkdir(parents=True)
        # challenges.json KHÔNG có platform_url lẫn ctf_info.url
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"title": "NoUrlCTF"},
            "challenges": [],
        }), encoding="utf-8")
        (meta_dir / "metadata.json").write_text(json.dumps({
            "id": 1, "name": "Some Chall",
            "submit_endpoint": "https://fb.example.com/api/challs/1/submit",
        }), encoding="utf-8")
        self.ws = str(root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_url_resolved_from_metadata_submit_endpoint(self):
        from ctf_downloader.submitter import FlagSubmitter

        with mock.patch("ctf_downloader.submitter.create_session",
                        return_value=mock.MagicMock()), \
             mock.patch("ctf_downloader.submitter.PlatformDetector.detect_platform",
                        return_value=mock.MagicMock()):
            fs = FlagSubmitter(workspace_dir=self.ws)
        self.assertEqual(fs.url, "https://fb.example.com")


# ---------------------------------------------------------------------------
# Carry item 5 — pin-test throttle registry (khóa quyết định task trước)
# ---------------------------------------------------------------------------

class TestRegistryThrottlePins(unittest.TestCase):
    def test_custom_rest_and_generic_html_throttle_pinned_at_5(self):
        from ctf_downloader.platforms.registry import get_spec

        self.assertEqual(get_spec("custom_rest").throttle, 5.0)
        self.assertEqual(get_spec("generic_html").throttle, 5.0)

    def test_other_throttles_still_match_old_submitter_dict(self):
        from ctf_downloader.platforms.registry import get_spec

        self.assertEqual(get_spec("ctfd").throttle, 6.0)
        self.assertEqual(get_spec("gzctf").throttle, 2.0)
        self.assertEqual(get_spec("rctf").throttle, 5.0)


# ---------------------------------------------------------------------------
# Task 8 — Characterization tests: StatusService + PullService
# ---------------------------------------------------------------------------

class TestStatusServiceSummaryStats(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch8_status_")
        self.root = _make_workspace(self._tmp)
        self.repo = WorkspaceRepo(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_summary_stats_from_repo(self):
        from ctf_downloader.services.status_service import StatusService

        stats = StatusService.summary_stats(self.repo)
        self.assertEqual(stats["title"], "TestCTF")
        self.assertEqual(stats["platform"], "gzctf")
        self.assertEqual(stats["total_challenges"], 1)
        self.assertEqual(stats["solved_challenges"], 0)
        self.assertEqual(stats["unsolved_challenges"], 1)
        self.assertEqual(stats["total_points"], 100)
        self.assertEqual(stats["earned_points"], 0)
        self.assertEqual(set(stats["categories"]), {"Web"})

    def test_scan_all_workspaces_prints_table(self):
        import io
        from contextlib import redirect_stdout
        from ctf_downloader.services.status_service import StatusService

        # Workspace rỗng -> bị skip (total == 0)
        empty = pathlib.Path(self._tmp) / "ws_empty"
        empty.mkdir(parents=True, exist_ok=True)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rows = StatusService.scan_all_workspaces(self._tmp)
        out = buf.getvalue()
        # UI redesign: header "==== SCANNING ALL CTF WORKSPACES ====" thay bằng
        # dòng Logger.info "Scanning all CTF workspaces in ..." + rich Table.
        self.assertIn("Scanning all CTF workspaces", out)
        self.assertIn("TestCTF", out)
        # synthesis-v6 N2: platform render qua display_label (spec.label
        # 'GZ::CTF') — không còn lộ key nội bộ lowercase 'gzctf'.
        self.assertIn("GZ::CTF", out)
        self.assertNotIn(" gzctf ", out)
        self.assertIn("0/1", out)
        # Workspace rỗng không xuất hiện trong bảng
        self.assertNotIn("ws_empty\n", out.split("Progress")[-1].split("TestCTF")[0])
        self.assertTrue(rows)
        self.assertEqual(rows[0]["title"], "TestCTF")

    def test_scan_all_workspaces_missing_dir_warns_only(self):
        import io
        from contextlib import redirect_stdout
        from ctf_downloader.services.status_service import StatusService

        missing = pathlib.Path(self._tmp) / "no_such_dir"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rows = StatusService.scan_all_workspaces(str(missing))
        self.assertEqual(rows, [])


class TestPullServiceRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch8_pull_")
        self.out_dir = os.path.join(self._tmp, "out")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _config(self, threads=2):
        from ctf_downloader.config import DownloaderConfig

        return DownloaderConfig(
            url="https://pull.example.com",
            cookie="session=abc",
            output_dir=self.out_dir,
            threads=threads,
        )

    def _fake_platform(self):
        from ctf_downloader.models import Challenge, CTFInfo

        plat = mock.MagicMock()
        plat.authenticate.return_value = True
        plat.ctf_info = CTFInfo(title="PullCTF", url="https://pull.example.com")
        plat.fetch_challenges.return_value = [
            Challenge(id=1, name="Chall A", category="Web", points=100),
            Challenge(id=2, name="Chall B", category="Pwn", points=200),
        ]
        return plat

    def test_run_returns_result_dict_and_builds_workspace(self):
        import threading
        from ctf_downloader.services import pull_service

        fake_platform = self._fake_platform()

        real_dm_cls = pull_service.DownloadManager
        constructions = []

        class SpyDM(real_dm_cls):
            def __init__(self, session=None, **kw):
                super().__init__(session=session, **kw)
                constructions.append((threading.get_ident(), id(session)))

        master_sessions = []
        real_create = pull_service.create_session

        def spy_create(**kw):
            sess = real_create(**kw)
            master_sessions.append(sess)
            return sess

        with mock.patch.object(pull_service.PlatformDetector, "detect_platform",
                               return_value=fake_platform), \
             mock.patch.object(pull_service, "DownloadManager", SpyDM), \
             mock.patch.object(pull_service, "create_session", side_effect=spy_create):
            result = pull_service.PullService.run(self._config())

        self.assertIsInstance(result, dict)
        self.assertTrue(result["ok"])
        self.assertEqual(result["challenges_processed"], 2)
        self.assertTrue(os.path.isdir(self.out_dir))
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "SUMMARY.md")))
        self.assertTrue(result["output_dir"] == self.out_dir)

        # Mỗi thread worker dùng đúng 1 session, và KHÔNG phải session master
        per_thread = {}
        for tid, sess_id in constructions:
            per_thread.setdefault(tid, set()).add(sess_id)
        for tid, sessions in per_thread.items():
            self.assertEqual(len(sessions), 1,
                             f"thread {tid} used multiple sessions")
        for _, sess_id in constructions:
            for master in master_sessions:
                self.assertNotEqual(sess_id, id(master),
                                    "worker reused the shared master session")

    def test_run_no_challenges_returns_not_ok(self):
        from ctf_downloader.services import pull_service

        fake_platform = self._fake_platform()
        fake_platform.fetch_challenges.return_value = []

        with mock.patch.object(pull_service.PlatformDetector, "detect_platform",
                               return_value=fake_platform):
            result = pull_service.PullService.run(self._config())

        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])

    def test_ctf_downloader_facade_delegates_and_keeps_bool(self):
        from ctf_downloader.core import CTFDownloader
        from ctf_downloader.services import pull_service

        fake_platform = self._fake_platform()

        with mock.patch.object(pull_service.PlatformDetector, "detect_platform",
                               return_value=fake_platform), \
             mock.patch.object(pull_service.PullService, "run",
                               return_value={"ok": True}) as run_mock:
            dl = CTFDownloader(self._config())
            ok = dl.run()

        self.assertTrue(ok)
        run_mock.assert_called_once()

    def test_dashboard_facade_delegates_to_status_service(self):
        from ctf_downloader.dashboard import CTFDashboard
        from ctf_downloader.services.status_service import StatusService

        ws_root = _make_workspace(self._tmp)
        with mock.patch.object(StatusService, "summary_stats",
                               wraps=StatusService.summary_stats) as spy:
            dash = CTFDashboard(str(ws_root))
            stats = dash.get_summary_stats()
        self.assertEqual(stats["total_challenges"], 1)
        self.assertGreaterEqual(spy.call_count, 1)


# ---------------------------------------------------------------------------
# Task 12 — Characterization test: render_tree(only_container=True)
# ---------------------------------------------------------------------------

class TestRenderTreeOnlyContainer(unittest.TestCase):
    """Chỉ challenge có dấu hiệu container (raw.type=dynamic_docker) được in."""

    def setUp(self):
        import contextlib
        import io

        self._ctxlib = contextlib
        self._io = io
        self._tmp = tempfile.mkdtemp(prefix="arch12_tree_")
        root = pathlib.Path(self._tmp) / "ws_ctf_tree"
        (root / "Web" / "dyn").mkdir(parents=True)
        (root / "Web" / "static").mkdir(parents=True)
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"title": "TreeCTF", "url": "https://tree.example.com",
                         "platform": "gzctf"},
            "challenges": [
                {"id": 1, "name": "Dyn", "category": "Web", "points": 100},
                {"id": 2, "name": "Static", "category": "Web", "points": 100},
            ],
        }), encoding="utf-8")
        (root / "Web" / "dyn" / "metadata.json").write_text(json.dumps(
            {"id": 1, "name": "Dyn", "category": "Web", "points": 100,
             "raw": {"type": "dynamic_docker"}}), encoding="utf-8")
        (root / "Web" / "static" / "metadata.json").write_text(json.dumps(
            {"id": 2, "name": "Static", "category": "Web", "points": 100}),
            encoding="utf-8")
        self.root = root

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _render(self, **kw) -> str:
        from ctf_downloader.services.status_service import StatusService

        buf = self._io.StringIO()
        with self._ctxlib.redirect_stdout(buf):
            StatusService.render_tree(WorkspaceRepo(self.root), **kw)
        return buf.getvalue()

    def test_only_container_filters_out_non_container_challenges(self):
        full = self._render()
        cont_only = self._render(only_container=True)

        # Full tree: cả 2 challenge đều xuất hiện; container có glyph ⛁
        # (PHOSPHOR §4.3 — bỏ tag "[🐳 Container]" trùng lặp).
        self.assertIn("Dyn", full)
        self.assertIn("Static", full)
        self.assertIn("⛁", full)

        # only_container=True: chỉ Dyn còn lại, Static bị lọc bỏ;
        # header workspace vẫn được in đầy đủ (panel title + subtitle).
        self.assertIn("Dyn", cont_only)
        self.assertNotIn("Static", cont_only)
        self.assertIn("TreeCTF", cont_only)
        self.assertIn("tree", cont_only.lower())
        self.assertIn("⛁", cont_only)


# ======================================================================
# Fix wave #2 — regression tests (weakness-report-cycle1 findings)
# ======================================================================

class TestLockedUpdateDataSafety(unittest.TestCase):
    """Critical-1: locked_update_json không được phá dữ liệu vĩnh viễn."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wave2_data_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _write(self, name: str, text: str) -> pathlib.Path:
        p = pathlib.Path(self._tmp) / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_bom_file_preserved_through_locked_update(self):
        # metadata.json có BOM UTF-8: update_status cũ parse fail -> coi như {}
        # -> ghi đè mất name/id. Sau fix phải đọc được (utf-8-sig) và giữ key.
        p = self._write(
            "metadata.json",
            "﻿" + json.dumps({"id": 1, "name": "Web Basics", "points": 100}),
        )
        out = locked_update_json(p, lambda d: {**d, "status": {"solve": "solved_by_me"}})
        self.assertEqual(out["name"], "Web Basics")
        self.assertEqual(out["id"], 1)
        on_disk = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertEqual(on_disk["name"], "Web Basics")
        self.assertEqual(on_disk["status"]["solve"], "solved_by_me")

    def test_unreadable_file_aborts_without_overwrite(self):
        # chmod 000: đọc fail -> KHÔNG được ghi đè (không có gì để backup).
        p = self._write("challenges.json", json.dumps({"ctf_info": {"title": "Keep"}}))
        os.chmod(p, 0o000)
        self.addCleanup(os.chmod, p, 0o644)
        with self.assertRaises(OSError):
            locked_update_json(p, lambda d: {"hacked": True})
        os.chmod(p, 0o644)
        # Nội dung gốc còn nguyên vẹn
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"ctf_info": {"title": "Keep"}})
        # Không có .bak nào bị ghi rác từ dữ liệu rỗng
        bak = p.with_name(p.name + ".bak")
        if bak.exists():
            self.assertEqual(bak.read_text(encoding="utf-8"), json.dumps({"ctf_info": {"title": "Keep"}}))

    def test_corrupt_file_backed_up_before_overwrite(self):
        original = "{ this is not json"
        p = self._write("submit_history.json", original)
        locked_update_json(p, lambda d: {"entries": [{"flag": "X"}]})
        bak = p.with_name(p.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(encoding="utf-8"), original)

    def test_valid_json_non_dict_list_backed_up_before_overwrite(self):
        # Deferred open-code (Task-2 #3 write-path): metadata.json chứa JSON
        # HỢP LỆ nhưng non-dict ([1, 2]) — trước đây bị nuốt im lặng KHÔNG
        # .bak. Phải mirror read-path: backup nội dung gốc sang .bak trước
        # khi thay bằng {} cho mutator làm việc.
        original = json.dumps([1, 2])
        p = self._write("metadata.json", original)
        out = locked_update_json(p, lambda d: {**d, "name": "rebuilt"})
        self.assertEqual(out, {"name": "rebuilt"})
        bak = p.with_name(p.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(encoding="utf-8"), original)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")),
                         {"name": "rebuilt"})

    def test_valid_json_non_dict_scalar_also_backed_up(self):
        # Tương tự trên kiểu scalar (string/int) — mọi JSON hợp lệ sai kiểu
        # đều phải đi qua .bak, không mất dữ liệu âm thầm.
        original = json.dumps("just a string")
        p = self._write("submit_history.json", original)
        locked_update_json(p, lambda d: {**d, "entries": []})
        bak = p.with_name(p.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(encoding="utf-8"), original)

    def test_repo_read_metadata_handles_bom_and_unreadable(self):
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        bom_path = self._write(
            "m1.json", "﻿" + json.dumps({"id": 7, "name": "Bom Chall"})
        )
        repo = WorkspaceRepo(self._tmp)
        meta = repo.read_metadata(bom_path)
        self.assertEqual(meta.get("name"), "Bom Chall")


class TestSummaryGeneratorSafeValues(unittest.TestCase):
    """Critical-2: summary_generator không crash với points/category bất thường."""

    def setUp(self):
        import tempfile as _tf
        self._tmp = _tf.mkdtemp(prefix="wave2_summary_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _generate(self, challenges):
        from ctf_downloader.models import CTFInfo

        info = CTFInfo(title="Wave2CTF", url="https://x.example", challenges=challenges)
        path = SummaryGenerator.generate_summary(
            base_output_dir=self._tmp, ctf_info=info,
            all_results={c.id: [] for c in challenges},
        )
        with open(path, encoding="utf-8") as f:
            summary = f.read()
        with open(os.path.join(self._tmp, "challenges.json"), encoding="utf-8") as f:
            data = json.load(f)
        return summary, data

    def test_none_string_negative_points_and_none_category(self):
        from ctf_downloader.models import Challenge

        challs = [
            Challenge(id=1, name="NoPoints", category=None, points=None),
            Challenge(id=2, name="StrPoints", category="Web", points="100"),
            Challenge(id=3, name="NegPoints", category="Web", points=-5),
        ]
        summary, data = self._generate(challs)
        self.assertIn("Wave2CTF", summary)
        # None -> 0, "100" -> 100, -5 giữ nguyên => tổng 95
        self.assertEqual(data["total_points"], 95)
        # category None không crash sorted(); nhóm về default
        cats = set(data["categories"])
        self.assertNotIn(None, cats)
        self.assertIn("Web", cats)

    def test_all_none_values_render_valid_output(self):
        from ctf_downloader.models import Challenge

        challs = [Challenge(id=i, name=f"c{i}", category=None, points=None) for i in range(3)]
        summary, data = self._generate(challs)
        self.assertEqual(data["total_points"], 0)
        self.assertEqual(len(data["categories"]), 1)


class TestValidateFlagReDoSGuard(unittest.TestCase):
    """Important: validate_flag / regex search có timeout, không treo CLI."""

    REDOS_FMT = "(a+)+$"

    def test_validate_flag_redos_returns_quickly_false(self):
        from ctf_downloader.utils.flag_format import validate_flag
        import time

        flag = "a" * 29 + "B"
        t0 = time.monotonic()
        result = validate_flag(flag, self.REDOS_FMT)
        elapsed = time.monotonic() - t0
        self.assertFalse(result)
        self.assertLess(elapsed, 3.0)

    def test_validate_flag_still_works_normal_cases(self):
        from ctf_downloader.utils.flag_format import validate_flag

        self.assertTrue(validate_flag("PTITCTF{abc}", "^PTITCTF\\{.+\\}$"))
        self.assertFalse(validate_flag("WRONG{abc}", "^PTITCTF\\{.+\\}$"))

    def test_oversized_pattern_rejected(self):
        from ctf_downloader.utils.flag_format import MAX_PATTERN_LENGTH, validate_flag

        self.assertFalse(validate_flag("X{y}", "^a" * (MAX_PATTERN_LENGTH // 2)))

    def test_regex_search_helper_has_timeout(self):
        from ctf_downloader.utils.flag_format import regex_search_with_timeout
        import time

        t0 = time.monotonic()
        m = regex_search_with_timeout("(a+)+$", "a" * 29 + "B")
        elapsed = time.monotonic() - t0
        self.assertIsNone(m)
        self.assertLess(elapsed, 3.0)


class TestDupAlternationReDoSGuard(unittest.TestCase):
    """Deferred open-code (cycle v3#4 residual): (a|a)+$ KHÔNG có quantifier
    lồng nên _scan_nested_quantifier bỏ qua, nhưng nhánh trùng nội dung vẫn
    backtrack theo cấp số mũ. Pattern đến từ trang challenge (untrusted)."""

    def test_dup_alternation_scanner_variants(self):
        from ctf_downloader.utils.flag_format import _scan_dup_alternation as dup

        self.assertTrue(dup("(a|a)+$"))
        self.assertTrue(dup("(?:x|x)+"))
        self.assertTrue(dup("([ab]|[ab])+"))      # class trùng nguyên văn
        self.assertTrue(dup("((a|a)|(a|a))+"))    # nhóm cha lặp nhóm con giống nhau
        # alternation hợp lệ, các nhánh khác nhau -> không bị cấm
        self.assertFalse(dup("(a|b)+$"))
        self.assertFalse(dup("(cat|dog)s?"))
        self.assertFalse(dup("(https?|ftp)://"))
        self.assertFalse(dup("[ab]+"))            # không có alternation

    def test_dup_alternation_redos_10k_a_completes_quickly(self):
        from ctf_downloader.utils.flag_format import regex_search_with_timeout
        import time

        t0 = time.monotonic()
        m = regex_search_with_timeout("(a|a)+$", "a" * 10000 + "B")
        elapsed = time.monotonic() - t0
        self.assertIsNone(m)
        self.assertLess(elapsed, 2.0)

    def test_nested_quantifier_redos_10k_a_completes_quickly(self):
        from ctf_downloader.utils.flag_format import regex_search_with_timeout
        import time

        t0 = time.monotonic()
        m = regex_search_with_timeout("(a+)+$", "a" * 10000 + "B")
        elapsed = time.monotonic() - t0
        self.assertIsNone(m)
        self.assertLess(elapsed, 2.0)

    def test_find_matches_helper_rejects_dup_alternation(self):
        from ctf_downloader.utils.flag_format import regex_matches_with_timeout
        import time

        t0 = time.monotonic()
        out = regex_matches_with_timeout("(?:x|x)+", "x" * 5000 + "y")
        elapsed = time.monotonic() - t0
        self.assertIsNone(out)
        self.assertLess(elapsed, 2.0)

    def test_legit_patterns_still_scan_after_dup_guard(self):
        from ctf_downloader.utils.flag_format import (
            regex_search_with_timeout,
            validate_flag,
        )

        self.assertIsNotNone(
            regex_search_with_timeout("(cat|dog)s?", "two dogs here"))
        self.assertIsNotNone(
            regex_search_with_timeout("^PTITCTF\\{.+\\}$", "PTITCTF{x}"))
        self.assertTrue(validate_flag("PTITCTF{abc}", "^PTITCTF\\{.+\\}$"))

    # ------------------------------------------------------------------
    # MED-fix (review 9b04099): bypass lớp guard qua inline-flag / escape
    # — (?i)(x|X)+$ và (\x61|a)+$ trượt cả _scan_nested_quantifier lẫn
    # _scan_dup_alternation vì so nhánh NGUYÊN VĂN.
    # ------------------------------------------------------------------

    def test_scanner_catches_inline_flag_and_escape_dup_variants(self):
        from ctf_downloader.utils.flag_format import _scan_dup_alternation as dup

        self.assertTrue(dup("(?i)(x|X)+$"))        # (?i) gộp x ≡ X
        self.assertTrue(dup("(?i:(?:x|X))+"))      # scoped (?i:...)
        self.assertTrue(dup(r"(\x61|a)+$"))        # \x61 ≡ a sau decode
        self.assertTrue(dup(r"(A|\x41)+"))    # A ≡ \x41
        self.assertTrue(dup("(?i)((x)|(X))+"))     # thân nhóm lồng đã fold
        self.assertTrue(dup("(?:\\101|A)+"))       # octal \101 ≡ 'A'

    def test_scanner_not_overblocking_flagged_valid_patterns(self):
        from ctf_downloader.utils.flag_format import _scan_dup_alternation as dup

        self.assertFalse(dup("(?i)(foo|bar)+$"))   # có (?i), nhánh khác nhau
        self.assertFalse(dup(r"(?-i:(a|A))+$"))    # tắt ignorecase → a ≠ A thật
        self.assertFalse(dup(r"(\x61|b)+$"))       # escape nhưng không trùng
        self.assertFalse(dup("(?i)[ab]+"))         # không có alternation
        self.assertFalse(dup(r"a\|b|(?:c|d)"))     # '|' literal ≠ '|' cấu trúc

    def test_redos_bypass_variants_blocked_statically_10k(self):
        from ctf_downloader.utils.flag_format import regex_search_with_timeout
        import time

        cases = [
            (r"(\x61|a)+$", "a" * 10000 + "B"),
            ("(?i)(x|X)+$", "x" * 20000 + "B"),       # ~4.4s nếu lọt qua guard
            ("(?i)(?:ab|AB)+$", "ab" * 5000 + "Z"),   # exponential thật
        ]
        for pat, payload in cases:
            t0 = time.monotonic()
            m = regex_search_with_timeout(pat, payload)
            elapsed = time.monotonic() - t0
            self.assertIsNone(m, pat)
            self.assertLess(elapsed, 2.0, pat)

    def test_flagged_legit_pattern_still_matches(self):
        from ctf_downloader.utils.flag_format import (
            regex_search_with_timeout,
            validate_flag,
        )

        self.assertIsNotNone(
            regex_search_with_timeout("(?i)(ptit)+ctf", "xxPTITptitctf"))
        self.assertTrue(
            validate_flag("PTITCTF{abc}", "(?i)^PTITCTF\\{.+\\}$"))

    # ------------------------------------------------------------------
    # Fixes (review de34074): (1) HIGH fail-open — U+0130 'İ' là codepoint
    # duy nhất có .lower() dài 2 ký tự ('i' + U+0307) -> ord() raise
    # TypeError, và crash xảy ra NGOÀI try/except ở call-site
    # _is_risky_pattern (flag_format.py) nên nổ lên caller thay vì trả
    # None an toàn. (2) MED — luật backref-vs-octal lệch sre: \DDD với
    # ĐỦ 3 chữ số octal là octal VÔ ĐIỀU KIỆN kể cả khi nhóm DDD tồn tại.
    # ------------------------------------------------------------------

    def test_norm_literal_keeps_multichar_lower_codepoint(self):
        from ctf_downloader.utils.flag_format import _norm_literal

        expected = "\x00" + format(ord("İ"), "x") + ";"
        # Trước fix: ord('i'+U+0307) raise TypeError.
        self.assertEqual(_norm_literal("İ", True), expected)
        self.assertEqual(_norm_literal("İ", False), expected)
        # Codepoint thường vẫn fold bình thường dưới (?i).
        self.assertEqual(_norm_literal("X", True),
                         "\x00" + format(ord("x"), "x") + ";")

    def test_scanner_survives_multichar_lower_patterns(self):
        from ctf_downloader.utils.flag_format import (
            _scan_dup_alternation as dup,
            regex_search_with_timeout,
        )

        self.assertFalse(dup("(?i)İ"))        # không crash, không có alt
        self.assertFalse(dup("(?i:xİ|xX)"))   # İ giữ nguyên ≠ 'x' đã fold
        self.assertTrue(dup("(?i)(İ|İ)+"))    # trùng nguyên văn vẫn bị chặn
        # Đường công khai không được nổ exception lên caller.
        self.assertIsNotNone(regex_search_with_timeout("(?i)İ", "co İ here"))

    def test_octal_three_digits_unconditional_even_if_group_exists(self):
        from ctf_downloader.utils.flag_format import (
            _scan_dup_alternation as dup,
            regex_search_with_timeout,
        )
        import time

        # sre (docs "\\number"): số có đủ 3 chữ số octal -> LUÔN là octal
        # escape, không bao giờ là group match — dù nhóm 101 tồn tại.
        # Vậy (A|\101)+ với 101 nhóm phía trước là dup thật (\101 ≡ 'A')
        # và phải bị chặn tĩnh; chọn backref-prefix trước đó đã bỏ lỡ.
        pat = "(x)" * 101 + r"(A|\101)+"
        self.assertTrue(dup(pat))
        t0 = time.monotonic()
        m = regex_search_with_timeout(pat, "A" * 10000 + "B")
        elapsed = time.monotonic() - t0
        self.assertIsNone(m, pat)
        self.assertLess(elapsed, 2.0, pat)

    def test_two_digit_ref_stays_backreference_not_octal(self):
        from ctf_downloader.utils.flag_format import _scan_dup_alternation as dup

        # \99 chỉ có 2 chữ số: nhóm 99 tồn tại -> backref THẬT theo sre
        # (đã verify: match 'x'*99). Nhánh 'A' vs \99 khác nhau -> không
        # bị chặn oan sau khi luật 3-octal được ưu tiên.
        self.assertFalse(dup("(x)" * 99 + r"(A|\99)+"))


# ----------------------------------------------------------------------
# Wave #3 fixes (weakness-report-cycle2): W4.1a OverflowError _safe_int,
# W4.1b NaN/Inf literal trong challenges.json, W2.1b os.replace phá symlink.
# ----------------------------------------------------------------------

class TestSafeIntOverflow(unittest.TestCase):
    """CRASH-HIGH-2 (W4.1a): int(float('inf')) raise OverflowError không thuộc
    (TypeError, ValueError) -> sập cả pipeline summary."""

    def setUp(self):
        import tempfile as _tf
        self._tmp = _tf.mkdtemp(prefix="wave3_safeint_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_safe_int_handles_infinity_nan_and_huge_string(self):
        from ctf_downloader.generator.summary_generator import _safe_int

        self.assertEqual(_safe_int(float("inf")), 0)
        self.assertEqual(_safe_int(float("-inf")), 0)
        self.assertEqual(_safe_int(float("nan")), 0)
        self.assertEqual(_safe_int("1e400"), 0)      # float('1e400') -> inf
        # Hành vi cũ giữ nguyên
        self.assertEqual(_safe_int(None), 0)
        self.assertEqual(_safe_int("abc"), 0)
        self.assertEqual(_safe_int(100), 100)
        self.assertEqual(_safe_int("42"), 42)

    def test_summary_pipeline_survives_infinite_points(self):
        from ctf_downloader.models import Challenge, CTFInfo

        challs = [
            Challenge(id=1, name="Inf", category="Web", points=float("inf")),
            Challenge(id=2, name="NaN", category="Web", points=float("nan")),
            Challenge(id=3, name="Ok", category="Web", points=50),
        ]
        info = CTFInfo(title="Wave3CTF", url="https://x.example", challenges=challs)
        path = SummaryGenerator.generate_summary(
            base_output_dir=self._tmp, ctf_info=info,
            all_results={c.id: [] for c in challs},
        )
        self.assertTrue(os.path.exists(path))
        with open(os.path.join(self._tmp, "challenges.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total_points"], 50)


class TestChallengesJsonStrictNoNaN(unittest.TestCase):
    """MINOR-4b (W4.1b): NaN/Infinity literal khiến challenges.json không đọc
    được bởi parser strict JSON — phải sanitize thành None trước khi dump."""

    def setUp(self):
        import tempfile as _tf
        self._tmp = _tf.mkdtemp(prefix="wave3_nan_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_non_finite_points_sanitized_to_null(self):
        from ctf_downloader.models import Challenge, CTFInfo

        challs = [
            Challenge(id=1, name="NanPts", category="Web", points=float("nan")),
            Challenge(id=2, name="InfPts", category="Pwn", points=float("inf")),
        ]
        info = CTFInfo(title="StrictCTF", url="https://x.example", challenges=challs)
        SummaryGenerator.generate_summary(
            base_output_dir=self._tmp, ctf_info=info,
            all_results={c.id: [] for c in challs},
        )
        with open(os.path.join(self._tmp, "challenges.json"), encoding="utf-8") as f:
            text = f.read()

        def _reject_constant(name):
            raise ValueError(f"non-finite JSON constant: {name}")

        strict = json.loads(text, parse_constant=_reject_constant)  # phải parse được
        pts = {c["id"]: c["points"] for c in strict["challenges"]}
        self.assertIsNone(pts[1])
        self.assertIsNone(pts[2])


class TestSymlinkWritePreserved(unittest.TestCase):
    """MINOR-4a (W2.1b): atomic_write_text qua path là symlink phải ghi vào
    ĐÍCH THẬT và giữ nguyên link, không âm thầm thay symlink bằng file thường."""

    def test_atomic_write_text_follows_symlink(self):
        from ctf_downloader.storage.fileio import atomic_write_text

        tmp = tempfile.mkdtemp(prefix="wave3_link_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        target = pathlib.Path(tmp) / "outside_real.md"
        target.write_text("old content\n", encoding="utf-8")
        link = pathlib.Path(tmp) / "sub"
        link.mkdir()
        readme_link = link / "README.md"
        readme_link.symlink_to(target)

        atomic_write_text(readme_link, "new content\n")

        self.assertTrue(readme_link.is_symlink(), "symlink bị thay bằng file thường")
        self.assertEqual(target.read_text(encoding="utf-8"), "new content\n")

    def test_solved_marker_via_symlink_readme_updates_target(self):
        tmp = tempfile.mkdtemp(prefix="wave3_link_ws_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = _make_workspace(tmp)
        repo = WorkspaceRepo(root)
        readme = root / "Web" / "web_basics" / "writeup" / "README.md"

        outside = pathlib.Path(tmp) / "real_target.md"
        outside.write_text("# Web Basics\n- [ ] Solved\nFLAG{...}\n", encoding="utf-8")
        readme.unlink()
        readme.symlink_to(outside)

        changed = repo.write_solved_state([readme], solved=True)

        self.assertEqual(changed, 1)
        self.assertTrue(readme.is_symlink(), "symlink bị os.replace phá vỡ")
        self.assertIn("- [x] Solved", outside.read_text(encoding="utf-8"))



class TestCTFdWindowInitBraceMatch(unittest.TestCase):
    """Deferred-minor: _window_init_value phải scan tới dấu '}' đóng object
    window.init (brace matching) thay vì cắt cứng 8000 ký tự — theme chèn
    nhiều field trước start/end vẫn phải parse được."""

    def _html_with_many_fields(self, n_fields: int) -> str:
        fields = "\n".join(
            f'  field{i}: "{chr(120) * 450}({i}),",' for i in range(n_fields)
        )
        return (
            "<html><head><script>"
            "window.init = {\n" + fields + '\n'
            '  "start": "1700000000",\n'
            '  "end": "1700100000"\n'
            "};"
            "</script></head><body></body></html>"
        )

    def test_window_init_scanned_past_8000_chars(self):
        from ctf_downloader.platforms.ctfd import CTFdPlatform as CTFd

        # ~20 field x padding đủ dài để vượt mốc 8000 ký tự cũ
        html = self._html_with_many_fields(20)
        self.assertGreaterEqual(len(html), 8000 + 200)
        self.assertEqual(CTFd._window_init_value(html, "start"), "1700000000")
        self.assertEqual(CTFd._window_init_value(html, "end"), "1700100000")

    def test_window_init_stops_at_closing_brace(self):
        from ctf_downloader.platforms.ctfd import CTFdPlatform as CTFd

        # Sau '}' của window.init có thêm text chứa pattern start giả ->
        # brace matching phải dừng đúng ở cuối object, không ăn nhầm.
        html = (
            '<script>window.init = { "start": "1111111111" };</script>'
            '<div>"start": "9999999999"</div>'
        )
        self.assertEqual(CTFd._window_init_value(html, "start"), "1111111111")


# ---------------------------------------------------------------------------
# Phase 5 (UI layer refactor) — PullService output discipline:
# transient fetch spinner + ok_summary, Diagnostic cho lỗi nghiêm trọng,
# tổng kết diff `+ name` / `- name` trong --update.
# ---------------------------------------------------------------------------
class TestPullServiceUIDiscipline(unittest.TestCase):
    def setUp(self):
        import io

        from ctf_downloader.services import pull_service
        self.pull_service = pull_service
        self.stderr_buf = io.StringIO()
        # Console ghi vào buffer, width cố định để assert không bị wrap.
        # Patch CẢ bản ở pull_service lẫn bản mà ui.diagnostics.render dùng.
        import rich.console

        from ctf_downloader.ui import diagnostics as ui_diag

        fake_console = rich.console.Console(file=self.stderr_buf, width=200)
        err_patch = mock.patch.object(pull_service, "err_console", fake_console)
        diag_patch = mock.patch.object(ui_diag, "err_console", fake_console)
        err_patch.start()
        diag_patch.start()
        self.addCleanup(err_patch.stop)
        self.addCleanup(diag_patch.stop)
        self._tmp = tempfile.mkdtemp(prefix="arch5_pull_ui_")
        self.out_dir = os.path.join(self._tmp, "out")

    def _stub_dm(self):
        """DownloadManager giả: không mạng, trả danh sách kết quả rỗng."""
        parent = self.pull_service.DownloadManager

        class _StubDM(parent):
            def download_challenge_files(self, *args, **kwargs):
                return []

        return mock.patch.object(self.pull_service, "DownloadManager", _StubDM)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _config(self, threads=2):
        from ctf_downloader.config import DownloaderConfig

        return DownloaderConfig(
            url="https://pull.example.com",
            cookie="session=abc",
            output_dir=self.out_dir,
            threads=threads,
        )

    def _fake_platform(self, n_challenges=2, auth_ok=True):
        from ctf_downloader.models import Challenge, CTFInfo

        plat = mock.MagicMock()
        plat.authenticate.return_value = auth_ok
        plat.ctf_info = CTFInfo(title="PullCTF", url="https://pull.example.com")
        plat.fetch_challenges.return_value = [
            Challenge(id=i + 1, name=f"Chall {chr(65 + i)}", category="Web",
                      points=100)
            for i in range(n_challenges)
        ]
        return plat

    # ---- 1. Fetch phase: spinner transient -> chỉ còn ok_summary ----

    def test_fetch_phase_prints_ok_summary_not_logger_success(self):
        fake_platform = self._fake_platform(n_challenges=2)

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform",
                               return_value=fake_platform), \
             self._stub_dm():
            result = self.pull_service.PullService.run(self._config())

        self.assertTrue(result["ok"])
        out = self.stderr_buf.getvalue()
        # ok_summary: "Đã tải 2 challenges trong X.XXs" (markup đã render)
        self.assertIn("Đã tải 2 challenges", out.replace("[bold]", "")
                                                 .replace("[/]", ""))
        # Log cũ của phase fetch phải biến mất
        self.assertNotIn("Successfully retrieved", out)
        self.assertNotIn("Fetching challenge lists", out)

    def test_fetch_phase_no_challenges_renders_diagnostic(self):
        fake_platform = self._fake_platform(n_challenges=0)

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform",
                               return_value=fake_platform):
            result = self.pull_service.PullService.run(self._config())

        self.assertFalse(result["ok"])
        out = self.stderr_buf.getvalue()
        self.assertIn("error:", out)
        self.assertIn("ACTION REQUIRED", out)  # E1: hint giờ là leaf dưới node kết
        self.assertIn("ctf doctor -u <url>", out)

    def test_detect_failure_renders_diagnostic(self):
        from ctf_downloader.platforms.detector import PlatformDetector

        with mock.patch.object(
                PlatformDetector, "detect_platform",
                side_effect=ValueError("unrecognized url")):
            result = self.pull_service.PullService.run(self._config())

        self.assertFalse(result["ok"])
        out = self.stderr_buf.getvalue()
        self.assertIn("error:", out)
        self.assertIn("Không phát hiện được nền tảng CTF", out)
        self.assertIn("unrecognized url", out)          # cause
        self.assertIn("ACTION REQUIRED", out)  # E1: hint giờ là leaf dưới node kết
        self.assertIn("ctf doctor -u <url>", out)       # hint cụ thể

    def test_auth_failure_renders_warning_diagnostic_and_proceeds(self):
        fake_platform = self._fake_platform(n_challenges=1, auth_ok=False)

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform",
                               return_value=fake_platform), \
             self._stub_dm():
            result = self.pull_service.PullService.run(self._config())

        # Pipeline giữ nguyên hành vi: tiếp tục với tư cách guest
        self.assertTrue(result["ok"])
        out = self.stderr_buf.getvalue()
        self.assertIn("warning:", out)
        self.assertIn("ACTION REQUIRED", out)  # E1: hint giờ là leaf dưới node kết
        self.assertIn("ctf doctor -u <url>", out)

    # ---- 1b. Diagnostic sweep: workspace không ghi được / fail tổng ----

    def test_workspace_write_failure_renders_diagnostic(self):
        fake_platform = self._fake_platform(n_challenges=1)

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform",
                               return_value=fake_platform), \
             mock.patch.object(self.pull_service.os, "makedirs",
                               side_effect=OSError(13, "Permission denied")):
            result = self.pull_service.PullService.run(self._config())

        self.assertFalse(result["ok"])
        out = self.stderr_buf.getvalue()
        self.assertIn("error:", out)
        self.assertIn("Không ghi được workspace", out)
        self.assertIn("ACTION REQUIRED", out)  # E1: hint giờ là leaf dưới node kết
        self.assertIn("quyền ghi", out)          # hint hành động

    def test_total_download_failure_renders_diagnostic_and_proceeds(self):
        fake_platform = self._fake_platform(n_challenges=2)

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform",
                               return_value=fake_platform), \
             mock.patch.object(self.pull_service.PullService, "_full_process",
                               side_effect=RuntimeError("boom")):
            result = self.pull_service.PullService.run(self._config())

        # Hành vi giữ nguyên: pipeline vẫn chạy tới hết (summary + ok=True),
        # chỉ THÊM Diagnostic tổng kết thất bại toàn bộ.
        self.assertTrue(result["ok"])
        out = self.stderr_buf.getvalue()
        self.assertIn("error:", out)
        self.assertIn("Tải thất bại trên toàn bộ 2/2 challenge", out)
        self.assertIn("kiểm tra kết nối mạng và cookie đăng nhập", out)
        self.assertIn("-j 1", out)

    # ---- 2. Incremental update: diff summary +name/-name sorted ----

    def _seed_workspace(self):
        """Full pull nền móng với Alpha/Beta rồi trả platform API chỉ còn delta."""
        from ctf_downloader.models import CTFInfo, Challenge

        base = mock.MagicMock()
        base.authenticate.return_value = True
        base.ctf_info = CTFInfo(title="PullCTF", url="https://pull.example.com")
        base.fetch_challenges.return_value = [
            Challenge(id=1, name="Beta", category="Web"),
            Challenge(id=2, name="Alpha", category="Pwn"),
        ]
        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform", return_value=base), \
             self._stub_dm():
            r1 = self.pull_service.PullService.run(self._config())
        self.assertTrue(r1["ok"])

        api = mock.MagicMock()
        api.authenticate.return_value = True
        api.ctf_info = CTFInfo(title="PullCTF", url="https://pull.example.com")
        api.ctf_info.challenges = [Challenge(id=3, name="delta", category="Web")]
        api.fetch_challenges.return_value = api.ctf_info.challenges
        return api

    def test_update_diff_summary_sorted_plus_minus(self):
        api = self._seed_workspace()

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform", return_value=api), \
             self._stub_dm():
            result = self.pull_service.PullService.run_update(self._config())

        self.assertTrue(result["ok"])
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["missing"], 2)

        lines = [ln.strip() for ln in self.stderr_buf.getvalue().splitlines()
                 if ln.strip().startswith(("+", "-"))]
        # Alphabetical (case-insensitive): Alpha(-), Beta(-), delta(+)
        self.assertEqual(lines, ["- Alpha", "- Beta", "+ delta"])

    def test_update_diff_summary_empty_prints_nothing(self):
        from ctf_downloader.models import Challenge

        api = self._seed_workspace()
        # API trả lại đúng bộ cũ → không có +/- nào
        api.fetch_challenges.return_value = [
            Challenge(id=1, name="Beta", category="Web"),
            Challenge(id=2, name="Alpha", category="Pwn"),
        ]

        with mock.patch.object(self.pull_service.PlatformDetector,
                               "detect_platform", return_value=api), \
             self._stub_dm():
            result = self.pull_service.PullService.run_update(self._config())

        self.assertTrue(result["ok"])
        lines = [ln.strip() for ln in self.stderr_buf.getvalue().splitlines()
                 if ln.strip().startswith(("+", "-"))]
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
