"""Splash logo dual-tier (DECISION_LOGO.md §4) — hợp đồng tier + tích hợp menu.

Ràng buộc chốt của user: terminal ≥ 80 cols dùng cand_1 (big, khung
box-drawing + scanline, 78×13); < 80 cols dùng cand_6 (pagga HUD rail ▍,
46×6). Art nhúng NGUYÊN VĂN dạng hằng text — KHÔNG pyfiglet lúc runtime,
KHÔNG màu/ANSI trộn vào art string. Menu gọi ``splash()`` ĐÚNG MỘT LẦN khi
khởi chạy, trước radar AppHeader đầu tiên.
"""
from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from rich.cells import cell_len
from rich.console import Console

import ctf_downloader.interactive_menu as im
from ctf_downloader.ui import splash as splash_mod
from ctf_downloader.ui.splash import WIDE_THRESHOLD, splash


def _render(text) -> str:
    """Render qua console non-TTY — rich tự strip mọi ANSI."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=200).print(text)
    return buf.getvalue()


class TestSplashTierByWidth(unittest.TestCase):
    """Chọn tier đúng theo chiều rộng giả lập (79 vs 80 là ranh giới)."""

    def test_80_and_above_get_big_boxframe(self):
        for w in (WIDE_THRESHOLD, 100, 120):
            with self.subTest(width=w):
                t = splash(w)
                lines = t.plain.splitlines()
                self.assertEqual(13, len(lines))
                self.assertTrue(lines[0].startswith("┌"))
                self.assertTrue(lines[-1].startswith("░▒▓"))

    def test_below_80_get_pagga_compact(self):
        for w in (46, 70, 79):
            with self.subTest(width=w):
                t = splash(w)
                lines = t.plain.splitlines()
                self.assertEqual(6, len(lines))
                self.assertTrue(lines[0].startswith("▍"))

    def test_exact_boundary_79_vs_80(self):
        self.assertNotEqual(splash(79).plain, splash(80).plain)
        self.assertEqual(splash(79).plain, splash(WIDE_THRESHOLD - 1).plain)
        self.assertEqual(splash(80).plain, splash(WIDE_THRESHOLD).plain)

    def test_width_none_or_falsy_falls_back_to_terminal_size(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "120", "LINES": "40"}):
            with mock.patch.object(splash_mod.shutil, "get_terminal_size",
                                   return_value=os.terminal_size((120, 40))):
                self.assertEqual(13, len(splash().plain.splitlines()))
            with mock.patch.object(splash_mod.shutil, "get_terminal_size",
                                   return_value=os.terminal_size((70, 24))):
                self.assertEqual(6, len(splash().plain.splitlines()))

    def test_art_reproduces_embedded_constant_verbatim(self):
        self.assertEqual(
            splash_mod._SPLASH_BIG.rstrip("\n").splitlines(),
            splash(80).plain.splitlines(),
        )
        self.assertEqual(
            splash_mod._SPLASH_NARROW.rstrip("\n").splitlines(),
            splash(79).plain.splitlines(),
        )


class TestSplashGeometry(unittest.TestCase):
    """Không dòng nào vượt width tier tương ứng; brand + tagline còn nguyên."""

    def test_no_line_exceeds_tier_width(self):
        cases = ((80, 78), (120, 78), (79, 46), (70, 46))
        for w, tier_w in cases:
            with self.subTest(width=w):
                for ln in splash(w).plain.splitlines():
                    self.assertLessEqual(cell_len(ln), tier_w, repr(ln))

    def test_no_line_exceeds_requested_terminal_width(self):
        for w in (80, 100, 79, 50):
            with self.subTest(width=w):
                for ln in splash(w).plain.splitlines():
                    self.assertLessEqual(cell_len(ln), w, repr(ln))

    def test_both_tiers_keep_brand_tagline_and_scanline(self):
        for w in (80, 79):
            with self.subTest(width=w):
                plain = splash(w).plain
                self.assertIn("capture-the-flag", plain)
                self.assertIn("v3◢", plain)
                self.assertTrue(set("░▒▓") <= set(plain))

    def test_art_is_pure_text_no_ansi_embedded(self):
        for w in (80, 79):
            with self.subTest(width=w):
                plain = splash(w).plain
                self.assertNotIn("\x1b", plain)


class TestMenuIntegration(unittest.TestCase):
    """``ctf menu`` gọi splash ĐÚNG MỘT LẦN, trước radar AppHeader đầu tiên."""

    def _launch_with_spies(self):
        con = Console(file=io.StringIO(), force_terminal=False, width=120)
        calls = []

        def spy(width=None):
            calls.append(width)
            return splash(width)

        class StubApp:
            def __init__(self, **kwargs):
                pass

            def run(self):
                # Vòng lặp thật: mỗi vòng in AppHeader radar trước tiên.
                im._menu_console().print(im.app_header("menu", width=120))

        with mock.patch.object(im, "splash", spy), \
                mock.patch.object(im, "CTFInteractiveConsole", StubApp), \
                mock.patch.object(im, "_menu_console", lambda: con):
            im.launch_interactive_menu()
        return calls, con.file.getvalue()

    def test_splash_called_exactly_once_on_launch(self):
        calls, out = self._launch_with_spies()
        self.assertEqual(1, len(calls), f"splash gọi {len(calls)} lần")
        self.assertIsNone(calls[0])

    def test_splash_printed_before_first_radar_header(self):
        _, out = self._launch_with_spies()
        # Art mở màn bằng khung ┌; radar AppHeader bằng dải ░░▒▒▓▓ full-width.
        art_idx = out.find("┌")
        radar_idx = out.find("░░▒▒▓▓")
        self.assertGreaterEqual(art_idx, 0, "splash không được in")
        self.assertGreater(radar_idx, art_idx,
                           "phải in splash TRƯỚC radar đầu tiên")

    def test_splash_appears_only_once_in_output(self):
        _, out = self._launch_with_spies()
        # Khung trên cand_1 chỉ tồn tại ở splash (radar không có box-drawing).
        self.assertEqual(1, out.count("┌"))


class TestNonTTY(unittest.TestCase):
    """Non-TTY vẫn render plain, không crash, không ANSI."""

    def test_default_call_without_tty_renders_plain(self):
        # Pytest capture → stdout/stderr không phải TTY; ép luôn môi trường
        # sạch để get_terminal_size rơi về fallback (80, 24).
        env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
        with mock.patch.dict(os.environ, env, clear=True):
            t = splash()
        self.assertEqual(13, len(t.plain.splitlines()))
        out = _render(t)
        # Font figlet vẽ chữ CTF-TOOLKIT bằng glyph — nhận diện qua khung
        # box-drawing + tagline, không phải substring "CTF" literal.
        self.assertIn("┌", out)
        self.assertIn("capture-the-flag", out.replace(" ", ""))
        self.assertNotIn("\x1b", out)

    def test_print_via_stderr_console_like_menu_does_not_crash(self):
        buf = io.StringIO()
        con = Console(file=buf, stderr=True, force_terminal=False, width=80)
        con.print(splash())
        self.assertIn("┌", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
