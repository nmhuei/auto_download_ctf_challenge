"""Cloudflare recovery wiring for auto-register and doctor."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.services.health_service import DoctorReport, HealthService
from ctf_downloader.services.register_service import RegisterService
from ctf_downloader.storage.fileio import SKIP_WRITE


def _updater_from(store):
    def update(mutator):
        fresh = dict(store)
        result = mutator(fresh)
        if result is SKIP_WRITE:
            return None
        store.clear()
        store.update(result)
        return dict(result)
    return update


def test_register_parser_accepts_cf_clearance():
    args = build_unified_parser().parse_args([
        "register", "-u", "https://ctf.test",
        "--email", "a@b.c",
        "--cf-clearance", "clear123",
    ])
    assert args.cf_clearance == "clear123"


def test_register_passes_scoped_cf_clearance_into_shared_session(monkeypatch):
    store = {}
    seen = {}

    class Platform:
        def register(self, **kwargs):
            return {"ok": True, "message": "ok"}

    info = SimpleNamespace(platform_type="ctfd", confidence="high")

    class Session:
        def __init__(self):
            self.cookies = []

    def fake_create_session(*, cookie=None, **kwargs):
        seen["cookie"] = cookie
        return Session()

    monkeypatch.setattr(
        "ctf_downloader.services.session_factory.create_session",
        fake_create_session,
    )
    service = RegisterService(
        now_fn=lambda: 1_000_000.0,
        config_loader=lambda: dict(store),
        config_updater=_updater_from(store),
        tempmail_factory=lambda: None,
        detect_fn=lambda *_: (Platform(), info),
    )

    result = service.run(
        "https://ctf.test",
        email="a@b.c",
        cf_clearance="clear123",
    )
    assert result["ok"] is True
    assert seen["cookie"] == "cf_clearance=clear123"


def test_register_accepts_full_cf_clearance_cookie_without_double_prefix(monkeypatch):
    store = {}
    seen = {}

    class Platform:
        def register(self, **kwargs):
            return {"ok": True, "message": "ok"}

    info = SimpleNamespace(platform_type="ctfd", confidence="high")

    class Session:
        def __init__(self):
            self.cookies = []

    def fake_create_session(*, cookie=None, **kwargs):
        seen["cookie"] = cookie
        return Session()

    monkeypatch.setattr(
        "ctf_downloader.services.session_factory.create_session",
        fake_create_session,
    )
    service = RegisterService(
        now_fn=lambda: 2_000_000.0,
        config_loader=lambda: dict(store),
        config_updater=_updater_from(store),
        tempmail_factory=lambda: None,
        detect_fn=lambda *_: (Platform(), info),
    )
    service.run(
        "https://ctf.test",
        email="a@b.c",
        cf_clearance="cf_clearance=clear456",
    )
    assert seen["cookie"] == "cf_clearance=clear456"


def test_register_persists_cf_clearance_together_with_platform_cookie(monkeypatch):
    store = {}

    class Cookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class Session:
        def __init__(self):
            self.cookies = [Cookie("cf_clearance", "clear123")]

    class Platform:
        def register(self, **kwargs):
            return {
                "ok": True,
                "message": "ok",
                "cookies": {"session": "platform456"},
            }

    info = SimpleNamespace(platform_type="ctfd", confidence="high")
    monkeypatch.setattr(
        "ctf_downloader.services.session_factory.create_session",
        lambda **_kwargs: Session(),
    )
    service = RegisterService(
        now_fn=lambda: 3_000_000.0,
        config_loader=lambda: dict(store),
        config_updater=_updater_from(store),
        tempmail_factory=lambda: None,
        detect_fn=lambda *_: (Platform(), info),
    )
    result = service.run(
        "https://ctf.test",
        email="a@b.c",
        cf_clearance="clear123",
    )
    assert result["ok"] is True
    saved = store["auth"]["https://ctf.test"]["cookie"]
    assert "cf_clearance=clear123" in saved
    assert "session=platform456" in saved


def test_doctor_url_check_surfaces_cloudflare_challenge_action():
    class Resp:
        status_code = 403
        headers = {
            "cf-mitigated": "challenge",
            "content-type": "text/html",
        }
        text = "<html>Just a moment...</html>"

    class Session:
        def get(self, *_args, **_kwargs):
            return Resp()

    report = DoctorReport(url="https://ctf.test")
    ok = HealthService()._check_url(report, "https://ctf.test", Session())
    assert ok is False
    check = next(c for c in report.checks if c.name == "URL sống")
    assert "Cloudflare Challenge" in check.detail
    assert "cf_clearance" in check.fix
