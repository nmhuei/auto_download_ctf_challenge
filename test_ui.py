"""Tests for ctf_downloader.ui — output-discipline layer (uv patterns)."""

from __future__ import annotations

import io
import re
import sys

import pytest
from rich.console import Console
from rich.text import Text

from ctf_downloader.ui import (
    BRANCH,
    DOT,
    Diagnostic,
    OK,
    Printer,
    SPINNER,
    Verbosity,
    build_lines,
    error,
    load_theme,
    ok_summary,
    render,
    warning,
)
from ctf_downloader.ui.console import err_console, out_console
from ctf_downloader.ui.diagnostics import (
    LEAF_DOT,
    TREE_ELL,
    TREE_TEE,
)
from ctf_downloader.ui.theme import ACCENT, ACCENT_DEEP, FG_BASE, FG_MUTED


def ansi_console(width: int = 100) -> Console:
    """Console into a StringIO, ANSI forced, fixed width."""
    return Console(
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=width,
    )


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- console.py


def test_err_console_is_stderr():
    assert err_console.file is sys.stderr


def test_out_console_is_stdout():
    assert out_console.file is sys.stdout


# ---------------------------------------------------------------- printers.py


def test_verbosity_ordering():
    assert Verbosity.QUIET2 < Verbosity.QUIET1 < Verbosity.NORMAL < Verbosity.VERBOSE
    assert Verbosity.QUIET2 == 0 and Verbosity.NORMAL == 2


@pytest.mark.parametrize("verbosity", [Verbosity.QUIET1, Verbosity.QUIET2])
def test_summary_suppressed_when_quiet(verbosity):
    p = Printer(verbosity=verbosity)
    with err_console.capture() as cap:
        p.summary("hello-summary")
    assert "hello-summary" not in cap.get()


def test_summary_printed_at_normal_and_verbose():
    for verbosity in (Verbosity.NORMAL, Verbosity.VERBOSE):
        p = Printer(verbosity=verbosity)
        with err_console.capture() as cap:
            p.summary("hello-summary")
        assert "hello-summary" in cap.get()


def test_debug_only_at_verbose():
    normal = Printer(verbosity=Verbosity.NORMAL)
    with err_console.capture() as cap:
        normal.debug("debug-detail")
    assert "debug-detail" not in cap.get()

    verbose = Printer(verbosity=Verbosity.VERBOSE)
    with err_console.capture() as cap:
        verbose.debug("debug-detail")
    assert "debug-detail" in cap.get()


def test_progress_disabled_when_quiet_or_verbose_or_flag():
    assert Printer(verbosity=Verbosity.NORMAL).progress_enabled is True
    assert Printer(verbosity=Verbosity.QUIET1).progress_enabled is False
    assert Printer(verbosity=Verbosity.QUIET2).progress_enabled is False
    # uv rule: --verbose disables the progress bar as well.
    assert Printer(verbosity=Verbosity.VERBOSE).progress_enabled is False
    assert Printer(verbosity=Verbosity.NORMAL, no_progress=True).progress_enabled is False


def test_out_data_is_undecorated_on_stdout():
    with out_console.capture() as cap:
        Printer(verbosity=Verbosity.NORMAL).out_data("/tmp/[weird] path.txt")
    # markup/highlight disabled -> literal brackets survive untouched
    assert "/tmp/[weird] path.txt" in cap.get()


# ---------------------------------------------------------------- diagnostics.py


def test_diagnostic_defaults_and_shorthands():
    d = error("x")
    assert isinstance(d, Diagnostic)
    assert d.severity == "error"
    assert d.exit_code == 1
    assert d.cause is None and d.hints == ()

    w = warning("y", hints=["a", "b"])
    assert w.severity == "warning" and w.hints == ("a", "b")


