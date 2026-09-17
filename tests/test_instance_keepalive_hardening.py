import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ctf_downloader.services.instance_keepalive import (
    CRITICAL,
    DEAD,
    ERROR,
    InstanceKeepAlive,
    InstanceTracker,
    RESTART_BACKOFF_BASE,
)


def test_flag_status_exception_fails_closed_and_blocks_restart(tmp_path):
    # Setup mock repo that raises an error when reading status
    mock_repo = MagicMock()
    mock_repo.read_status.side_effect = OSError("Disk read error")

    meta_file = tmp_path / "web_chall" / ".challenge.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.touch()

    mock_platform = MagicMock()
    mock_svc = MagicMock()
    mock_svc.platform = mock_platform
    mock_svc.repo = mock_repo

    keepalive = InstanceKeepAlive(mock_svc, repo=mock_repo)
    tracker = InstanceTracker(
        challenge_id=1,
        name="web_chall",
        meta_path=meta_file,
        platform_kind="whale",
    )

    events = keepalive._maybe_restart(tracker, [])

    # Auto-restart must be BLOCKED
    assert tracker.blocked_flag_rotate is True
    assert tracker.state == DEAD
    assert any(level == CRITICAL for level, _ in events)
    mock_platform.stop_instance.assert_not_called()
    mock_platform.start_instance.assert_not_called()


def test_restart_backs_off_if_stop_instance_fails(tmp_path):
    mock_platform = MagicMock()
    # stop_instance returns False
    mock_platform.stop_instance.return_value = (False, "network error")
    mock_platform.get_instance_status.return_value = (True, {"status": "running"})

    mock_svc = MagicMock()
    mock_svc.platform = mock_platform
    mock_svc.repo = None

    keepalive = InstanceKeepAlive(mock_svc)
    tracker = InstanceTracker(
        challenge_id=2,
        name="pwn_chall",
        meta_path=tmp_path / ".challenge.json",
        platform_kind="whale",
    )

    events = keepalive._begin_restart(tracker)

    # Should report error and not transition to cooldown
    assert any(level == ERROR for level, _ in events)
    assert tracker.restart_phase != "cooldown"
    mock_platform.start_instance.assert_not_called()


def test_whale_gap_ok_respects_cross_process_timestamp_file(tmp_path):
    meta_file = tmp_path / "crypto_chall" / ".challenge.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.touch()

    mock_svc = MagicMock()
    mock_svc.platform = MagicMock()
    mock_svc.repo = None

    keepalive = InstanceKeepAlive(mock_svc)
    tracker = InstanceTracker(
        challenge_id=3,
        name="crypto_chall",
        meta_path=meta_file,
        platform_kind="whale",
    )

    assert keepalive._whale_gap_ok(tracker) is True

    # Simulate another process writing a fresh whale operation timestamp 10s ago
    op_file = meta_file.parent / ".whale_last_op"
    op_file.write_text(str(time.time() - 10), encoding="utf-8")

    # Should detect cross-process operation within 61s gap
    assert keepalive._whale_gap_ok(tracker) is False

    # Simulate operation older than 61s
    op_file.write_text(str(time.time() - 70), encoding="utf-8")
    assert keepalive._whale_gap_ok(tracker) is True
