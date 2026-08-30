"""CLI contract tests for destructive history operations and completions."""

import json
import os
import tempfile
import unittest
from argparse import Namespace

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.cli_commands import handle_history
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


class TestHistoryCliContracts(unittest.TestCase):
    def test_prune_and_clear_are_mutually_exclusive(self):
        parser = build_unified_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["history", "--prune", "FLAG{x}", "--clear"])

    def test_prune_flag_is_exact_not_substring(self):
        with tempfile.TemporaryDirectory() as ws:
            repo = WorkspaceRepo(ws)
            repo.save_submit_history({
                "entries": [
                    {
                        "challenge_id": 1,
                        "flag": "FLAG{alpha}",
                        "result": "incorrect",
                        "timestamp": "t1",
                    },
                    {
                        "challenge_id": 2,
                        "flag": "FLAG{beta}",
                        "result": "incorrect",
                        "timestamp": "t2",
                    },
                ]
            })

            # A short substring used to delete both entries.
            handle_history(Namespace(
                workspace=ws,
                prune="FLAG",
                clear=False,
                show_all=False,
                tail=100,
            ))
            self.assertEqual(
                len(repo.load_submit_history()["entries"]),
                2,
            )

            handle_history(Namespace(
                workspace=ws,
                prune="FLAG{alpha}",
                clear=False,
                show_all=False,
                tail=100,
            ))
            remaining = repo.load_submit_history()["entries"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["flag"], "FLAG{beta}")

    def test_history_completions_include_destructive_options(self):
        root = os.path.dirname(os.path.abspath(__file__))
        bash = open(
            os.path.join(root, "completions", "ctf.bash"),
            encoding="utf-8",
        ).read()
        zsh = open(
            os.path.join(root, "completions", "ctf.zsh"),
            encoding="utf-8",
        ).read()
        for text in (bash, zsh):
            self.assertIn("--prune", text)
            self.assertIn("--clear", text)


if __name__ == "__main__":
    unittest.main()
