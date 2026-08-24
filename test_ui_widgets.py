"""Tests for ctf_downloader.ui.widgets (btop-pattern widget layer)."""

import unittest

from rich.text import Text

from ctf_downloader.ui.widgets import (
    BRAILLE_UP,
    braille_graph,
    footer_bar,
    gradient,
    meter,
    shortcut_title,
)


def _strip(text: Text) -> str:
    return text.plain


class GradientTests(unittest.TestCase):
    def test_endpoints_two_color(self):
        ramp = gradient((0, 0, 0), None, (100, 100, 100))
        self.assertEqual(len(ramp), 101)
        self.assertEqual(ramp[0], (0, 0, 0))
        self.assertEqual(ramp[100], (100, 100, 100))
        # Midpoint ~ half way.
        self.assertEqual(ramp[50], (50, 50, 50))

    def test_mid_color_three_pass(self):
        ramp = gradient((0, 0, 0), (100, 0, 0), (100, 100, 0))
        self.assertEqual(len(ramp), 101)
        self.assertEqual(ramp[0], (0, 0, 0))
        self.assertEqual(ramp[50], (100, 0, 0))
        self.assertEqual(ramp[100], (100, 100, 0))
        # Second pass interpolates from mid towards end.
        self.assertEqual(ramp[75], (100, 50, 0))

    def test_no_end_collapses_to_start(self):
        ramp = gradient((10, 20, 30), None, None)
        self.assertEqual(len(ramp), 101)
        self.assertTrue(all(c == (10, 20, 30) for c in ramp))

    def test_custom_steps(self):
        ramp = gradient((0, 0, 0), None, (200, 200, 200), steps=11)
        self.assertEqual(len(ramp), 11)
        self.assertEqual(ramp[-1], (200, 200, 200))
        self.assertEqual(ramp[5], (100, 100, 100))


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.ramp = gradient((255, 0, 0), None, (0, 255, 0))

    def _styles_of(self, text: Text):
        return [style for _, style in text.spans] if text.spans else []

    def test_full_bar_all_cells_colored(self):
        t = meter(100, 10, self.ramp)
        self.assertEqual(_strip(t), "█" * 10)
        # Per-cell gradient: first and last cell colors differ.
        styles = [s.style for s in t.spans]
        self.assertEqual(len(styles), 10)
        self.assertNotEqual(styles[0], styles[-1])

    def test_empty_bar_dim_placeholder(self):
        t = meter(0, 8, self.ramp)
        self.assertEqual(_strip(t), "░" * 8)

    def test_clamp(self):
        over = _strip(meter(150, 6, self.ramp))
        exact = _strip(meter(100, 6, self.ramp))
        under = _strip(meter(-5, 6, self.ramp))
        zero = _strip(meter(0, 6, self.ramp))
        self.assertEqual(over, exact)
        self.assertEqual(under, zero)

    def test_partial_fill_counts(self):
        t = meter(50, 10, self.ramp)
        filled = _strip(t).count("█")
        empty = _strip(t).count("░")
        self.assertEqual(filled + empty, 10)
        self.assertGreater(filled, 0)
        self.assertGreater(empty, 0)

    def test_invert_uses_opposite_gradient_end(self):
        normal = meter(100, 4, self.ramp)
        inv = meter(100, 4, self.ramp, invert=True)
        n_first = normal.spans[0].style
        i_first = inv.spans[0].style
        self.assertNotEqual(n_first, i_first)

    def test_cache_hit_returns_equal_output(self):
        a = meter(37, 12, self.ramp)
        b = meter(37, 12, self.ramp)
        self.assertEqual(_strip(a), _strip(b))
        self.assertEqual([s.style for s in a.spans], [s.style for s in b.spans])

    def test_zero_width(self):
        self.assertEqual(_strip(meter(50, 0, self.ramp)), "")


