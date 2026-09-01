"""SPEC UI v2 §S1 — selection state (theme tokens sel/done + ui/selection.py).

Khóa contract:
- token ``sel`` = cyan/teal selected state with high-contrast text on a
  subdued teal surface; export hằng SEL_FG/SEL_BG.
- token ``done`` = strike-through muted cho item đã giải hết trong list chọn.
- :func:`ui.selection.selected_row` trả :class:`rich.text.Text` thuần:
  ❯ CHỈ xuất hiện trên dòng được chọn; dòng thường prefix đúng 2 space,
  không glyph; done → tên mang token ``done``.
"""

import re
import unittest

from rich.cells import cell_len
from rich.text import Text

from ctf_downloader import interactive_menu as im
from ctf_downloader.ui import theme
from ctf_downloader.ui.selection import MENU_CURSOR, fit_cells, selected_row


def _span_text(t: Text, span) -> str:
    return t.plain[span.start:span.end]


class ThemeSelectionTokens(unittest.TestCase):
    def test_sel_token_is_teal_semantic_state(self):
        self.assertEqual(theme.SEL_FG, theme.SELECTED_FG)
        self.assertEqual(theme.SEL_BG, theme.SELECTED_BG)
        self.assertNotEqual(theme.SEL_BG, theme.ACCENT)
        self.assertEqual(theme.DEFAULT_STYLES["sel"],
                         f"bold {theme.SEL_FG} on {theme.SEL_BG}")

    def test_done_token_is_strike_muted(self):
        style = theme.DEFAULT_STYLES["done"]
        self.assertTrue(style.startswith("strike"))
        self.assertIn(theme.FG_MUTED, style)

    def test_load_theme_exposes_both_tokens(self):
        styles = theme.load_theme(None).styles
        self.assertIn("sel", styles)
        self.assertIn("done", styles)


class ResponsiveMenuActionTests(unittest.TestCase):
    def test_compact_actions_fit_60_columns_without_wrap(self):
        for key, label in im._main_menu_actions(60):
            self.assertLessEqual(cell_len(f"  [{key}] {label}"), 60)

    def test_wide_and_compact_action_sets_switch_at_breakpoint(self):
        self.assertIs(im._main_menu_actions(100), im._MAIN_ACTIONS_FULL)
        self.assertIs(im._main_menu_actions(60), im._MAIN_ACTIONS_COMPACT)


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


class FitCellsTests(unittest.TestCase):
    """fit_cells — lưới cell-width dùng chung cho cột switcher (MUST uiv2 #4)."""

    def test_noop_when_fits(self):
        self.assertEqual(fit_cells("CTFD", 8), "CTFD")

    def test_pad_to_width_by_display_cells(self):
        out = fit_cells("ab", 6, pad=True)
        self.assertEqual(out, "ab    ")
        self.assertEqual(cell_len(out), 6)

    def test_truncate_ascii_with_ellipsis_exact_width(self):
        out = fit_cells("x" * 13, 8)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(cell_len(out), 8)

    def test_truncate_counts_wide_chars_by_cell_width(self):
        out = fit_cells("あ" * 10, 5)
        self.assertEqual(cell_len(out), 5)
        self.assertTrue(out.endswith("…"))

    def test_empty_padded_fills_full_width(self):
        self.assertEqual(fit_cells("", 4, pad=True), "    ")


class SwitcherColumnWidthTests(unittest.TestCase):
    """§S1.2 + MUST uiv2 #4 — switcher không bao giờ tràn cột dữ liệu.

    Capture trước-fix: platform ``{:<8}`` không cắt → ``CUSTOM_REST0/12
    solved`` (dính vào số solved); title ``[:30]`` cắt cứng theo len() →
    ``Hostile Tak`` + ``CTFD`` dính nhau, không ellipsis. Layout mới:
    ``[nn] TITLE(30 cell) PLATFORM(8 cell) n/m solved`` — title/platform cắt
    theo display width kèm ``…``, ít nhất 1 space ngăn cách các trường.
    """

    WS_LONG = [(
        "/demo/ws-long",
        {"title": "TallDwarf Hosting: Hostile Takeover of the Grid",  # 49 ký tự > 30
         "platform": "custom_restricted",  # 16 ký tự > 8
         "solved_challenges": 6, "total_challenges": 22},
    )]

    #: offset lưới: prefix(2) + '[nn]'(4) + ' '(1) = title bắt đầu cell 7,
    #: rộng đúng 30 cell; sau đó 1 space ngăn cách rồi meta.
    TITLE_AT, TITLE_W = 7, 30

    def _row_plain(self, *, active: bool = False) -> str:
        act = "/demo/ws-long" if active else "/demo/other"
        return im._workspace_rows(self.WS_LONG, active=act)[0].plain

    def test_title_field_never_exceeds_column_and_gets_ellipsis(self):
        for active in (False, True):
            plain = self._row_plain(active=active)
            field = plain[self.TITLE_AT:self.TITLE_AT + self.TITLE_W]
            self.assertEqual(cell_len(field), 30, plain)
            self.assertTrue(field.rstrip().endswith("…"), plain)
            self.assertNotIn("Takeover", field)  # đã cắt, không tràn

    def test_platform_field_fixed_8_cells_then_space_before_solved(self):
        for active in (False, True):
            plain = self._row_plain(active=active)
            meta_at = self.TITLE_AT + self.TITLE_W + 1  # +1 space ngăn cách
            meta = plain[meta_at:]
            plat_field = meta[:im.SWITCHER_PLATFORM_W]
            self.assertLessEqual(cell_len(plat_field), im.SWITCHER_PLATFORM_W)
            self.assertNotIn("CUSTOM_REST6/22", plain)  # bug capture cũ
            self.assertIsNotNone(re.search(r"\S\s+6/22 solved", plain),
                                 f"thiếu space ngăn cách: {plain!r}")

    def test_title_and_meta_never_glued(self):
        # 'Tak' + 'CTFD' dính nhau trong capture trước-fix → giờ luôn có space.
        for active in (False, True):
            plain = self._row_plain(active=active)
            self.assertFalse(re.search(r"\S[A-Z]{3,}\d+/\d+ solved", plain),
                             f"title/platform dính meta: {plain!r}")

    def test_short_fields_keep_grid_aligned_with_long_rows(self):
        ws_short = [("/demo/s", {"title": "SECCON", "platform": "gzctf",
                                 "solved_challenges": 1, "total_challenges": 2})]
        short = im._workspace_rows(ws_short, active="/none")[0].plain
        long_row = self._row_plain()
        self.assertEqual(cell_len(short[:self.TITLE_AT]),
                         cell_len(long_row[:self.TITLE_AT]))
        self.assertIn("GZCTF   ", short)  # pad đủ 8 cell như hàng dài


