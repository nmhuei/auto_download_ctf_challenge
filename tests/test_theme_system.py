import pytest
from unittest.mock import patch

from ctf_downloader.interactive_menu import CTFInteractiveConsole, _MAIN_ACTIONS_FULL, _update_menu_theme
from ctf_downloader.ui.theme import (
    get_active_palette,
    set_active_theme,
    list_themes,
    load_theme,
)
from ctf_downloader.ui.palettes import PRESET_PALETTES


def test_preset_palettes_exist():
    available = list_themes()
    assert "exodia" in available
    assert "cyberpunk" in available
    assert "matrix" in available
    assert "amber" in available
    assert "nord" in available


def test_set_active_theme():
    assert set_active_theme("cyberpunk") is True
    pal = get_active_palette()
    assert pal.name == "cyberpunk"
    assert pal.accent == "#00F5FF"

    # Switch back to default
    set_active_theme("exodia")
    assert get_active_palette().name == "exodia"


def test_load_theme_by_name():
    theme_matrix = load_theme("matrix")
    assert theme_matrix is not None
    # Semantic token category.web should match matrix accent
    assert "category.web" in theme_matrix.styles


def test_theme_menu_switch(tmp_path):
    ws = tmp_path / "test_ws"
    ws.mkdir()
    app = CTFInteractiveConsole(workspace_path=str(ws))

    # Test selecting theme 2 (Cyberpunk), then pausing ""
    inputs = iter(["2", ""])
    with patch("ctf_downloader.interactive_menu._prompt", side_effect=lambda *args: next(inputs)):
        app._menu_theme()

    assert get_active_palette().name == "cyberpunk"

    # Reset back to default
    set_active_theme("exodia")
