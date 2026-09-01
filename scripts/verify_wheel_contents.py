#!/usr/bin/env python3
"""Build a wheel and prove packaged Python modules exactly match source bytes."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module_available(name: str) -> bool:
    """Return whether an executable Python module is importable.

    ``find_spec('build')`` alone is insufficient: an empty/namespace package
    named build can exist while ``python -m build`` still fails because
    ``build.__main__`` is absent.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _wheel_build_command(out: Path) -> tuple[list[str], str]:
    """Select a reproducible wheel builder with an explicit fallback."""
    if _module_available("build.__main__"):
        return ([
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out),
        ], "python-build")

    if _module_available("pip.__main__"):
        return ([
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(out),
        ], "pip-wheel-fallback")

    raise RuntimeError(
        "Không có wheel builder khả dụng: thiếu cả build.__main__ và "
        "pip.__main__. Cài requirements-dev.txt rồi thử lại."
    )

def main() -> int:
    source_root = ROOT / "ctf_downloader"
    expected = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }

    with tempfile.TemporaryDirectory(prefix="ctf-wheel-integrity-") as td:
        out = Path(td) / "dist"
        out.mkdir()
        try:
            command, backend = _wheel_build_command(out)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        proc = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        if proc.returncode:
            sys.stderr.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode or 1

        wheels = sorted(out.glob("*.whl"))
        if len(wheels) != 1:
            print(
                f"expected exactly one wheel, found {len(wheels)} in {out}",
                file=sys.stderr,
            )
            return 1

        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as zf:
            packaged_names = {
                name
                for name in zf.namelist()
                if name.startswith("ctf_downloader/") and name.endswith(".py")
            }
            expected_names = set(expected)

            missing = sorted(expected_names - packaged_names)
            extra = sorted(packaged_names - expected_names)
            mismatched = []
            for name in sorted(expected_names & packaged_names):
                if zf.read(name) != expected[name]:
                    mismatched.append(name)

        if missing or extra or mismatched:
            if missing:
                print("wheel missing modules:", file=sys.stderr)
                for name in missing:
                    print(f"  - {name}", file=sys.stderr)
            if extra:
                print("wheel has unexpected modules:", file=sys.stderr)
                for name in extra:
                    print(f"  + {name}", file=sys.stderr)
            if mismatched:
                print("wheel contains stale/truncated module bytes:", file=sys.stderr)
                for name in mismatched:
                    print(f"  ! {name}", file=sys.stderr)
            return 1

        print(
            f"wheel source integrity PASS: {len(expected)} Python modules "
            f"match source bytes ({wheel.name}; builder={backend})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
