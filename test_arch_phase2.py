"""Phase 2 tests: ctf_downloader.storage (fileio + constants)."""
import json
import multiprocessing
import pathlib
import tempfile
import unittest

from ctf_downloader.storage.fileio import atomic_write_json, atomic_write_text, locked_update_json
from ctf_downloader.storage import constants


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
        self.assertEqual(
            constants.SUMMARY_FILES_LINE_PREFIX,
            "- **Total Files Downloaded**:",
        )
        self.assertTrue(constants.SUMMARY_FILES_LINE.startswith(constants.SUMMARY_FILES_LINE_PREFIX))
        self.assertTrue(constants.LIVE_RANK_PREFIX.startswith("- **Live Rank**"))

    def test_scalar_constants(self):
        self.assertEqual(constants.SOLVE_VAR_NAMES, ("HOST", "PORT", "TARGET_URL"))
        self.assertEqual(constants.FLAG_PLACEHOLDER, "FLAG{...}")
        self.assertEqual(constants.DEFAULT_CATEGORY, "Misc")
        self.assertEqual(constants.SOLVED_EMOJI_DONE, "✅ Solved")
        self.assertIn(constants.SOLVED_EMOJI_DONE, constants.SOLVED_MARKERS_DONE)


if __name__ == "__main__":
    unittest.main()
