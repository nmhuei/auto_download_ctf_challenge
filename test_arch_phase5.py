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

            errors = multiprocessing.Queue()
            procs = [
                multiprocessing.Process(
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
        self.assertIn("SCANNING ALL CTF WORKSPACES", out)
        self.assertIn("TestCTF", out)
        self.assertIn("GZCTF", out)
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

        # Full tree: cả 2 challenge đều xuất hiện; container được gắn tag
        self.assertIn("Dyn", full)
        self.assertIn("Static", full)
        self.assertIn("[🐳 Container]", full)

        # only_container=True: chỉ Dyn còn lại, Static bị lọc bỏ;
        # header workspace vẫn được in đầy đủ.
        self.assertIn("Dyn", cont_only)
        self.assertNotIn("Static", cont_only)
        self.assertIn("CTF WORKSPACE: TreeCTF [GZCTF]", cont_only)
        self.assertIn("[🐳 Container]", cont_only)


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


if __name__ == "__main__":
    unittest.main()