def test_render_error_labels_with_ansi_colors():
    con = ansi_console()
    with con.capture() as cap:
        render(error("download failed", hints=["check the URL"]), console=con)
    out = cap.get()
    assert "\x1b[" in out
    assert "error:" in out
    assert "ACTION REQUIRED" in out
    # red label (31/91/truecolor-red) and muted hint leaf (#99917E)
    assert re.search(r"\x1b\[[0-9;]*(?:31|91)m", out), out
    # rich render hex fg.muted thành SGR 38;2;r;g;b — không bao giờ literal hex
    r, g, b = (int(FG_MUTED[i : i + 2], 16) for i in (1, 3, 5))
    assert f"\x1b[38;2;{r};{g};{b}m" in out, out
    # cyan hint label đã bỏ theo SPEC E1 — hint giờ là leaf fg.muted
    assert not re.search(r"\x1b\[[0-9;]*(?:36|96)m", out), out


def test_render_warning_label_yellow():
    con = ansi_console()
    with con.capture() as cap:
        render(warning("stale cache"), console=con)
    out = cap.get()
    assert "warning:" in out
    assert re.search(r"\x1b\[[0-9;]*(?:33|93)m", out), out


def test_build_lines_cause_branch_connector_and_indent():
    diag = error("top level", cause="root cause here", hints=["fix it"])
    lines = build_lines(diag, width=80)
    cause_line = next(ln for ln in lines if TREE_TEE in ln.plain)
    # branch connector starts indented under the label column
    assert cause_line.plain.index(TREE_TEE) == 2
    # cause text present after the connector
    assert "root cause here" in cause_line.plain


def test_e1_error_tree_full_layout_cause_then_action_required():
    """SPEC E1: headline glyph ✗ → ├─ cause → └─ ACTION REQUIRED → · hints."""
    diag = error(
        "failed to download set",
        cause="network unreachable after 3 retries",
        hints=["check your VPN connection", "pass --retry 5 to increase"],
    )
    lines = build_lines(diag, width=80)
    plain = [ln.plain for ln in lines]

    assert plain[0].startswith("✗ error: failed to download set")

    tee_idx = next(i for i, p in enumerate(plain) if "network unreachable" in p)
    assert plain[tee_idx].startswith("  ├─ ")  # "  ├─ "

    ell_lines = [i for i, p in enumerate(plain) if "ACTION REQUIRED" in p]
    assert len(ell_lines) == 1  # nhãn Action Required xuất hiện đúng 1 lần
    ell_idx = ell_lines[0]
    assert plain[ell_idx].startswith("  └─ ACTION REQUIRED")  # "  └─ "
    assert tee_idx < ell_idx  # cause nhánh ├─ đứng trước node kết └─

    assert plain[ell_idx + 1] == f"     {LEAF_DOT}check your VPN connection"
    assert plain[ell_idx + 2] == f"     {LEAF_DOT}pass --retry 5 to increase"


def test_e1_tree_node_styles_theme_tokens():
    diag = error(
        "m", cause="why",
        hints=["fix a"],
    )
    lines = build_lines(diag, width=80)

    def span_style(line: Text, needle: str) -> str:
        for s in line.spans:
            if needle in line.plain[s.start : s.end]:
                return s.style or ""
        return ""

    tee_line = next(ln for ln in lines if TREE_TEE in ln.plain)
    assert ACCENT_DEEP in span_style(tee_line, "├─")      # div_line connector
    assert FG_BASE in span_style(tee_line, "why")          # cause fg.base

    ell_line = next(ln for ln in lines if "ACTION REQUIRED" in ln.plain)
    act_style = span_style(ell_line, "ACTION REQUIRED")
    assert ACCENT in act_style and "bold" in act_style

    leaf_line = next(ln for ln in lines if LEAF_DOT in ln.plain)
    assert FG_MUTED in span_style(leaf_line, "·")          # leaf dot muted
    assert FG_MUTED in span_style(leaf_line, "fix a")      # leaf text muted


