"""Phase 5 tests: storage.workspace_repo (WorkspaceRepo) + chuyển 4 module dùng repo.

Task 3: class TestWorkspaceRepo.
Task 4: characterization tests cho dashboard/instance_manager/ranking/submitter
        + pin-test throttle registry (carry item).
"""
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
