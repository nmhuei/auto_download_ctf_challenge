#!/usr/bin/env python3
"""Generate the canonical argparse option index embedded in README/man."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctf_downloader.cli import build_unified_parser  # noqa: E402

README = ROOT / "README.md"
MAN = ROOT / "docs" / "ctf.1"

README_BEGIN = "<!-- BEGIN GENERATED CLI OPTIONS -->"
README_END = "<!-- END GENERATED CLI OPTIONS -->"
MAN_BEGIN = '.\\" BEGIN GENERATED CLI OPTIONS'
MAN_END = '.\\" END GENERATED CLI OPTIONS'


def _subparsers(parser: argparse.ArgumentParser):
    return next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )


def iter_command_parsers(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    subs = _subparsers(parser)
    if subs is None:
        return
    seen: set[int] = set()
    for name, child in subs.choices.items():
        if id(child) in seen:
            continue
        seen.add(id(child))
        path = (*prefix, name)
        yield path, child
        yield from iter_command_parsers(child, path)


def command_options(parser: argparse.ArgumentParser) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for path, child in iter_command_parsers(parser):
        opts = sorted({
            opt
            for action in child._actions
            for opt in action.option_strings
            if opt.startswith("--") and opt != "--help"
        })
        if opts or _subparsers(child) is None:
            rows.append((" ".join(path), opts))
    return rows


def render_readme(rows: list[tuple[str, list[str]]]) -> str:
    lines = [
        README_BEGIN,
        "### Chỉ mục tuỳ chọn CLI (tự sinh)",
        "",
        "> Nguồn chân lý: ctf_downloader.cli.build_unified_parser(). "
        "Chạy python3 scripts/generate_cli_option_index.py sau khi đổi parser.",
        "",
        "| Lệnh | Long options |",
        "| --- | --- |",
    ]
    bt = chr(96)
    for command, opts in rows:
        rendered = " · ".join(bt + o + bt for o in opts) if opts else "—"
        lines.append("| " + bt + "ctf " + command + bt + " | " + rendered + " |")
    lines.append(README_END)
    return "\n".join(lines)


def _roff_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("-", "\\-")


def render_man(rows: list[tuple[str, list[str]]]) -> str:
    lines = [
        MAN_BEGIN,
        ".SH CLI OPTION INDEX (GENERATED)",
        "Chỉ mục này được sinh tự động từ argparse; không sửa tay.",
    ]
    for command, opts in rows:
        lines.extend([
            ".TP",
            ".B ctf " + _roff_escape(command),
            _roff_escape(" ".join(opts)) if opts else "(không có long option riêng)",
        ])
    lines.append(MAN_END)
    return "\n".join(lines)


def replace_block(text: str, begin: str, end: str, block: str, before: str) -> str:
    if begin in text and end in text:
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        return text[:start] + block + text[finish:]
    idx = text.find(before)
    if idx < 0:
        return text.rstrip() + "\n\n" + block + "\n"
    return text[:idx] + block + "\n\n" + text[idx:]


def desired_contents() -> tuple[str, str]:
    rows = command_options(build_unified_parser())
    readme = replace_block(
        README.read_text(encoding="utf-8"),
        README_BEGIN,
        README_END,
        render_readme(rows),
        "## 4. Cây workspace output",
    )
    man = replace_block(
        MAN.read_text(encoding="utf-8"),
        MAN_BEGIN,
        MAN_END,
        render_man(rows),
        ".SH EXAMPLES",
    )
    return readme, man


def main(argv: list[str] | None = None) -> int:
    args = set(argv if argv is not None else sys.argv[1:])
    check = "--check" in args
    desired_readme, desired_man = desired_contents()
    stale = []
    if README.read_text(encoding="utf-8") != desired_readme:
        stale.append(str(README.relative_to(ROOT)))
    if MAN.read_text(encoding="utf-8") != desired_man:
        stale.append(str(MAN.relative_to(ROOT)))
    if check:
        if stale:
            print("CLI option index stale: " + ", ".join(stale), file=sys.stderr)
            return 1
        return 0
    README.write_text(desired_readme, encoding="utf-8")
    MAN.write_text(desired_man, encoding="utf-8")
    print("updated generated CLI option index: README.md, docs/ctf.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