def test_e1_wrapped_cause_continuation_uses_vertical_connector():
    cause = "network unreachable after " + "many ".rstrip() + "retries " * 12
    diag = error("failed", cause=cause, hints=["do something"])
    lines = build_lines(diag, width=60)
    conts = [ln for ln in lines if ln.plain.startswith("  │  ")]  # "  │  "
    assert conts, "cause wrap phải nối tiếp bằng '│  '"


def test_e1_no_hints_skips_action_required_node():
    diag = error("boom")
    lines = build_lines(diag, width=80)
    assert len(lines) == 1  # không in cây rỗng khi trống cause+hints

    diag2 = error("boom", cause="root cause")
    plain = [ln.plain for ln in build_lines(diag2, width=80)]
    # cause là node cuối (không hints) → connector kết cây └─, không còn ├─
    assert any(TREE_ELL in p for p in plain)
    assert not any(TREE_TEE in p for p in plain)
    assert not any("ACTION REQUIRED" in p for p in plain)


def test_build_lines_empty_message_still_renders_headline():
    """message="" không được làm nổ build_lines — headline vẫn hiện."""
    lines = build_lines(error(""), width=80)
    assert len(lines) == 1
    assert lines[0].plain.startswith("✗ error:")


def test_empty_hints_are_skipped():
    """Hint rỗng bị bỏ qua: không leaf dòng trắng, các hint thật vẫn hiện."""
    diag = error("m", hints=["", "real hint"])
    plain = [ln.plain for ln in build_lines(diag, width=80)]
    assert any("ACTION REQUIRED" in p for p in plain)
    leaves = [p for p in plain if p.lstrip().startswith(LEAF_DOT.strip())]
    assert leaves == [f"     {LEAF_DOT}real hint"]


def test_all_empty_hints_skip_action_required_node():
    """Toàn bộ hint rỗng → coi như không có hints, không treo node kết."""
    plain = [ln.plain for ln in build_lines(error("m", hints=["", ""]), width=80)]
    assert len(plain) == 1
    assert not any("ACTION REQUIRED" in p for p in plain)


def test_terminal_cause_uses_ell_connector():
    """Có cause NHƯNG không hints: cause là node cuối → └─ thay vì ├─."""
    plain = [ln.plain for ln in build_lines(error("m", cause="why"), width=80)]
    assert any(p.startswith("  └─ why") for p in plain)
    assert not any("├" in p for p in plain)


def test_terminal_wrapped_cause_continuation_has_no_vertical_bar():
    """Cause node cuối khi wrap: continuation thụt dòng, không treo '│'."""
    diag = error("m", cause="word " * 20)
    plain = [ln.plain for ln in build_lines(diag, width=40)]
    assert len(plain) > 2  # cause đã wrap nhiều dòng
    assert not any(p.startswith("  │") for p in plain), plain


def test_e1_warning_uses_bang_glyph_and_same_tree():
    diag = warning("auth failed", cause="cookie expired", hints=["ctf doctor -u URL"])
    lines = build_lines(diag, width=80)
    plain = [ln.plain for ln in lines]
    assert plain[0].startswith("! warning: auth failed")
    assert any(p.startswith("  ├─ ") for p in plain)
    assert any(p.startswith("  └─ ACTION REQUIRED") for p in plain)


def test_build_lines_wrap_multiline_aligns_continuation_after_label():
    long_msg = "word " * 60  # forces wrapping at width 80
    lines = build_lines(error(long_msg), width=80)
    plain = [ln.plain for ln in lines]
    assert len(lines) > 1
    col = len("✗ ") + len("error:") + 1  # sau glyph ✗ + nhãn + space
    for cont in plain[1:]:
        if "ACTION" in cont or "├" in cont or "└" in cont:
            continue
        assert cont.startswith(" " * col)


def test_hints_one_per_line():
    diag = error("boom", hints=["first hint", "second hint"])
    lines = build_lines(diag, width=80)
    hint_lines = [ln for ln in lines if LEAF_DOT.strip() in ln.plain and "ACTION" not in ln.plain]
    assert len(hint_lines) == 2
    assert "first hint" in hint_lines[0].plain
    assert "second hint" in hint_lines[1].plain


