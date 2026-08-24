"""Phase 2 tests: ctf_downloader.storage (fileio + constants)."""
import json
import pathlib
import tempfile
import unittest

from ctf_downloader.storage.fileio import atomic_write_json, atomic_write_text, locked_update_json
from ctf_downloader.storage import constants


class TestFileIO(unittest.TestCase):
    def test_atomic_roundtrip_and_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.json"
            atomic_write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})
            p.write_text("{corrupt")            # hỏng
            out = locked_update_json(p, lambda d: {**(d or {}), "b": 2})
            self.assertEqual(out, {"b": 2})
            self.assertTrue((pathlib.Path(d) / "x.json.bak").exists())

    def test_atomic_write_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "sub" / "note.txt"
            p.parent.mkdir()
            atomic_write_text(p, "hello")
            self.assertEqual(p.read_text(encoding="utf-8"), "hello")
            # no leftover tmp files
            self.assertEqual(list(p.parent.glob("*.tmp")), [])

    def test_locked_update_normal_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "state.json"
            atomic_write_json(p, {"n": 1})
            out = locked_update_json(p, lambda d: {**d, "n": d["n"] + 1})
            self.assertEqual(out, {"n": 2})
            self.assertEqual(json.loads(p.read_text()), {"n": 2})

    def test_locked_update_mutator_returns_none_keeps_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "state.json"
            atomic_write_json(p, {"keep": True})
            out = locked_update_json(p, lambda d: None)
            self.assertEqual(out, {"keep": True})


class TestConstants(unittest.TestCase):
    def test_solved_markers(self):
        self.assertEqual(constants.SOLVED_DONE, "- [x] Solved")
        self.assertEqual(constants.SOLVED_TODO, "- [ ] Solved")
        self.assertIsInstance(constants.SOLVED_MARKERS_DONE, tuple)
        self.assertIn("- [x] Solved", constants.SOLVED_MARKERS_DONE)
        self.assertIn("✅ Solved", constants.SOLVED_MARKERS_DONE)
        self.assertIn("Status: ✅", constants.SOLVED_MARKERS_DONE)

    def test_format_constants(self):
        self.assertIn("{info}", constants.TARGET_CONNECTION_FMT)
        self.assertIn("{total_files}", constants.SUMMARY_FILES_LINE)
        self.assertTrue(constants.LIVE_RANK_PREFIX.startswith("- **Live Rank**"))

    def test_scalar_constants(self):
        self.assertEqual(constants.SOLVE_VAR_NAMES, ("HOST", "PORT", "TARGET_URL"))
        self.assertEqual(constants.FLAG_PLACEHOLDER, "FLAG{...}")
        self.assertEqual(constants.DEFAULT_CATEGORY, "Misc")


if __name__ == "__main__":
    unittest.main()
