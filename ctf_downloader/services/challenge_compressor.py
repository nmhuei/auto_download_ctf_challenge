"""Challenge files ultra-compression service.

Compresses CTF challenge files and attachments to maximum ratio (XZ/LZMA2 preset 9 extreme)
before pushing to Git repositories. If a file still exceeds the size threshold (e.g. 50MB)
even after maximum compression, it is safely skipped, ignored in Git, and represented by a
lightweight .skipped.json stub containing SHA256 and metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    import lzma
except ImportError:
    lzma = None  # type: ignore

from ..utils.logger import Logger

# Default threshold for Git / GitHub (GitHub warns at 50MB, rejects at 100MB)
DEFAULT_THRESHOLD_MB = 50
DEFAULT_MIN_SIZE_KB = 50

# Source / metadata extensions that should NOT be compressed unless explicitly forced
EXCLUDED_TEXT_EXTENSIONS = {
    ".py", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".java", ".go", ".rs", ".js", ".ts", ".html", ".css",
    ".sh", ".bash", ".zsh", ".bat", ".ps1",
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".flag",
}

# Already compressed archive extensions
COMPRESSED_EXTENSIONS = {
    ".xz", ".txz", ".lzma", ".gz", ".tgz", ".bz2", ".tbz2",
    ".7z", ".zip", ".zst", ".tzst", ".rar",
}

# Giant raw disk / VM image extensions that cannot feasibly compress under threshold without severe stalling
GIANT_DISK_EXTENSIONS = {".raw", ".vmdk", ".img", ".iso", ".qcow2", ".vdi"}


def _file_sha256(path: Path) -> str:
    """Compute SHA256 checksum of a file efficiently in 64KB chunks."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _human_size(num_bytes: int) -> str:
    """Format bytes into readable human-friendly string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


@dataclass
class PackResult:
    original_path: str
    action: str  # 'compressed', 'skipped_too_large', 'already_compressed', 'ignored_small', 'error'
    original_size: int
    compressed_size: int = 0
    compressed_path: Optional[str] = None
    sha256: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CompressionError(ValueError):
    """Raised when compression, decompression, or checksum verification fails."""
    pass


class ChallengeCompressor:
    """Ultra-compression engine for CTF challenge files with threshold guard."""

    MANIFEST_FILE = "pack_manifest.json"

    @classmethod
    def _has_xz_binary(cls) -> bool:
        """Check if native system xz binary is available."""
        return shutil.which("xz") is not None

    @classmethod
    def compress_file(
        cls,
        file_path: Path,
        threshold_mb: int = DEFAULT_THRESHOLD_MB,
        dest_dir: Optional[Path] = None,
        early_abort: bool = True,
        replace_original: bool = False,
    ) -> PackResult:
        """Compress a single file with maximum XZ/LZMA2 extreme compression.

        If the compressed file exceeds threshold_mb, early aborts or cancels,
        returning an action='skipped_too_large' result.
        """
        if not file_path.is_file():
            return PackResult(
                original_path=str(file_path),
                action="error",
                original_size=0,
                reason="File does not exist",
            )

        try:
            orig_size = file_path.stat().st_size
        except OSError as e:
            return PackResult(
                original_path=str(file_path),
                action="error",
                original_size=0,
                reason=f"Cannot stat file: {e}",
            )

        threshold_bytes = threshold_mb * 1024 * 1024

        def _get_sha() -> str:
            if orig_size > 250 * 1024 * 1024:
                return f"size:{orig_size}"
            return _file_sha256(file_path)

        # Check if already compressed
        ext = file_path.suffix.lower()
        if ext in COMPRESSED_EXTENSIONS:
            if orig_size > threshold_bytes:
                return PackResult(
                    original_path=str(file_path),
                    action="skipped_too_large",
                    original_size=orig_size,
                    compressed_size=orig_size,
                    sha256=_get_sha(),
                    reason=f"Already compressed ({ext}) but exceeds {threshold_mb}MB limit ({_human_size(orig_size)})",
                )
            return PackResult(
                original_path=str(file_path),
                action="already_compressed",
                original_size=orig_size,
                compressed_size=orig_size,
                compressed_path=str(file_path),
                sha256=_get_sha(),
                reason="File is already in compressed archive format",
            )

        # Early guard: giant uncompressed disk images that exceed feasible compression
        if ext in GIANT_DISK_EXTENSIONS and orig_size > max(250 * 1024 * 1024, threshold_bytes * 4):
            return PackResult(
                original_path=str(file_path),
                action="skipped_too_large",
                original_size=orig_size,
                compressed_size=orig_size,
                sha256=_get_sha(),
                reason=f"Giant disk image ({ext}, {_human_size(orig_size)}) exceeds compression budget ({threshold_mb}MB limit)",
            )

        target_dir = dest_dir or file_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_file = target_dir / f"{file_path.name}.xz"
        tmp_dest = target_dir / f"{file_path.name}.xz.tmp"

        if tmp_dest.exists():
            try:
                tmp_dest.unlink()
            except OSError:
                pass

        use_xz_bin = cls._has_xz_binary()
        aborted = False

        if use_xz_bin:
            try:
                # Run xz with -9e (level 9 extreme) and -T0 (all CPU threads)
                with open(file_path, "rb") as fin, open(tmp_dest, "wb") as fout:
                    proc = subprocess.Popen(
                        ["xz", "-9e", "-T0"],
                        stdin=fin,
                        stdout=fout,
                        stderr=subprocess.PIPE,
                    )
                    while proc.poll() is None:
                        if early_abort:
                            try:
                                if tmp_dest.exists() and tmp_dest.stat().st_size > threshold_bytes:
                                    proc.terminate()
                                    try:
                                        proc.wait(timeout=2.0)
                                    except subprocess.TimeoutExpired:
                                        proc.kill()
                                    aborted = True
                                    break
                            except OSError:
                                pass
                        time.sleep(0.05)
                    if not aborted:
                        proc.wait()
                        if proc.returncode != 0:
                            stderr_msg = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                            raise RuntimeError(f"xz failed with code {proc.returncode}: {stderr_msg}")
            except Exception as exc:
                if tmp_dest.exists():
                    try:
                        tmp_dest.unlink()
                    except OSError:
                        pass
                if not aborted:
                    return PackResult(
                        original_path=str(file_path),
                        action="error",
                        original_size=orig_size,
                        reason=f"Native xz compression error: {exc}",
                    )
        else:
            # Pure Python fallback using built-in lzma
            if lzma is None:
                return PackResult(
                    original_path=str(file_path),
                    action="error",
                    original_size=orig_size,
                    reason="Neither system xz nor python lzma module is available",
                )
            try:
                filters = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]
                with open(file_path, "rb") as fin, lzma.open(tmp_dest, "wb", filters=filters) as fout:
                    while chunk := fin.read(1048576):  # 1MB buffer
                        fout.write(chunk)
                        if early_abort and tmp_dest.stat().st_size > threshold_bytes:
                            aborted = True
                            break
            except Exception as exc:
                if tmp_dest.exists():
                    try:
                        tmp_dest.unlink()
                    except OSError:
                        pass
                return PackResult(
                    original_path=str(file_path),
                    action="error",
                    original_size=orig_size,
                    reason=f"Python lzma compression error: {exc}",
                )

        if aborted:
            if tmp_dest.exists():
                try:
                    tmp_dest.unlink()
                except OSError:
                    pass
            return PackResult(
                original_path=str(file_path),
                action="skipped_too_large",
                original_size=orig_size,
                sha256=_get_sha(),
                reason=f"Compressed output exceeded {threshold_mb}MB limit during streaming (early aborted)",
            )

        if not tmp_dest.exists():
            return PackResult(
                original_path=str(file_path),
                action="error",
                original_size=orig_size,
                reason="Compressed file was not created",
            )

        comp_size = tmp_dest.stat().st_size
        if comp_size > threshold_bytes:
            # Over threshold
            try:
                tmp_dest.unlink()
            except OSError:
                pass
            return PackResult(
                original_path=str(file_path),
                action="skipped_too_large",
                original_size=orig_size,
                compressed_size=comp_size,
                sha256=_get_sha(),
                reason=f"Compressed size ({_human_size(comp_size)}) exceeds {threshold_mb}MB limit",
            )

        # Successful compression within threshold!
        if dest_file.exists():
            dest_file.unlink()
        tmp_dest.rename(dest_file)

        final_sha = _get_sha()

        if replace_original and file_path.exists() and file_path != dest_file:
            try:
                file_path.unlink()
            except OSError:
                pass

        return PackResult(
            original_path=str(file_path),
            action="compressed",
            original_size=orig_size,
            compressed_size=comp_size,
            compressed_path=str(dest_file),
            sha256=final_sha,
            reason=f"Successfully compressed to {_human_size(comp_size)} ({100 - (comp_size / orig_size * 100):.1f}% reduction)",
        )

    @classmethod
    def decompress_file(
        cls,
        compressed_path: Path,
        dest_path: Optional[Path] = None,
        expected_sha: Optional[str] = None,
        remove_xz: bool = False,
    ) -> Path:
        """Decompress an .xz challenge file back to its original filename."""
        if not compressed_path.is_file():
            raise FileNotFoundError(f"Compressed file not found: {compressed_path}")

        if not compressed_path.name.endswith(".xz"):
            raise ValueError(f"File {compressed_path.name} does not have .xz extension")

        target = dest_path or compressed_path.with_name(compressed_path.name[:-3])
        tmp_target = target.with_name(target.name + ".decomp.tmp")

        if tmp_target.exists():
            tmp_target.unlink()

        use_xz_bin = cls._has_xz_binary()
        if use_xz_bin:
            with open(compressed_path, "rb") as fin, open(tmp_target, "wb") as fout:
                proc = subprocess.run(
                    ["xz", "-d", "-c", "-T0"],
                    stdin=fin,
                    stdout=fout,
                    capture_output=False,
                    check=False,
                )
                if proc.returncode != 0:
                    if tmp_target.exists():
                        tmp_target.unlink()
                    raise RuntimeError(f"xz decompression failed with code {proc.returncode}")
        else:
            if lzma is None:
                raise RuntimeError("No xz binary or lzma module available")
            with lzma.open(compressed_path, "rb") as fin, open(tmp_target, "wb") as fout:
                shutil.copyfileobj(fin, fout)

        if expected_sha:
            computed = _file_sha256(tmp_target)
            if computed != expected_sha:
                tmp_target.unlink()
                raise CompressionError(f"Checksum mismatch for {target.name}: expected {expected_sha}, got {computed}")

        if target.exists():
            target.unlink()
        tmp_target.rename(target)

        if remove_xz and compressed_path.is_file() and compressed_path != target:
            try:
                compressed_path.unlink()
            except OSError:
                pass

        return target

    @classmethod
    def pack_workspace(
        cls,
        workspace: str | Path,
        threshold_mb: int = DEFAULT_THRESHOLD_MB,
        min_size_kb: int = DEFAULT_MIN_SIZE_KB,
        pack_all: bool = False,
        replace_original: bool = True,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Pack all eligible challenge files in a workspace for safe Git backup.

        Files compressed under threshold_mb replace original heavy files.
        Files exceeding threshold_mb are skipped and added to workspace .gitignore.
        """
        ws = Path(workspace).expanduser().resolve()
        if not ws.exists():
            raise FileNotFoundError(f"Workspace not found: {ws}")

        min_bytes = min_size_kb * 1024
        threshold_bytes = threshold_mb * 1024 * 1024

        meta_dir = ws / ".ctf"
        meta_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = meta_dir / cls.MANIFEST_FILE

        manifest: Dict[str, Any] = {
            "version": 1,
            "threshold_mb": threshold_mb,
            "packed_files": {},
            "skipped_files": {},
        }
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Scan for challenge files
        candidates: List[Path] = []
        for root, dirs, files in os.walk(ws):
            dirs[:] = [
                d for d in dirs
                if d not in {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ctf-solver", "node_modules"}
            ]
            root_p = Path(root)
            # Only pack files located inside challenge/ directories or raw attachments
            is_challenge_dir = "challenge" in root_p.parts or root_p.name == "challenge"

            for fname in files:
                fpath = root_p / fname
                if fname.endswith(".skipped.json") or fname == cls.MANIFEST_FILE:
                    continue

                ext = fpath.suffix.lower()
                try:
                    fsize = fpath.stat().st_size
                except OSError:
                    continue

                if fsize < min_bytes and not pack_all:
                    continue

                if not pack_all and ext in EXCLUDED_TEXT_EXTENSIONS and fsize < 1024 * 1024:
                    continue

                if is_challenge_dir or fsize >= threshold_bytes or ext in COMPRESSED_EXTENSIONS:
                    candidates.append(fpath)

        results: List[PackResult] = []
        skipped_for_gitignore: List[str] = []
        total = len(candidates)

        for idx, cand in enumerate(candidates, 1):
            if progress_cb:
                progress_cb(cand.name, idx, total)

            res = cls.compress_file(
                cand,
                threshold_mb=threshold_mb,
                early_abort=True,
            )
            results.append(res)
            rel_path = str(cand.relative_to(ws))

            if res.action == "compressed":
                comp_p = Path(res.compressed_path)  # type: ignore
                comp_rel = str(comp_p.relative_to(ws))
                manifest.setdefault("packed_files", {})[rel_path] = {
                    "original_path": rel_path,
                    "compressed_path": comp_rel,
                    "original_size": res.original_size,
                    "compressed_size": res.compressed_size,
                    "sha256": res.sha256,
                }
                # Remove large raw original file so git only commits .xz
                if replace_original and cand.exists() and cand != comp_p:
                    try:
                        cand.unlink()
                    except OSError:
                        pass

            elif res.action == "skipped_too_large":
                manifest.setdefault("skipped_files", {})[rel_path] = {
                    "original_path": rel_path,
                    "original_size": res.original_size,
                    "compressed_size": res.compressed_size,
                    "sha256": res.sha256,
                    "reason": res.reason,
                }
                skipped_for_gitignore.append(rel_path)

                # Write a local .skipped.json descriptor in the challenge directory
                stub_file = cand.parent / f"{cand.name}.skipped.json"
                try:
                    stub_data = {
                        "original_file": cand.name,
                        "original_size": res.original_size,
                        "original_size_human": _human_size(res.original_size),
                        "sha256": res.sha256,
                        "status": "skipped_too_large",
                        "reason": res.reason,
                        "threshold_mb": threshold_mb,
                    }
                    stub_file.write_text(json.dumps(stub_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                except OSError:
                    pass

        # Update .gitignore with skipped large files so Git never stages them
        if skipped_for_gitignore:
            cls._ensure_gitignored(ws, skipped_for_gitignore)

        # Save manifest
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        compressed_list = [r for r in results if r.action == "compressed"]
        skipped_list = [r for r in results if r.action == "skipped_too_large"]
        kept_list = [r for r in results if r.action == "already_compressed"]

        orig_sum = sum(r.original_size for r in compressed_list)
        comp_sum = sum(r.compressed_size for r in compressed_list)
        saved = max(0, orig_sum - comp_sum)
        ratio = (saved / orig_sum * 100) if orig_sum > 0 else 0.0

        return {
            "workspace": str(ws),
            "total_scanned": total,
            "compressed_count": len(compressed_list),
            "skipped_count": len(skipped_list),
            "kept_count": len(kept_list),
            "original_total_bytes": orig_sum,
            "compressed_total_bytes": comp_sum,
            "saved_bytes": saved,
            "saved_ratio_percent": ratio,
            "results": [r.to_dict() for r in results],
        }

    @classmethod
    def unpack_workspace(
        cls,
        workspace: str | Path,
        remove_xz: bool = True,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Unpack all compressed challenge files in a workspace back to original state."""
        ws = Path(workspace).expanduser().resolve()
        if not ws.exists():
            raise FileNotFoundError(f"Workspace not found: {ws}")

        manifest_path = ws / ".ctf" / cls.MANIFEST_FILE
        packed_map: Dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                packed_map = data.get("packed_files", {})
            except Exception:
                pass

        unpacked = []
        skipped_existing = []
        errors = []

        if packed_map:
            total = len(packed_map)
            for idx, (orig_rel, info) in enumerate(packed_map.items(), 1):
                comp_rel = info.get("compressed_path")
                if not comp_rel:
                    continue
                comp_path = ws / comp_rel
                orig_path = ws / orig_rel
                if progress_cb:
                    progress_cb(orig_path.name, idx, total)

                if comp_path.is_file():
                    try:
                        exp_sha = info.get("sha256")
                        if orig_path.is_file() and exp_sha and _file_sha256(orig_path) == exp_sha:
                            skipped_existing.append(str(orig_path))
                            if remove_xz and comp_path != orig_path:
                                try:
                                    comp_path.unlink()
                                except OSError:
                                    pass
                            continue

                        cls.decompress_file(
                            comp_path,
                            orig_path,
                            expected_sha=exp_sha,
                            remove_xz=remove_xz,
                        )
                        unpacked.append(str(orig_path))
                    except Exception as exc:
                        errors.append(f"{comp_path.name}: {exc}")
        else:
            # Fallback scan for *.xz inside challenge/ subdirectories
            xz_files = list(ws.glob("**/challenge/**/*.xz"))
            total = len(xz_files)
            for idx, xz_f in enumerate(xz_files, 1):
                if progress_cb:
                    progress_cb(xz_f.name, idx, total)
                try:
                    target_file = xz_f.with_name(xz_f.name[:-3])
                    if target_file.is_file():
                        skipped_existing.append(str(target_file))
                        if remove_xz:
                            try:
                                xz_f.unlink()
                            except OSError:
                                pass
                        continue

                    cls.decompress_file(xz_f, remove_xz=remove_xz)
                    unpacked.append(str(target_file))
                except Exception as exc:
                    errors.append(f"{xz_f.name}: {exc}")

        return {
            "workspace": str(ws),
            "unpacked_count": len(unpacked),
            "skipped_existing_count": len(skipped_existing),
            "error_count": len(errors),
            "unpacked_files": unpacked,
            "skipped_existing_files": skipped_existing,
            "errors": errors,
        }

    @classmethod
    def _ensure_gitignored(cls, workspace: Path, rel_paths: List[str]) -> None:
        """Add skipped large files to workspace .gitignore."""
        gitignore = workspace / ".gitignore"
        existing_lines = set()
        if gitignore.is_file():
            try:
                existing_lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()}
            except OSError:
                pass

        new_entries = []
        for rp in rel_paths:
            # Normalize to POSIX relative path
            posix_rp = Path(rp).as_posix()
            if posix_rp not in existing_lines and f"/{posix_rp}" not in existing_lines:
                new_entries.append(posix_rp)

        if new_entries:
            try:
                with open(gitignore, "a", encoding="utf-8") as f:
                    f.write("\n# Large CTF challenge files (>50MB skipped for GitHub)\n")
                    for entry in new_entries:
                        f.write(f"/{entry}\n")
            except OSError:
                pass
