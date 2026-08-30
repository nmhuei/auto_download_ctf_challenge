#!/usr/bin/env python3
"""Build a wheel and prove packaged Python modules exactly match source bytes."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(out),
            ],
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
            f"match source bytes ({wheel.name})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
