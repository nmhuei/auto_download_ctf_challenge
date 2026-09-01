#!/usr/bin/env python3
"""Real-browser verification for the Manifest V3 Browser Extension Bridge.

This is intentionally separate from the normal pytest suite because it needs a
locally installed Chromium-family browser and matching ChromeDriver. It starts a
loopback HTTP fixture, loads extension/ in headless Chromium, pairs it with the
local bridge daemon, and verifies both JSON forwarding and disk-backed binary
streaming.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import shutil
import socketserver
import sys
import threading
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "extension"
LARGE_PAYLOAD = b"PK\x03\x04" + (bytes(range(256)) * 5000)


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/api":
            body = json.dumps(
                {"ok": True, "source": "real-chromium-extension"}
            ).encode("utf-8")
            content_type = "application/json"
            status = 200
        elif self.path == "/large.bin":
            body = LARGE_PAYLOAD
            content_type = "application/octet-stream"
            status = 200
        else:
            body = b"not found"
            content_type = "text/plain"
            status = 404

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def _first_executable(candidates: list[str]) -> str | None:
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Browser Bridge with a real Chromium extension runtime."
    )
    parser.add_argument("--browser", help="Chromium/Chrome executable path")
    parser.add_argument("--driver", help="Matching chromedriver executable path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    browser = args.browser or _first_executable(
        ["chromium", "google-chrome", "google-chrome-stable", "brave-browser"]
    )
    driver_path = args.driver or _first_executable(["chromedriver"])
    if not browser or not driver_path:
        print(
            "Missing Chromium-family browser or chromedriver. "
            "Install matching versions and retry.",
            file=sys.stderr,
        )
        return 2

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        print(
            "Missing Selenium. Install: python -m pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return 2

    from ctf_downloader.bridge.daemon import BridgeDaemon
    from ctf_downloader.bridge.transport import BrowserBridgeTransport

    daemon = BridgeDaemon()
    daemon_was_running = daemon.is_running() and daemon.is_port_open()
    daemon_started_here = False
    if not daemon_was_running:
        if not daemon.ensure_running():
            print(
                f"Bridge daemon could not start on {daemon.host}:{daemon.port}.",
                file=sys.stderr,
            )
            return 1
        daemon_started_here = True

    token = daemon.get_or_create_token()
    fixture = _ThreadingServer(("127.0.0.1", 0), _FixtureHandler)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    fixture_port = int(fixture.server_address[1])

    options = Options()
    options.binary_location = browser
    for flag in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        f"--disable-extensions-except={EXTENSION_DIR}",
        f"--load-extension={EXTENSION_DIR}",
    ):
        options.add_argument(flag)

    browser_driver = None
    try:
        browser_driver = webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )
        wait = WebDriverWait(browser_driver, 12)

        def extension_id(_driver: object) -> str | bool:
            targets = browser_driver.execute_cdp_cmd(
                "Target.getTargets", {}
            ).get("targetInfos", [])
            for target in targets:
                url = str(target.get("url", ""))
                if (
                    url.startswith("chrome-extension://")
                    and "/background/service_worker.js" in url
                ):
                    return url.split("/")[2]
            return False

        ext_id = wait.until(extension_id)
        browser_driver.get(
            f"chrome-extension://{ext_id}/popup/popup.html"
        )
        token_input = wait.until(
            lambda drv: drv.find_element(By.ID, "token-input")
        )
        token_input.clear()
        token_input.send_keys(token)
        browser_driver.find_element(By.ID, "btn-save").click()
        wait.until(
            lambda drv: drv.find_element(By.ID, "status-indicator").text
            == "CONNECTED"
        )

        transport = BrowserBridgeTransport(
            host=daemon.host,
            port=daemon.port,
            token=token,
            auto_start_daemon=False,
            timeout=10,
        )

        api_response = transport.send(
            requests.Request(
                "GET", f"http://127.0.0.1:{fixture_port}/api"
            ).prepare(),
            timeout=10,
        )
        if api_response.status_code != 200:
            raise RuntimeError(
                f"Browser text forward returned HTTP {api_response.status_code}"
            )
        if api_response.json() != {
            "ok": True,
            "source": "real-chromium-extension",
        }:
            raise RuntimeError("Browser text forward body mismatch")

        binary_response = transport.send(
            requests.Request(
                "GET", f"http://127.0.0.1:{fixture_port}/large.bin"
            ).prepare(),
            timeout=15,
            stream=True,
        )
        if binary_response.status_code != 200:
            raise RuntimeError(
                f"Browser binary forward returned HTTP {binary_response.status_code}"
            )
        if binary_response._content is not False:
            raise RuntimeError("stream=True response was materialized in memory")

        digest = hashlib.sha256()
        received = 0
        for chunk in binary_response.iter_content(chunk_size=128 * 1024):
            digest.update(chunk)
            received += len(chunk)
        binary_response.close()

        expected_digest = hashlib.sha256(LARGE_PAYLOAD).digest()
        if received != len(LARGE_PAYLOAD) or digest.digest() != expected_digest:
            raise RuntimeError("Browser binary stream length/hash mismatch")

        print("REAL_BROWSER_BRIDGE_OK")
        print(f"bytes={received}")
        print(f"sha256={digest.hexdigest()}")
        return 0
    finally:
        if browser_driver is not None:
            browser_driver.quit()
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=2.0)
        if daemon_started_here:
            daemon.stop()


if __name__ == "__main__":
    raise SystemExit(main())
