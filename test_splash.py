"""UCS_ExOdia splash contract: width tiers, spectral rail and menu wiring."""
from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from rich.cells import cell_len
from rich.console import Console

import ctf_downloader.interactive_menu as im
from ctf_downloader.ui import brand as brand_mod
from ctf_downloader.ui.brand import FULL_LOGO, WIDE_THRESHOLD, operation_rail
from ctf_downloader.ui.splash import splash


def _render(text) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=200).print(text)
    return buf.getvalue()


class TestSplashTierByWidth(unittest.TestCase):
    def test_80_and_above_get_full_ucs_exodia_brand(self):
        for w in (WIDE_THRESHOLD, 100, 120):
            with self.subTest(width=w):
                lines = splash(w).plain.splitlines()
                self.assertEqual(15, len(lines))
                self.assertIn("██╗   ██╗", lines[0])
                self.assertIn("UCS_ExOdia", "\n".join(lines))
                self.assertEqual("READY ●", lines[-1].strip())

    def test_below_80_get_compact_three_line_brand(self):
        for w in (40, 46, 70, 79):
            with self.subTest(width=w):
                lines = splash(w).plain.splitlines()
                self.assertEqual(3, len(lines))
                self.assertIn("UCS_ExOdia", lines[0])
                self.assertIn("▰", lines[1])
                self.assertIn("CTF OPERATIONS", lines[2])

    def test_exact_boundary_79_vs_80(self):
        self.assertNotEqual(splash(79).plain, splash(80).plain)
        self.assertEqual(3, len(splash(WIDE_THRESHOLD - 1).plain.splitlines()))
        self.assertEqual(15, len(splash(WIDE_THRESHOLD).plain.splitlines()))

    def test_width_none_uses_brand_terminal_probe(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "120", "LINES": "40"}):
            with mock.patch.object(
                brand_mod.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((120, 40)),
            ):
                self.assertEqual(15, len(splash().plain.splitlines()))
            with mock.patch.object(
                brand_mod.shutil,
                "get_terminal_size",
                return_value=os.terminal_size((70, 24)),
            ):
                self.assertEqual(3, len(splash().plain.splitlines()))

    def test_full_logo_uses_letter_O_not_zero_glyph(self):
        raw = "\n".join(FULL_LOGO)
        self.assertIn("██╔═══██╗", raw)
        self.assertIn("██║   ██║", raw)
        self.assertNotIn("██╔═████╗", raw)
        self.assertNotIn("██║██╔██║", raw)


class TestSplashGeometryAndGradient(unittest.TestCase):
    def test_no_line_exceeds_requested_terminal_width(self):
        for w in (40, 46, 60, 70, 79, 80, 100, 120):
            with self.subTest(width=w):
                for line in splash(w).plain.splitlines():
                    self.assertLessEqual(cell_len(line), w, repr(line))
                    self.assertEqual(line, line.rstrip(), repr(line))

    def test_full_logo_first_row_aligns_with_body_rows(self):
        for w in (80, 100, 120):
            with self.subTest(width=w):
                rows = splash(w).plain.splitlines()[:6]
                left_edges = [len(row) - len(row.lstrip()) for row in rows]
                # Rows 1-5 are the logo body and must share one block edge.
                # Row 6 intentionally begins with one intrinsic FIGlet space.
                self.assertEqual([left_edges[0]] * 5, left_edges[:5])
                self.assertEqual(left_edges[0] + 1, left_edges[5])

    def test_both_tiers_keep_brand_framework_and_rail(self):
        for w in (80, 79):
            with self.subTest(width=w):
                plain = splash(w).plain
                self.assertIn("UCS_ExOdia", plain)
                self.assertIn("CTF OPERATIONS FRAMEWORK", plain)
                self.assertIn("▰", plain)
                self.assertIn("v3", plain)

    def test_rail_has_eight_segments_and_many_color_steps(self):
        rail = operation_rail(cells_per_stage=4, separator="  ")
        self.assertEqual(32, rail.plain.count("▰"))
        self.assertEqual(7, rail.plain.count("  "))
        styles = {span.style for span in rail.spans}
        self.assertGreaterEqual(len(styles), 24)

    def test_plain_text_contains_no_embedded_ansi(self):
        for w in (80, 79):
            self.assertNotIn("\x1b", splash(w).plain)


class TestMenuIntegration(unittest.TestCase):
    def _launch_with_spies(self):
        con = Console(file=io.StringIO(), force_terminal=False, width=120)
        calls = []

        def spy(width=None):
            calls.append(width)
            return splash(120 if width is None else width)

        class StubApp:
            def __init__(self, **kwargs):
                pass

            def run(self):
                im._menu_console().print(im.app_header("menu", width=120))

        with mock.patch.object(im, "splash", spy), \
                mock.patch.object(im, "CTFInteractiveConsole", StubApp), \
                mock.patch.object(im, "_menu_console", lambda: con):
            im.launch_interactive_menu()
        return calls, con.file.getvalue()

    def test_splash_called_exactly_once_on_launch(self):
        calls, _ = self._launch_with_spies()
        self.assertEqual([None], calls)

    def test_splash_printed_before_compact_header(self):
        _, out = self._launch_with_spies()
        art_idx = out.find("██╗   ██╗")
        header_idx = out.find("UCS_ExOdia // menu")
        self.assertGreaterEqual(art_idx, 0)
        self.assertGreater(header_idx, art_idx)

    def test_full_logo_appears_only_once(self):
        _, out = self._launch_with_spies()
        self.assertEqual(1, out.count("██╗   ██╗"))


class TestNonTTY(unittest.TestCase):
    def test_default_call_without_tty_renders_plain(self):
        with mock.patch.object(
            brand_mod.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((80, 24)),
        ):
            t = splash()
        self.assertEqual(15, len(t.plain.splitlines()))
        out = _render(t)
        self.assertIn("UCS_ExOdia", out)
        self.assertIn("CTF OPERATIONS FRAMEWORK", out)
        self.assertNotIn("\x1b", out)

    def test_print_via_stderr_console_like_menu_does_not_crash(self):
        buf = io.StringIO()
        con = Console(file=buf, stderr=True, force_terminal=False, width=80)
        con.print(splash(80))
        self.assertIn("UCS_ExOdia", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