class BrailleGraphTests(unittest.TestCase):
    def test_table_size_and_indexing(self):
        self.assertEqual(len(BRAILLE_UP), 25)
        self.assertEqual(BRAILLE_UP[0], " ")
        self.assertEqual(BRAILLE_UP[24], "⣿")

    def test_all_zeros_is_spaces(self):
        t = braille_graph([0] * 10, width=5)
        self.assertEqual(_strip(t), " " * 5)

    def test_all_max_is_full_blocks(self):
        t = braille_graph([9] * 10, width=5)
        self.assertEqual(_strip(t), "⣿" * 5)

    def test_auto_scale_low_high(self):
        vals = [0, 4] * 6  # min -> level 0, max -> level 4
        t = braille_graph(vals, width=6)
        # Pair (0, 4): result0=0, result1=4 -> index 4 == "⢸".
        self.assertEqual(_strip(t), "⢸" * 6)

    def test_explicit_range_rescaled(self):
        # Values 0..40 mapped onto explicit low/high 0..10 -> both clamp to max.
        t = braille_graph([10, 10], width=1, low=0, high=10)
        self.assertEqual(_strip(t), "⣿")
        t2 = braille_graph([0, 0], width=1, low=-10, high=10)
        # 0 sits at the middle of [-10, 10] -> level 2 pair -> index 12.
        self.assertEqual(_strip(t2), BRAILLE_UP[12])

    def test_down_flips_vertical_axis(self):
        up = braille_graph([0, 4], width=1)
        down = braille_graph([0, 4], width=1, down=True)
        # Flipped levels: r0=4, r1=0 -> index 20.
        self.assertEqual(_strip(down), BRAILLE_UP[20])
        self.assertEqual(_strip(up), "⢸")

    def test_width_trims_to_last_points(self):
        vals = list(range(21))  # more than width*2 points
        t = braille_graph(vals, width=3)
        self.assertEqual(len(_strip(t)), 3)

    def test_short_series_padded(self):
        t = braille_graph([5], width=4)
        self.assertEqual(len(_strip(t)), 4)


class FooterBarTests(unittest.TestCase):
    BINDINGS = [("↑↓", "chọn"), ("s", "sync"), ("q", "thoát")]

    def test_basic_render_markup(self):
        out = footer_bar(self.BINDINGS, width=120)
        self.assertIn("[hi_fg]q[/] thoát", out)
        self.assertIn("[dim] · [/]", out)
        self.assertIn("[hi_fg]s[/] sync", out)

    def test_visible_length_strips_markup(self):
        out = footer_bar(self.BINDINGS, width=120)
        import re
        visible = len(re.sub(r"\[[^\]]*\]", "", out))
        self.assertLessEqual(visible, 120)

    def test_trim_keeps_quit_when_narrow(self):
        out = footer_bar(self.BINDINGS, width=10)
        self.assertIn("thoát", out)
        self.assertNotIn("sync", out)

    def test_empty_bindings(self):
        self.assertEqual(footer_bar([], 40), "")


class ShortcutTitleTests(unittest.TestCase):
    def test_highlight_matching_letter(self):
        out = shortcut_title("Menu", "m")
        self.assertEqual(out, "┌┐[hi_fg]M[/]enu┌┐")

    def test_case_insensitive_match(self):
        out = shortcut_title("Sync", "s")
        self.assertEqual(out, "┌┐[hi_fg]S[/]ync┌┐")

    def test_missing_key_falls_back_to_first_char(self):
        out = shortcut_title("abc", "z")
        self.assertEqual(out, "┌┐[hi_fg]a[/]bc┌┐")


class VisualSamples(unittest.TestCase):
    """Manual eyeball output only -- no assertions."""

    def test_print_samples(self):
        ramp = gradient((64, 128, 255), (255, 255, 64), (255, 64, 64))
        for v in (0, 25, 50, 75, 100):
            print(f"meter {v:>3}: ", end="")
            from rich.console import Console
            Console().print(meter(v, 40, ramp))
        print("graph : ", end="")
        Console().print(braille_graph([1, 2, 0, 5, 3, 8, 2, 7, 4, 9, 1, 3], width=6))
        print("footer: ", end="")
        Console().print(footer_bar([("↑↓", "chọn"), ("s", "sync"), ("d", "download"), ("q", "thoát")], 60))
        print(f"title : {shortcut_title('Menu', 'm')}")


if __name__ == "__main__":
    unittest.main()
