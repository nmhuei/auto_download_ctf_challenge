#!/usr/bin/env python3
"""Platform Scaffold Generator for CTF Toolkit.

Quickly scaffolds a new platform definition (declarative JSON or Python adapter)
and corresponding deterministic unit test in one command.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "ctf_downloader" / "platforms" / "templates"
DEFINITIONS_DIR = ROOT / "ctf_downloader" / "platforms" / "definitions"
PLATFORMS_DIR = ROOT / "ctf_downloader" / "platforms"
TESTS_DIR = ROOT / "tests"


def _sanitize_key(key: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_")
    if not cleaned:
        raise ValueError("Invalid platform key. Use letters, numbers, and underscores.")
    return cleaned


def scaffold_json(key: str, label: str, scope: str = "builtin", workspace: str | None = None) -> tuple[Path, Path]:
    tpl_file = TEMPLATES_DIR / "declarative_platform.template.json"
    content = tpl_file.read_text(encoding="utf-8")
    content = content.replace("example_ctf", key).replace("Example CTF", label)

    if scope == "builtin":
        target_dir = DEFINITIONS_DIR
    elif scope == "workspace":
        ws = Path(workspace or Path.cwd())
        target_dir = ws / ".ctf" / "platforms"
    else:  # global
        target_dir = Path.home() / ".config" / "ctf_toolkit" / "platforms"

    target_dir.mkdir(parents=True, exist_ok=True)
    out_json = target_dir / f"{key}.json"
    if out_json.exists():
        print(f"Error: Target file {out_json} already exists. Aborting.", file=sys.stderr)
        sys.exit(1)

    out_json.write_text(content, encoding="utf-8")
    print(f"Created platform schema: {out_json}")

    # Generate unit test
    test_file = TESTS_DIR / f"test_platform_{key}.py"
    test_code = f'''"""Unit test for {label} declarative schema."""
from ctf_downloader.platforms.registry import get_spec


def test_{key}_schema_loads():
    spec = get_spec("{key}")
    assert spec.key == "{key}"
    assert spec.label == "{label}"
'''
    if not test_file.exists():
        test_file.write_text(test_code, encoding="utf-8")
        print(f"Created unit test: {test_file}")

    return out_json, test_file


def scaffold_python(key: str, label: str) -> tuple[Path, Path]:
    class_name = "".join(part.capitalize() for part in key.split("_")) + "Adapter"

    tpl_py = TEMPLATES_DIR / "python_adapter.template.py"
    content = tpl_py.read_text(encoding="utf-8")
    content = (
        content.replace("example_ctf", key)
        .replace("Example CTF", label)
        .replace("ExampleCTFAdapter", class_name)
    )

    out_py = PLATFORMS_DIR / f"{key}.py"
    if out_py.exists():
        print(f"Error: Target file {out_py} already exists. Aborting.", file=sys.stderr)
        sys.exit(1)

    out_py.write_text(content, encoding="utf-8")
    print(f"Created adapter module: {out_py}")

    # Register in registry.py if not present
    registry_file = PLATFORMS_DIR / "registry.py"
    reg_text = registry_file.read_text(encoding="utf-8")
    import_match = re.search(r"from \. import (.*?)(\n|$)", reg_text)
    if import_match and key not in import_match.group(1):
        old_import = import_match.group(0)
        items = [x.strip() for x in import_match.group(1).replace("# noqa: E402,F401", "").split(",") if x.strip()]
        items.append(key)
        items = sorted(set(items))
        new_import = f"from . import {', '.join(items)}  # noqa: E402,F401\n"
        reg_text = reg_text.replace(old_import, new_import)
        registry_file.write_text(reg_text, encoding="utf-8")
        print(f"Registered '{key}' in {registry_file}")

    # Generate unit test
    tpl_test = TEMPLATES_DIR / "platform_test.template.py"
    test_content = (
        tpl_test.read_text(encoding="utf-8")
        .replace("example_ctf", key)
        .replace("Example CTF", label)
    )
    test_file = TESTS_DIR / f"test_platform_{key}.py"
    if not test_file.exists():
        test_file.write_text(test_content, encoding="utf-8")
        print(f"Created unit test: {test_file}")

    return out_py, test_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold a new CTF platform definition or adapter")
    parser.add_argument("-k", "--key", required=True, help="Platform key (e.g. metactf, myplatform)")
    parser.add_argument("-l", "--label", required=True, help="Platform display label (e.g. 'MetaCTF', 'My CTF')")
    parser.add_argument(
        "-t", "--type", choices=["json", "python"], default="json",
        help="Scaffold type: 'json' (Declarative Zero-Code, recommended) or 'python' (custom adapter)"
    )
    parser.add_argument(
        "-s", "--scope", choices=["builtin", "workspace", "global"], default="builtin",
        help="Storage scope for JSON schemas (default: builtin)"
    )
    parser.add_argument("-w", "--workspace", help="Workspace path if scope=workspace")

    args = parser.parse_args()
    key = _sanitize_key(args.key)
    label = args.label.strip()

    if args.type == "json":
        scaffold_json(key, label, scope=args.scope, workspace=args.workspace)
    else:
        scaffold_python(key, label)

    print("\n✅ Scaffold complete! Run verification:")
    print(f"pytest tests/test_platform_{key}.py")


if __name__ == "__main__":
    main()
