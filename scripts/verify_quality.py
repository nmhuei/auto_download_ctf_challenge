#!/usr/bin/env python3
"""Run reproducible quality/security gates for the CTF toolkit."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TYPE_TARGETS = [
    # Persistence/download core
    "ctf_downloader/config.py",
    "ctf_downloader/models.py",
    "ctf_downloader/storage/fileio.py",
    "ctf_downloader/downloaders/http_downloader.py",
    "ctf_downloader/downloaders/manager.py",
    # Deep-debug reliability paths
    "ctf_downloader/utils/gzctf_crypto.py",
    "ctf_downloader/utils/http_client.py",
    "ctf_downloader/utils/tempmail.py",
    "ctf_downloader/core.py",
    "ctf_downloader/services/session_factory.py",
    "ctf_downloader/services/platform_resolver.py",
    "ctf_downloader/services/pull_service.py",
    "ctf_downloader/services/register_service.py",
    "ctf_downloader/services/submit_service.py",
    "ctf_downloader/services/sniper_service.py",
    "ctf_downloader/services/rank_service.py",
    "ctf_downloader/services/git_workflow.py",
    "ctf_downloader/services/health_service.py",
    "ctf_downloader/platforms/ctfd.py",
    "ctf_downloader/platforms/gzctf.py",
    "ctf_downloader/platforms/rctf.py",
    # Browser Bridge reliability boundary
    "ctf_downloader/bridge/constants.py",
    "ctf_downloader/bridge/messages.py",
    "ctf_downloader/bridge/daemon.py",
    "ctf_downloader/bridge/server.py",
    "ctf_downloader/bridge/transport.py",
    "scripts/verify_bridge_browser.py",
    # Generated-doc contract
    "scripts/generate_cli_option_index.py",
]


def run(label: str, command: list[str]) -> bool:
    print("\n== " + label + " ==")
    proc = subprocess.run(command, cwd=ROOT)
    if proc.returncode:
        print(label + " FAILED (exit " + str(proc.returncode) + ")", file=sys.stderr)
        return False
    print(label + " PASS")
    return True


def require(name: str) -> str | None:
    exe = shutil.which(name)
    if exe is None:
        print(
            "Missing dev tool '" + name + "'. Install: python -m pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
    return exe


def main() -> int:
    tools = {name: require(name) for name in ("ruff", "mypy", "bandit", "pip-audit")}
    if any(value is None for value in tools.values()):
        return 2

    checks = [
        run("compileall", [sys.executable, "-m", "compileall", "-q", "ctf_downloader", "scripts"]),
        run("generated CLI docs", [sys.executable, "scripts/generate_cli_option_index.py", "--check"]),
        run("wheel source integrity", [sys.executable, "scripts/verify_wheel_contents.py"]),
        run("ruff correctness", [tools["ruff"], "check", "ctf_downloader", "scripts"]),
        run(
            "mypy reliability core",
            [tools["mypy"], "--follow-imports=skip", *TYPE_TARGETS],
        ),
        run("bandit high severity", [tools["bandit"], "-q", "-lll", "-r", "ctf_downloader"]),
        run("pip-audit runtime deps", [tools["pip-audit"], "-r", "requirements.txt"]),
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