class MenuHeaderRadarTests(unittest.TestCase):
    """Menu dùng compact UCS_ExOdia AppHeader, không lặp full splash."""

    def test_print_header_renders_compact_brand_not_full_logo(self):
        from unittest.mock import patch

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = "/demo/ws"
        app.cookie = None
        app.token = None

        with patch.object(im, "_menu_console") as mc:
            mc.return_value.width = 120
            with patch.object(mc.return_value, "print") as pr:
                app._print_header()

        rendered = "".join(str(c.plain) for c in pr.call_args_list[0].args[0].renderables) \
            if hasattr(pr.call_args_list[0].args[0], "renderables") \
            else str(pr.call_args_list[0].args[0])
        self.assertIn("UCS_ExOdia // menu", rendered)
        self.assertIn("▰▰▰", rendered)
        self.assertNotIn("██╗   ██╗", rendered, "compact header không được lặp full splash")


    def test_workspace_summary_filters_redundant_identity_and_auth_success(self):
        from unittest.mock import patch

        class Dash:
            def __init__(self, _path):
                pass

            def get_summary_stats(self):
                return {
                    "title": "Demo CTF",
                    "platform": "gzctf",
                    "total_challenges": 10,
                    "solved_challenges": 5,
                    "completion_rate": 50.0,
                    "earned_points": 500,
                    "total_points": 1000,
                    "user": "user-123",
                    "team": "team-secret",
                }

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = "/demo/ws"
        app.cookie = "session=ok"
        app.token = None
        app._suppress_next_brand = False

        rendered = []
        with patch.object(im, "CTFDashboard", Dash), patch.object(im, "_menu_console") as mc:
            mc.return_value.width = 100
            mc.return_value.print.side_effect = (
                lambda value=None, *a, **k: rendered.append(
                    getattr(value, "plain", str(value or ""))
                )
            )
            app._print_header()

        out = "\n".join(rendered)
        self.assertIn("Demo CTF · GZCTF · ws", out)
        self.assertIn("5/10 · 50.0%", out)
        self.assertNotIn("/demo/ws", out)
        self.assertNotIn("user-123", out)
        self.assertNotIn("team-secret", out)
        self.assertNotIn("cookie/token", out)
        self.assertNotIn("auth chưa cấu hình", out)

    def test_workspace_summary_only_surfaces_auth_when_missing(self):
        from unittest.mock import patch

        class Dash:
            def __init__(self, _path):
                pass

            def get_summary_stats(self):
                return {"total_challenges": 0}

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = "/demo/ws"
        app.cookie = None
        app.token = None
        app._suppress_next_brand = True

        rendered = []
        with patch.object(im, "CTFDashboard", Dash), patch.object(im, "_menu_console") as mc:
            mc.return_value.width = 80
            mc.return_value.print.side_effect = (
                lambda value=None, *a, **k: rendered.append(
                    getattr(value, "plain", str(value or ""))
                )
            )
            app._print_header()

        out = "\n".join(rendered)
        self.assertIn("auth chưa cấu hình", out)
        self.assertNotIn("UCS_ExOdia // menu", out)


class MenuWiringTests(unittest.TestCase):
    def test_interactive_menu_reuses_selection_component(self):
        self.assertIs(im.selected_row, selected_row)
        self.assertIs(im.MENU_CURSOR, MENU_CURSOR)


if __name__ == "__main__":
    unittest.main()
