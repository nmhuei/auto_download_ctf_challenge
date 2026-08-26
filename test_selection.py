"""SPEC UI v2 §S1 — selection state (theme tokens sel/done + ui/selection.py).

Khóa contract:
- token ``sel`` = reverse highlight ``#14100A on #FFB000`` (fg near-black
  trên nền accent amber — không hue mới); export hằng SEL_FG/SEL_BG.
- token ``done`` = strike-through muted cho item đã giải hết trong list chọn.
- :func:`ui.selection.selected_row` trả :class:`rich.text.Text` thuần:
  ❯ CHỈ xuất hiện trên dòng được chọn; dòng thường prefix đúng 2 space,
  không glyph; done → tên mang token ``done``.
"""

import unittest

from rich.cells import cell_len
from rich.text import Text

from ctf_downloader import interactive_menu as im
from ctf_downloader.ui import theme
from ctf_downloader.ui.selection import MENU_CURSOR, selected_row


def _span_text(t: Text, span) -> str:
    return t.plain[span.start:span.end]


class ThemeSelectionTokens(unittest.TestCase):
    def test_sel_token_is_reverse_amber(self):
        self.assertEqual(theme.SEL_FG, "#14100A")
        self.assertEqual(theme.SEL_BG, theme.ACCENT)  # trùng accent Amber Refit
        self.assertEqual(theme.DEFAULT_STYLES["sel"],
                         f"{theme.SEL_FG} on {theme.SEL_BG}")

    def test_done_token_is_strike_muted(self):
        style = theme.DEFAULT_STYLES["done"]
        self.assertTrue(style.startswith("strike"))
        self.assertIn(theme.FG_MUTED, style)

    def test_load_theme_exposes_both_tokens(self):
        styles = theme.load_theme(None).styles
        self.assertIn("sel", styles)
        self.assertIn("done", styles)


class SelectedRowTests(unittest.TestCase):
    def test_cursor_glyph_only_on_selected_line(self):
        self.assertIn(MENU_CURSOR, selected_row("Web A", selected=True).plain)
        self.assertNotIn(MENU_CURSOR, selected_row("Web A").plain)

    def test_non_selected_prefix_exactly_two_spaces_no_glyph(self):
        t = selected_row("abc")
        self.assertEqual(t.plain, "  abc")

    def test_selected_row_uses_sel_token_on_cursor_and_label(self):
        t = selected_row("[1] Clone", selected=True)
        self.assertTrue(t.spans, "selected row phải có span màu")
        self.assertTrue(all(s.style == "sel" for s in t.spans))
        self.assertIn(_span_text(t, t.spans[0]).strip(), (MENU_CURSOR, ""))

    def test_plain_row_defaults_to_fg_base(self):
        t = selected_row("abc")
        styles = {s.style for s in t.spans}
        self.assertIn(theme.FG_BASE, styles)

    def test_done_row_strike_token_on_label_no_cursor(self):
        t = selected_row("old ctf", done=True)
        self.assertNotIn(MENU_CURSOR, t.plain)
        done_spans = [s for s in t.spans if s.style == "done"]
        self.assertTrue(done_spans)
        covered = "".join(_span_text(t, s) for s in done_spans)
        self.assertIn("old ctf", covered)

    def test_selected_wins_over_done_visual(self):
        t = selected_row("full clear", selected=True, done=True)
        self.assertTrue(all(s.style == "sel" for s in t.spans))

    def test_width_pads_reverse_background_to_full_columns(self):
        t = selected_row("ab", selected=True, width=6)
        self.assertGreaterEqual(cell_len(t.plain), 2 + 6)
        label_span = t.spans[-1]
        self.assertEqual(_span_text(t, label_span), "ab    ")

    def test_width_none_keeps_label_verbatim(self):
        t = selected_row("ab", selected=True)
        self.assertEqual(t.plain, "❯ ab")


class WorkspaceSwitcherRowsTests(unittest.TestCase):
    """§S1.2 — switcher workspace: active reverse full-row, giải xong 100% → done."""

    WS = [
        ("/demo/ws-a", {"title": "SECCON 2026 Quals", "platform": "gzctf",
                        "solved_challenges": 9, "total_challenges": 22}),
        ("/demo/ws-b", {"title": "PTIT CTF 2026", "platform": "gzctf",
                        "solved_challenges": 14, "total_challenges": 14}),
    ]

    def test_active_row_full_reverse_and_no_duplicate_suffix(self):
        rows = im._workspace_rows(self.WS, active="/demo/ws-a")
        self.assertIn(MENU_CURSOR, rows[0].plain)
        self.assertNotIn("đang dùng", rows[0].plain)  # thay suffix cũ bằng reverse
        self.assertTrue(all(s.style == "sel" for s in rows[0].spans))

    def test_completed_workspace_gets_done_strike(self):
        rows = im._workspace_rows(self.WS, active="/demo/ws-a")
        done_spans = [s for s in rows[1].spans if s.style == "done"]
        self.assertTrue(done_spans)
        covered = "".join(_span_text(rows[1], s) for s in done_spans)
        self.assertIn("PTIT CTF 2026", covered)
        self.assertNotIn(MENU_CURSOR, rows[1].plain)


class MenuWiringTests(unittest.TestCase):
    def test_interactive_menu_reuses_selection_component(self):
        self.assertIs(im.selected_row, selected_row)
        self.assertIs(im.MENU_CURSOR, MENU_CURSOR)


if __name__ == "__main__":
    unittest.main()
