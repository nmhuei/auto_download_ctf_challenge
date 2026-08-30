"""Deep readiness contracts for doctor/health."""

from __future__ import annotations

import datetime as dt
import json

from unittest.mock import MagicMock

from ctf_downloader.platforms.base import EventTimes
from ctf_downloader.services.health_service import DoctorReport, HealthService


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_ended_event_is_not_ready():
    now = dt.datetime.now(dt.timezone.utc)
    platform = MagicMock()
    platform.fetch_event_times.return_value = EventTimes(
        start_utc=now - dt.timedelta(hours=2),
        end_utc=now - dt.timedelta(hours=1),
    )
    report = DoctorReport()
    HealthService()._check_event_window(report, platform)
    chk = _check(report, "Event window")
    assert chk.ok is False
    assert "ĐÃ KẾT THÚC" in chk.detail
    assert "đã kết thúc" in chk.fix.lower()


def test_future_event_is_ready_with_countdown():
    now = dt.datetime.now(dt.timezone.utc)
    platform = MagicMock()
    platform.fetch_event_times.return_value = EventTimes(
        start_utc=now + dt.timedelta(hours=1),
        end_utc=now + dt.timedelta(hours=3),
    )
    report = DoctorReport()
    HealthService()._check_event_window(report, platform)
    chk = _check(report, "Event window")
    assert chk.ok is True
    assert "CHƯA BẮT ĐẦU" in chk.detail


def test_empty_event_window_is_not_false_green():
    platform = MagicMock()
    platform.fetch_event_times.return_value = EventTimes()
    report = DoctorReport()
    HealthService()._check_event_window(report, platform)
    chk = _check(report, "Event window")
    assert chk.ok is False
    assert "rỗng" in chk.detail


def test_inverted_event_window_is_not_ready():
    now = dt.datetime.now(dt.timezone.utc)
    platform = MagicMock()
    platform.fetch_event_times.return_value = EventTimes(
        start_utc=now + dt.timedelta(hours=2),
        end_utc=now + dt.timedelta(hours=1),
    )
    report = DoctorReport()
    HealthService()._check_event_window(report, platform)
    chk = _check(report, "Event window")
    assert chk.ok is False
    assert "không hợp lệ" in chk.detail


def test_workspace_flag_format_is_valid_fallback_when_rules_unavailable(tmp_path):
    (tmp_path / "challenges.json").write_text(
        json.dumps({
            "ctf_info": {"flag_format": r"^ASIS{[A-Za-z0-9_]+}$"},
            "challenges": [],
        }),
        encoding="utf-8",
    )
    platform = MagicMock()
    platform.fetch_rules.return_value = None
    report = DoctorReport()
    HealthService()._check_flag_format(
        report, platform, workspace=str(tmp_path)
    )
    chk = _check(report, "Flag format")
    assert chk.ok is True
    assert "baseline workspace" in chk.detail
    assert "ASIS" in chk.detail


def test_invalid_workspace_flag_regex_does_not_false_green(tmp_path):
    (tmp_path / "challenges.json").write_text(
        json.dumps({
            "ctf_info": {"flag_format": r"^(broken["},
            "challenges": [],
        }),
        encoding="utf-8",
    )
    platform = MagicMock()
    platform.fetch_rules.return_value = None
    report = DoctorReport()
    HealthService()._check_flag_format(
        report, platform, workspace=str(tmp_path)
    )
    chk = _check(report, "Flag format")
    assert chk.ok is False
