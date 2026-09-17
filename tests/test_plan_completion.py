"""Unit tests verifying completed plan items:
1. WorkspaceRepo.prune_submit_history / clear_submit_history & CLI ctf history --clear / --prune
2. log_meter_percentage in ui/widgets.py
3. cleanup_stale_locks in storage/fileio.py
4. WorkspaceBuilder.register_solver_template custom solver hook
"""

import os
import shutil
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

from ctf_downloader.cli_commands import handle_history
from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.models import Challenge
from ctf_downloader.storage.fileio import cleanup_stale_locks
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.ui.widgets import log_meter_percentage


class TestPlanCompletionFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_plan_comp_")
        self.repo = WorkspaceRepo(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        WorkspaceBuilder._CUSTOM_SOLVER_TEMPLATES.clear()

    # ------------------------------------------------------------------
    # 1. Prune & Clear Submit History
    # ------------------------------------------------------------------
    def test_prune_and_clear_submit_history(self):
        hist_data = {
            "entries": [
                {"challenge_id": 1, "flag": "FLAG{test1}", "result": "correct", "timestamp": "2026-08-27T10:00:00Z"},
                {"challenge_id": 2, "flag": "FLAG{test2}", "result": "incorrect", "timestamp": "2026-08-27T10:05:00Z"},
                {"challenge_id": 3, "flag": "FLAG{junk}", "result": "incorrect", "timestamp": "2026-08-27T10:10:00Z"},
            ]
        }
        self.repo.save_submit_history(hist_data)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 3)

        # Prune entry for challenge_id == 2
        removed = self.repo.prune_submit_history(lambda e: e.get("challenge_id") == 2)
        self.assertEqual(removed, 1)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 2)
        self.assertEqual([e["challenge_id"] for e in loaded["entries"]], [1, 3])

        # Prune by flag
        removed = self.repo.prune_submit_history(lambda e: e.get("flag") == "FLAG{junk}")
        self.assertEqual(removed, 1)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 1)
        self.assertEqual(loaded["entries"][0]["flag"], "FLAG{test1}")

        # Clear all
        removed = self.repo.clear_submit_history()
        self.assertEqual(removed, 1)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 0)

    def test_cli_history_prune_and_clear(self):
        hist_data = {
            "entries": [
                {"challenge_id": 10, "flag": "FLAG{web_10}", "result": "correct", "timestamp": "2026-08-27T10:00:00Z"},
                {"challenge_id": 20, "flag": "FLAG{pwn_20}", "result": "incorrect", "timestamp": "2026-08-27T10:05:00Z"},
            ]
        }
        self.repo.save_submit_history(hist_data)

        # CLI --prune 10
        args = Namespace(workspace=self.tmp_dir, clear=False, prune="10", show_all=True)
        handle_history(args)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 1)
        self.assertEqual(loaded["entries"][0]["challenge_id"], 20)

        # CLI --clear
        args = Namespace(workspace=self.tmp_dir, clear=True, prune=None, show_all=True)
        handle_history(args)
        loaded = self.repo.load_submit_history()
        self.assertEqual(len(loaded["entries"]), 0)

    # ------------------------------------------------------------------
    # 2. Log Meter Percentage
    # ------------------------------------------------------------------
    def test_log_meter_percentage(self):
        self.assertEqual(log_meter_percentage(0, 1000), 0.0)
        self.assertEqual(log_meter_percentage(-10, 1000), 0.0)
        self.assertEqual(log_meter_percentage(1000, 1000), 100.0)
        self.assertEqual(log_meter_percentage(2000, 1000), 100.0)

        # Log scale: 10 out of 1000 should be ~34.6%
        pct_10 = log_meter_percentage(10, 1000)
        pct_100 = log_meter_percentage(100, 1000)
        self.assertTrue(30.0 < pct_10 < 40.0)
        self.assertTrue(60.0 < pct_100 < 70.0)

    # ------------------------------------------------------------------
    # 3. Cleanup Stale Locks
    # ------------------------------------------------------------------
    def test_cleanup_stale_locks(self):
        lock1 = Path(self.tmp_dir) / "test1.lock"
        lock2 = Path(self.tmp_dir) / "test2.lock"
        lock1.write_text("dummy")
        lock2.write_text("dummy")

        # Set mtime back by 2 hours
        old_time = time.time() - 7200
        os.utime(lock1, (old_time, old_time))
        os.utime(lock2, (old_time, old_time))

        # Cleanup locks older than 3600s
        cleaned = cleanup_stale_locks(self.tmp_dir, max_age_seconds=3600.0)
        self.assertEqual(cleaned, 2)
        self.assertFalse(lock1.exists())
        self.assertFalse(lock2.exists())

    # ------------------------------------------------------------------
    # 4. WorkspaceBuilder Custom Solver Templates
    # ------------------------------------------------------------------
    def test_custom_solver_template_hook(self):
        def my_custom_solver(chal, conns):
            return f"# CUSTOM TEMPLATE FOR {chal.name}\n# Pwned with custom tool"

        WorkspaceBuilder.register_solver_template("custom_rev", my_custom_solver)

        chal = Challenge(id="101", name="CustomChallenge", category="custom_rev")
        res = WorkspaceBuilder._generate_solve_template(chal, [])
        self.assertIn("# CUSTOM TEMPLATE FOR CustomChallenge", res)
        self.assertIn("# Pwned with custom tool", res)


if __name__ == "__main__":
    unittest.main()
