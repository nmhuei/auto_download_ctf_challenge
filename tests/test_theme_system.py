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


def test_theme_menu_switch(tmp_path, monkeypatch):
    ws = tmp_path / "test_ws"
    ws.mkdir()
    app = CTFInteractiveConsole(workspace_path=str(ws))

    fake_config = {}
    monkeypatch.setattr(
        "ctf_downloader.storage.global_config.update_global_config",
        lambda mut: fake_config.update(mut(dict(fake_config))) or fake_config,
    )

    try:
        # Test selecting theme 2 (Cyberpunk), then pausing ""
        inputs = iter(["2", ""])
        with patch("ctf_downloader.interactive_menu._prompt", side_effect=lambda *args: next(inputs)):
            app._menu_theme()

        assert get_active_palette().name == "cyberpunk"
        assert fake_config.get("theme") == "cyberpunk"
    finally:
        set_active_theme(None)


def test_expanded_preset_palettes_available():
    available = list_themes()
    for expected in ("dracula", "tokyo", "synthwave", "monokai", "crimson"):
        assert expected in available


def test_palette_color_ramp_and_spectrum_text():
    pal = PRESET_PALETTES["dracula"]
    swatch = pal.color_ramp_text()
    assert swatch is not None
    assert "■" in swatch.plain

    spectrum = pal.full_spectrum_text()
    assert spectrum is not None
    assert "Core Spectrum:" in spectrum.plain
    assert "Category Spectrum:" in spectrum.plain
    assert "Web" in spectrum.plain


def test_theme_menu_switch_by_name(tmp_path, monkeypatch):
    ws = tmp_path / "test_ws"
    ws.mkdir()
    app = CTFInteractiveConsole(workspace_path=str(ws))

    fake_config = {}
    monkeypatch.setattr(
        "ctf_downloader.storage.global_config.update_global_config",
        lambda mut: fake_config.update(mut(dict(fake_config))) or fake_config,
    )

    try:
        inputs = iter(["dracula", ""])
        with patch("ctf_downloader.interactive_menu._prompt", side_effect=lambda *args: next(inputs)):
            app._menu_theme()

        assert get_active_palette().name == "dracula"
        assert fake_config.get("theme") == "dracula"
    finally:
        set_active_theme(None)


def test_theme_persistence_and_init_theme(tmp_path, monkeypatch):
    from ctf_downloader.ui.theme import init_theme
    import ctf_downloader.ui.brand as brand

    fake_cfg = {"theme": "tokyo"}
    monkeypatch.setattr("ctf_downloader.storage.global_config.load_global_config", lambda: fake_cfg)
    monkeypatch.setattr("ctf_downloader.interactive_menu.load_global_config", lambda: fake_cfg)

    try:
        pal = init_theme()
        assert pal.name == "tokyo"
        assert get_active_palette().name == "tokyo"
        assert brand.BRAND_NAME == "UCS_Tokyo"

        # Initialize CTFInteractiveConsole and verify it activates the saved theme
        ws = tmp_path / "ws"
        ws.mkdir()
        app = CTFInteractiveConsole(workspace_path=str(ws))
        assert get_active_palette().name == "tokyo"
    finally:
        set_active_theme(None)

