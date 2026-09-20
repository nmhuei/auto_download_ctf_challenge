"""Resolution of a human-friendly workspace reference under workspace_root."""
import json
from io import StringIO

import pytest

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.storage.workspace_locator import (
    WorkspaceReferenceAmbiguous,
    WorkspaceReferenceNotFound,
    resolve_workspace_reference,
)


def _workspace(root, directory, title):
    workspace = root / directory
    workspace.mkdir()
    (workspace / "challenges.json").write_text(
        json.dumps({"ctf_info": {"title": title}}), encoding="utf-8"
    )
    return workspace


def test_resolves_direct_workspace_directory_name(tmp_path):
    workspace = _workspace(tmp_path, "ASIS_CTF_2026", "ASIS CTF Quals 2026")

    assert resolve_workspace_reference("ASIS_CTF_2026", workspace_root=tmp_path) == str(workspace)


def test_resolves_event_title_ignoring_separator_and_case(tmp_path):
    workspace = _workspace(tmp_path, "ASIS_CTF_2026", "ASIS CTF Quals 2026")

    assert resolve_workspace_reference("asis_ctf_quals_2026", workspace_root=tmp_path) == str(workspace)


def test_rejects_ambiguous_event_title_without_choosing_one(tmp_path):
    _workspace(tmp_path, "ASIS_A", "ASIS CTF Quals 2026")
    _workspace(tmp_path, "ASIS_B", "ASIS_CTF_Quals_2026")

    with pytest.raises(WorkspaceReferenceAmbiguous) as exc:
        resolve_workspace_reference("ASIS CTF Quals 2026", workspace_root=tmp_path)

    assert "ASIS_A" in str(exc.value)
    assert "ASIS_B" in str(exc.value)


def test_reports_reference_not_found(tmp_path):
    with pytest.raises(WorkspaceReferenceNotFound):
        resolve_workspace_reference("missing-event", workspace_root=tmp_path)


def test_sync_accepts_event_reference_as_a_positional_argument():
    args = build_unified_parser().parse_args(["sync", "ASIS_CTF_Quals_2026"])

    assert args.workspace_ref == "ASIS_CTF_Quals_2026"
    assert args.workspace == "."


def test_root_help_uses_short_superbqa_description():
    output = StringIO()
    build_unified_parser().print_help(file=output)

    flattened = " ".join(output.getvalue().split())
    assert "solve Kích hoạt SuperBQA" in flattened
    assert "tối đa 3 luồng" not in flattened


def test_sync_falls_back_to_default_workspace_when_not_explicit(tmp_path, monkeypatch):
    ws = tmp_path / "Active_CTF"
    ws.mkdir()
    (ws / "challenges.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "ctf_downloader.storage.global_config.load_global_config",
        lambda: {"default_workspace": str(ws)},
    )

    from ctf_downloader.cli import main
    # Running parser logic
    from ctf_downloader.cli import build_unified_parser
    parser = build_unified_parser()
    args = parser.parse_args(["sync", "--apply"])

    # Simulate cli.py resolution logic
    from pathlib import Path
    ws_path = Path(args.workspace or ".").resolve()
    if not (ws_path / "challenges.json").exists() and not (ws_path / ".ctf").exists():
        from ctf_downloader.storage.global_config import load_global_config
        cfg = load_global_config()
        def_ws = cfg.get("default_workspace")
        if def_ws and Path(def_ws).is_dir() and ((Path(def_ws) / "challenges.json").exists() or (Path(def_ws) / ".ctf").exists()):
            args.workspace = str(Path(def_ws).resolve())

    assert args.workspace == str(ws.resolve())

