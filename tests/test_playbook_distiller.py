"""Unit tests for Playbook Distiller and Session Forker."""
import json
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ctf_downloader.services.playbook_distiller import PlaybookDistiller
from ctf_downloader.services.session_forker import fork_agy_session
from ctf_downloader.services.solver_service import SolverJob, SolverService


def test_session_forker_mock_env(tmp_path: Path):
    cli_dir = tmp_path / "gemini_cli"
    brain_dir = cli_dir / "brain"
    conv_dir = cli_dir / "conversations"
    brain_dir.mkdir(parents=True)
    conv_dir.mkdir(parents=True)

    source_id = str(uuid.uuid4())
    source_brain = brain_dir / source_id
    source_brain.mkdir()
    (source_brain / "notes.txt").write_text("prior knowledge", encoding="utf-8")

    source_db = conv_dir / f"{source_id}.db"
    with sqlite3.connect(str(source_db)) as conn:
        conn.execute("CREATE TABLE trajectory_meta (trajectory_id text, cascade_id text, trajectory_type integer, source integer)")
        conn.execute("INSERT INTO trajectory_meta VALUES ('traj-1', ?, 1, 1)", (source_id,))

    summaries_db = cli_dir / "conversation_summaries.db"
    with sqlite3.connect(str(summaries_db)) as conn:
        conn.execute("CREATE TABLE conversation_summaries (conversation_id text, title text, parent_conversation_id text, last_modified_time text)")
        conn.execute("INSERT INTO conversation_summaries VALUES (?, 'Crypto Master', '', '2026-01-01') ", (source_id,))

    target_id = fork_agy_session(source_id, cli_dir=cli_dir, title="Crypto · Challenge 1")
    assert target_id is not None
    assert target_id != source_id

    # Verify cloned brain
    assert (brain_dir / target_id / "notes.txt").read_text(encoding="utf-8") == "prior knowledge"

    # Verify cloned db has updated cascade_id
    with sqlite3.connect(str(conv_dir / f"{target_id}.db")) as conn:
        row = conn.execute("SELECT cascade_id, trajectory_id FROM trajectory_meta").fetchone()
        assert row[0] == target_id
        assert row[1] != "traj-1"

    # Verify summaries db
    with sqlite3.connect(str(summaries_db)) as conn:
        row = conn.execute("SELECT conversation_id, title, parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?", (target_id,)).fetchone()
        assert row is not None
        assert row[0] == target_id
        assert row[1] == "Crypto · Challenge 1"
        assert row[2] == source_id


def test_playbook_distiller_flow(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    chall_dir = ws / "Crypto" / "RSA1"
    script_dir = chall_dir / "script"
    script_dir.mkdir(parents=True)
    (script_dir / "worker-state.json").write_text(json.dumps({
        "state": "completed",
        "phase": "completed",
        "outcome": "solved_local",
    }), encoding="utf-8")
    (script_dir / "agy.log").write_text(
        '{"tool_name": "view_file"}\n{"tool_name": "run_command"}\n',
        encoding="utf-8",
    )
    (script_dir / "analysis.md").write_text("Factorized n via Wiener attack.", encoding="utf-8")

    job = SolverJob(
        display_id=1,
        challenge_id=101,
        category="Crypto",
        name="RSA1",
        path=chall_dir,
        has_source=True,
        has_instance=False,
    )

    distiller = PlaybookDistiller(ws)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="# Custom Distilled Playbook Content")
        res = distiller.distill("Crypto", [job])
        assert res["success"] is True
        assert res["category"] == "Crypto"
        assert Path(res["playbook_path"]).is_file()
        assert distiller.read_playbook("Crypto") == "# Custom Distilled Playbook Content"


def test_solver_service_distill_method(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    service = SolverService(ws)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        res = service.distill_playbook("Web")
        assert res["success"] is True
        assert Path(res["playbook_path"]).is_file()
        assert "Web Operational Playbook" in Path(res["playbook_path"]).read_text(encoding="utf-8")


def test_distiller_preserves_existing_playbook_on_failure(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    distiller = PlaybookDistiller(ws)
    distiller.save_playbook("Crypto", "# Existing User Custom Playbook")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        res = distiller.distill("Crypto", [])
        assert res["success"] is False
        assert "preserved existing playbook" in res["error"]
        assert distiller.read_playbook("Crypto") == "# Existing User Custom Playbook"


def test_session_forker_wal_consistency(tmp_path: Path):
    cli_dir = tmp_path / "gemini_cli"
    brain_dir = cli_dir / "brain"
    conv_dir = cli_dir / "conversations"
    brain_dir.mkdir(parents=True)
    conv_dir.mkdir(parents=True)

    source_id = str(uuid.uuid4())
    source_brain = brain_dir / source_id
    source_brain.mkdir()

    source_db = conv_dir / f"{source_id}.db"
    # Open connection in WAL mode and keep it open (uncheckpointed frames)
    conn = sqlite3.connect(str(source_db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE test_data (id integer, note text)")
    conn.execute("INSERT INTO test_data VALUES (1, 'in-wal-data')")
    conn.commit()

    target_id = fork_agy_session(source_id, cli_dir=cli_dir, title="WAL Fork")
    assert target_id is not None

    # Verify target db has the uncheckpointed WAL data
    with sqlite3.connect(str(conv_dir / f"{target_id}.db")) as dst_conn:
        row = dst_conn.execute("SELECT note FROM test_data WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] == "in-wal-data"

    conn.close()

