"""Unit tests for SolverSessionStore."""
from pathlib import Path
import tempfile

from ctf_downloader.storage.session_store import SolverSessionStore


def test_session_store_lifecycle():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        store = SolverSessionStore(workspace)

        assert store.get_session("1", "gpt") is None

        # Save session
        store.save_session("1", "gpt", "sess_abc123", metadata={"name": "crypto-test", "category": "Crypto"})
        assert store.get_session("1", "gpt") == "sess_abc123"

        # Increment continuation
        count = store.increment_continuation("1", "gpt")
        assert count == 1
        count = store.increment_continuation("1", "gpt")
        assert count == 2

        # Check multi-engine isolation
        assert store.get_session("1", "agy") is None
        store.save_session("1", "agy", "conv_xyz789")
        assert store.get_session("1", "agy") == "conv_xyz789"
        assert store.get_session("1", "gpt") == "sess_abc123"

        # Archive session on filter
        archived = store.archive_session("1", "gpt", reason="filtered")
        assert archived == "sess_abc123"
        assert store.get_session("1", "gpt") is None

        # Verify history is preserved
        all_sessions = store.list_sessions("gpt")
        assert "1" in all_sessions
        assert len(all_sessions["1"]["session_history"]) == 1
        hist_entry = all_sessions["1"]["session_history"][0]
        assert hist_entry["session_id"] == "sess_abc123"
        assert hist_entry["reason"] == "filtered"
        assert hist_entry["continuation_count"] == 2

        # Start new session after filter
        store.save_session("1", "gpt", "sess_clean456")
        assert store.get_session("1", "gpt") == "sess_clean456"
