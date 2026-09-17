import tempfile
from pathlib import Path

from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.models import Challenge


def test_pull_refresh_preserves_note_rules_and_user_notes():
    with tempfile.TemporaryDirectory() as temp:
        challenge = Challenge(id=7, name="portal", category="Web")
        root = Path(WorkspaceBuilder.create_challenge_workspace(temp, challenge, [], [], []))
        note = root / "challenge" / "NOTE.md"
        note.write_text(note.read_text(encoding="utf-8") + "\n## User note\nkeep this\n", encoding="utf-8")

        WorkspaceBuilder.create_challenge_workspace(temp, challenge, [], [], [])

        text = note.read_text(encoding="utf-8")
        assert "keep this" in text
        assert "Read `metadata.json` first" in text
        assert "script/" in text
        assert "Solve and verify locally first" in text
        assert "worker-report.json" in text


def test_generated_solver_template_exposes_optional_remote_adapters():
    template = WorkspaceBuilder._generate_solve_template(
        Challenge(id=8, name="math", category="Crypto"), []
    )

    assert "--url" in template
    assert "--remote" in template
