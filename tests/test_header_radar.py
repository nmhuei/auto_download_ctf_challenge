"""UCS_ExOdia compact command-header contract."""
from __future__ import annotations

import unittest

from rich.cells import cell_len

from ctf_downloader.ui.banner import app_header


class TestUCSExOdiaCommandHeader(unittest.TestCase):
    def test_height_exactly_one_line(self):
        for w in (40, 60, 80, 120):
            with self.subTest(width=w):
                lines = app_header(
                    "status",
                    context="ws:PTIT-CTF-2026",
                    timestamp="10:13 UTC+7",
                    width=w,
                ).plain.splitlines()
                self.assertEqual(1, len(lines), f"w={w}: {lines!r}")

    def test_every_line_fits_width_and_has_no_trailing_padding(self):
        for w in (40, 60, 80, 120):
            with self.subTest(width=w):
                t = app_header(
                    "status", context="ws:X", timestamp="10:13 UTC+7", width=w
                )
                for line in t.plain.splitlines():
                    self.assertLessEqual(cell_len(line), w, repr(line))
                    self.assertEqual(line, line.rstrip(), repr(line))

    def test_line1_has_brand_command_and_version(self):
        first = app_header("status", timestamp="10:13", width=80).plain.splitlines()[0]
        self.assertIn("UCS_ExOdia", first)
        self.assertIn("// status", first)
        self.assertIn("v3", first)

    def test_inline_segmented_operation_rail(self):
        t = app_header("status", timestamp="10:13", width=80)
        line = t.plain.splitlines()[0]
        self.assertGreaterEqual(line.count("▰"), 16)
        rail_styles = {
            span.style
            for span in t.spans
            if "▰" in t.plain[span.start:span.end]
        }
        self.assertGreaterEqual(len(rail_styles), 8)

    def test_payload_context_timestamp_present(self):
        plain = app_header(
            "sync", context="ws:PTIT", timestamp="10:13 UTC+7", width=120
        ).plain
        for piece in ("sync", "ws:PTIT", "10:13 UTC+7"):
            self.assertIn(piece, plain)

    def test_long_context_truncates_instead_of_wrapping(self):
        t = app_header(
            "doctor",
            context="/very/" + ("long/" * 40) + "workspace",
            timestamp="10:13 UTC+7",
            width=40,
        )
        lines = t.plain.splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn("…", lines[0])
        self.assertLessEqual(cell_len(lines[0]), 40)

    def test_no_context_does_not_add_redundant_tagline(self):
        t = app_header("pull", width=80)
        self.assertEqual(1, len(t.plain.splitlines()))
        self.assertNotIn("CTF OPERATIONS FRAMEWORK", t.plain)


if __name__ == "__main__":
    unittest.main()
