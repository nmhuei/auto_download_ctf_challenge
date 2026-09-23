"""Tests for ctf auth CLI command and interactive Burp Suite cookie synchronization."""

from argparse import Namespace
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ctf_downloader.cli_commands import handle_auth
from ctf_downloader.services.auth_service import AuthService
from ctf_downloader.services.burp_service import BurpService


def test_handle_auth_save_cookie_and_token():
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = str(Path(tmp_dir).resolve())
        args = Namespace(
            workspace=ws,
            url="https://demo.ctf",
            cookie="session=demo123; XSRF=abc",
            token="token_xyz",
            from_burp=False,
            clear=False,
            show=False,
        )
        handle_auth(args)

        c, t = AuthService.resolve(ws, allow_burp_fallback=False)
        assert c == "session=demo123; XSRF=abc"
        assert t == "token_xyz"


def test_handle_auth_clear():
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = str(Path(tmp_dir).resolve())
        # First save
        AuthService.save_auth(ws, url="https://demo.ctf", cookie="session=val", token=None)
        assert AuthService.resolve(ws, allow_burp_fallback=False)[0] == "session=val"

        # Now clear via handle_auth
        args = Namespace(
            workspace=ws,
            url="https://demo.ctf",
            cookie=None,
            token=None,
            from_burp=False,
            clear=True,
            show=False,
        )
        handle_auth(args)
        c, t = AuthService.resolve(ws, allow_burp_fallback=False)
        assert c is None


def test_handle_auth_from_burp():
    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = str(Path(tmp_dir).resolve())
        args = Namespace(
            workspace=ws,
            url="https://asisctf.com",
            cookie=None,
            token=None,
            from_burp=True,
            burp_port=9876,
            clear=False,
            show=False,
        )

        with patch.object(BurpService, "is_mcp_available", return_value=True), \
             patch.object(BurpService, "extract_cookies", return_value={"session": "burp_live", "XSRF": "tok"}):
            handle_auth(args)

        c, t = AuthService.resolve(ws, allow_burp_fallback=False)
        assert "session=burp_live" in c
        assert "XSRF=tok" in c


def test_menu_configure_auth_burp_option():
    from ctf_downloader.interactive_menu import CTFInteractiveConsole

    with tempfile.TemporaryDirectory() as tmp_dir:
        ws = str(Path(tmp_dir).resolve())
        app = CTFInteractiveConsole(workspace_path=ws)

        # Mock render_hub_menu returning '4', then '0'
        menu_responses = iter(['4', '0'])
        with patch("ctf_downloader.ui.menu_hubs.render_hub_menu", side_effect=lambda **kwargs: next(menu_responses)), \
             patch("ctf_downloader.interactive_menu._prompt", return_value="https://asisctf.com"), \
             patch.object(BurpService, "is_mcp_available", return_value=True), \
             patch.object(BurpService, "extract_cookies", return_value={"session": "menu_burp", "asis_session": "active"}), \
             patch("ctf_downloader.interactive_menu._pause"):
            app._menu_configure_auth()

        assert app.cookie is not None
        assert "session=menu_burp" in app.cookie
        assert "asis_session=active" in app.cookie
