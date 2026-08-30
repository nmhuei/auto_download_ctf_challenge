import json
import os
import pytest

def test_manifest_v3_structure():
    manifest_path = "extension/manifest.json"
    assert os.path.exists(manifest_path), "Missing extension/manifest.json"
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["manifest_version"] == 3
    assert data["name"] == "CTF Operations Bridge"
    assert "background" in data
    assert "service_worker" in data["background"]
    assert "cookies" in data["permissions"]
    assert "storage" in data["permissions"]
    sw_file = os.path.join("extension", data["background"]["service_worker"])
    assert os.path.exists(sw_file), f"Service worker not found: {sw_file}"

def test_extension_required_files():
    required = [
        "extension/manifest.json",
        "extension/background/service_worker.js",
        "extension/background/bridge_client.js",
        "extension/popup/popup.html",
        "extension/popup/popup.js",
        "extension/popup/popup.css",
        "extension/README.md",
    ]
    for r in required:
        assert os.path.exists(r), f"Missing required extension file: {r}"
