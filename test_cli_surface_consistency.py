"""CLI surface consistency: argparse -> completions -> generated docs."""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import unittest

from ctf_downloader.cli import build_unified_parser


ROOT = pathlib.Path(__file__).resolve().parent


def iter_parsers(parser, prefix=()):
    sub = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    if sub is None:
        return
    seen = set()
    for name, child in sub.choices.items():
        if id(child) in seen:
            continue
        seen.add(id(child))
        path = (*prefix, name)
        yield path, child
        yield from iter_parsers(child, path)


def canonical_long_options():
    result = set()
    for _path, parser in iter_parsers(build_unified_parser()):
        for action in parser._actions:
            result.update(
                opt for opt in action.option_strings
                if opt.startswith("--") and opt != "--help"
            )
    return result


class TestCliSurfaceConsistency(unittest.TestCase):
    def test_every_long_option_is_present_in_bash_and_zsh_completion(self):
        options = canonical_long_options()
        bash = (ROOT / "completions" / "ctf.bash").read_text(encoding="utf-8")
        zsh = (ROOT / "completions" / "ctf.zsh").read_text(encoding="utf-8")
        missing_bash = sorted(o for o in options if o not in bash)
        missing_zsh = sorted(o for o in options if o not in zsh)
        self.assertEqual(missing_bash, [], f"bash completion missing: {missing_bash}")
        self.assertEqual(missing_zsh, [], f"zsh completion missing: {missing_zsh}")

    def test_generated_readme_and_man_option_indexes_are_fresh(self):
        proc = subprocess.run(
            [sys.executable, "scripts/generate_cli_option_index.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            proc.stdout + proc.stderr,
        )

    def test_security_sensitive_pull_flags_parse(self):
        parser = build_unified_parser()
        args = parser.parse_args([
            "pull",
            "-u",
            "https://ctf.test",
            "--verify-downloads",
            "strict",
            "--allow-private-redirects",
        ])
        self.assertEqual(args.verify_downloads, "strict")
        self.assertTrue(args.allow_private_redirects)


if __name__ == "__main__":
    unittest.main()