def test_body_bold_but_uncolored():
    diag = error("plain body")
    line = build_lines(diag, width=80)[0]
    plain = line.plain
    spans = list(line.spans)
    # Span.style is the raw style string passed to Text.append.
    body = next(s for s in spans if plain[s.start : s.end] == "plain body")
    assert body.style == "bold"  # bold, never colored
    # label carries the color instead
    label_style = next(
        s.style for s in spans if plain[s.start : s.end] == "error:"
    )
    assert "red" in label_style and "bold" in label_style


def test_label_styles_by_severity():
    err_line = build_lines(error("m"), width=80)[0]
    warn_line = build_lines(warning("m"), width=80)[0]

    def label_style(line: Text) -> str:
        for s in line.spans:
            if line.plain[s.start : s.end].rstrip(":") in ("error", "warning"):
                return s.style
        return ""

    assert "red" in label_style(err_line)
    assert "yellow" in label_style(warn_line)


# ---------------------------------------------------------------- style.py


def test_ok_summary_singular_plural():
    singular = ok_summary("tải", 1, "challenge", 1.0)
    assert "[bold]1[/] challenge" in singular
    assert "challenges" not in singular

    plural = ok_summary("tải", 12, "challenge", 4.2118)
    assert plural.startswith("Đã tải ")
    assert "[bold]12[/] challenges" in plural
    assert "[dim]trong 4.21s[/]" in plural


def test_symbol_constants_exist():
    assert OK == "✔"
    assert BRANCH == "╰─▶"
    assert DOT == "·"
    assert SPINNER and all(len(f) == 1 for f in SPINNER)


# ---------------------------------------------------------------- theme.py


def test_theme_defaults_have_ctf_semantic_keys():
    from rich.style import Style

    theme = load_theme(None)
    for key in ("solved", "unsolved", "firstblood", "div_line", "hi_fg", "title"):
        assert key in theme.styles
    # PHOSPHOR FIELD KIT (spec §3): token mới + legacy alias trỏ vào hex spec.
    for key in ("fg.base", "fg.muted", "fg.faint",
                "accent", "accent.hi", "accent.deep",
                "info", "solved", "firstblood", "error", "warn"):
        assert key in theme.styles
    # PHOSPHOR chuẩn hoá codex-r3 #1: success = solved-green semantic,
    # div_line = accent.deep trùng mốc đầu meter (#6B4300).
    assert theme.styles["success"] == Style.parse("#62C97E")   # = solved
    assert theme.styles["div_line"] == Style.parse("#6B4300")  # = accent.deep


def test_theme_toml_override(tmp_path):
    from rich.style import Style

    toml_file = tmp_path / "theme.toml"
    toml_file.write_text(
        '[styles]\nsolved = "bold blue"\nfirstblood = "#ff004f"\n',
        encoding="utf-8",
    )
    theme = load_theme(toml_file)
    assert theme.styles["solved"] == Style.parse("bold blue")
    assert theme.styles["firstblood"] == Style.parse("#ff004f")
    # untouched defaults remain (info/path/literal → neutral fg.base,
    # codex-r3 #1: đã bỏ cyan #62C8CE)
    assert theme.styles["hint"] == Style.parse("#E6E1D3")


def test_loaded_theme_applies_to_console(tmp_path):
    toml_file = tmp_path / "theme.toml"
    toml_file.write_text('[styles]\nsuccess = "bold red"\n', encoding="utf-8")
    con = ansi_console(width=80)
    con.push_theme(load_theme(toml_file))
    try:
        with con.capture() as cap:
            con.print("done", style="success")
    finally:
        con.pop_theme()
    out = cap.get()
    assert "done" in out
    # bold red in some encoding form
    assert re.search(r"\x1b\[1;31m|\x1b\[31;1m|\x1b\[91;1m", out), out
