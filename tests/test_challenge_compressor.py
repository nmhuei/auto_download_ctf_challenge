import json
import os
import shutil
from pathlib import Path
import pytest

from ctf_downloader.services.challenge_compressor import (
    ChallengeCompressor,
    CompressionError,
    COMPRESSED_EXTENSIONS,
)
from ctf_downloader.services.git_workflow import GitWorkflowService


def test_compress_and_decompress_file(tmp_path):
    f = tmp_path / "chal.bin"
    # Create non-trivial compressible content > 50KB to pass min_size threshold
    original_data = b"AntigravityCTFCompressorPayload" * 3000
    f.write_bytes(original_data)

    # 1. Compress
    res = ChallengeCompressor.compress_file(f, threshold_mb=50, replace_original=True)
    assert res.action == "compressed"
    assert not f.exists()  # Replaced
    xz_file = tmp_path / "chal.bin.xz"
    assert xz_file.exists()
    assert res.compressed_size < len(original_data)
    assert res.sha256 != ""

    # 2. Decompress
    decomp_path = ChallengeCompressor.decompress_file(
        xz_file,
        expected_sha=res.sha256,
        remove_xz=True,
    )
    assert decomp_path == f
    assert not xz_file.exists()
    assert f.exists()
    assert f.read_bytes() == original_data


def test_compress_already_compressed(tmp_path):
    f = tmp_path / "archive.tar.gz"
    f.write_bytes(b"\x1f\x8b\x08" + b"dummygzipdata" * 10)
    res = ChallengeCompressor.compress_file(f, threshold_mb=50)
    assert res.action == "already_compressed"
    assert f.exists()


def test_compress_threshold_early_abort(tmp_path):
    f = tmp_path / "random_noise.bin"
    # Uncompressible pseudo-random data of 2MB
    random_data = os.urandom(2 * 1024 * 1024)
    f.write_bytes(random_data)

    # With threshold 1MB, compressed random data will exceed 1MB and trigger early abort
    res = ChallengeCompressor.compress_file(f, threshold_mb=1)
    assert res.action == "skipped_too_large"
    assert f.exists()  # Original preserved
    assert not (tmp_path / "random_noise.bin.xz").exists()


def test_decompress_corrupted_checksum(tmp_path):
    f = tmp_path / "data.bin"
    # Use > 50KB data
    f.write_bytes(b"Secret Data payload " * 4000)
    res = ChallengeCompressor.compress_file(f)
    assert res.action == "compressed"
    xz_file = tmp_path / "data.bin.xz"
    assert xz_file.exists()

    # Attempt decompression with wrong expected SHA256
    with pytest.raises(CompressionError, match="Checksum mismatch"):
        ChallengeCompressor.decompress_file(
            xz_file,
            expected_sha="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            remove_xz=False,
        )


def test_pack_and_unpack_workspace(tmp_path):
    ws = tmp_path / "CTF_Workspace"
    ws.mkdir()
    chal_dir = ws / "challenge" / "crypto" / "chal1"
    chal_dir.mkdir(parents=True)

    file_a = chal_dir / "chall.bin"
    file_a.write_bytes(b"flag{test_flag_payload_for_testing}\n" * 3000)

    # Big uncompressible file to trigger skip (> 1MB threshold)
    big_file = chal_dir / "big_disk.img"
    big_file.write_bytes(os.urandom(2 * 1024 * 1024))

    # Pack with threshold 1MB
    rep = ChallengeCompressor.pack_workspace(ws, threshold_mb=1, pack_all=False)

    assert rep["compressed_count"] >= 1
    assert rep["skipped_count"] >= 1
    assert (chal_dir / "chall.bin.xz").exists()
    assert not file_a.exists()

    # Verify big file skipped, .skipped.json created, and .gitignore updated
    assert big_file.exists()
    skipped_json = chal_dir / "big_disk.img.skipped.json"
    assert skipped_json.exists()
    skipped_meta = json.loads(skipped_json.read_text())
    assert skipped_meta["original_file"] == "big_disk.img"
    assert skipped_meta["status"] == "skipped_too_large"

    gitignore = ws / ".gitignore"
    assert gitignore.exists()
    assert "big_disk.img" in gitignore.read_text()

    # Check manifest
    manifest_path = ws / ".ctf" / "pack_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "challenge/crypto/chal1/chall.bin" in manifest.get("packed_files", {})

    # Now unpack workspace
    unpack_rep = ChallengeCompressor.unpack_workspace(ws, remove_xz=True)
    assert unpack_rep["unpacked_count"] >= 1
    assert file_a.exists()
    assert b"flag{test_flag_payload_for_testing}" in file_a.read_bytes()
    assert not (chal_dir / "chall.bin.xz").exists()


def test_git_checkpoint_and_push_packs_challenges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = repo / "TestCTF"
    ws.mkdir()
    chal_dir = ws / "challenge" / "rev"
    chal_dir.mkdir(parents=True)

    binary_file = chal_dir / "program.bin"
    binary_file.write_bytes(b"\x7fELF" + b"\x90" * 80000)

    # Initialize Git
    import subprocess
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CTF Bot"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "bot@ctf.local"], cwd=str(repo), check=True)

    # Checkpoint with pack_challenges=True
    res = GitWorkflowService.checkpoint_and_push(
        ws,
        message="test checkpoint",
        push=False,
        pack_challenges=True,
        threshold_mb=50,
    )
    assert res["committed"] is True
    assert "pack_report" in res
    pack_rep = res["pack_report"]
    assert pack_rep["compressed_count"] >= 1
    assert (chal_dir / "program.bin.xz").exists()
    assert not binary_file.exists()


def test_cli_pack_and_unpack_arguments():
    from ctf_downloader.cli import build_unified_parser
    parser = build_unified_parser()

    # Top-level pack & unpack
    args = parser.parse_args(["pack", "-w", "/tmp/ws", "--threshold", "30", "--all"])
    assert args.subcommand == "pack"
    assert args.workspace == "/tmp/ws"
    assert args.threshold == 30
    assert args.all is True

    args = parser.parse_args(["unpack", "-w", "/tmp/ws", "--keep-xz"])
    assert args.subcommand == "unpack"
    assert args.workspace == "/tmp/ws"
    assert args.keep_xz is True

    # Git subcommands pack & unpack & push flags
    args = parser.parse_args(["git", "pack", "-w", "/tmp/ws", "--threshold", "40"])
    assert args.subcommand == "git"
    assert args.git_command == "pack"
    assert args.threshold == 40

    args = parser.parse_args(["git", "unpack", "-w", "/tmp/ws"])
    assert args.subcommand == "git"
    assert args.git_command == "unpack"

    args = parser.parse_args(["git", "push", "-w", "/tmp/ws", "--no-pack", "--threshold", "25"])
    assert args.subcommand == "git"
    assert args.git_command == "push"
    assert args.no_pack is True
    assert args.threshold == 25


def test_cli_handlers_pack_and_unpack(tmp_path):
    from ctf_downloader.cli import build_unified_parser
    from ctf_downloader.cli_commands import handle_pack, handle_unpack

    ws = tmp_path / "CLI_WS"
    ws.mkdir()
    chal_dir = ws / "challenge" / "misc"
    chal_dir.mkdir(parents=True)
    target_file = chal_dir / "chall.bin"
    target_file.write_bytes(b"A" * 70000)

    parser = build_unified_parser()

    # Call handle_pack
    pack_args = parser.parse_args(["pack", "-w", str(ws), "--threshold", "50"])
    handle_pack(pack_args)
    assert not target_file.exists()
    assert (chal_dir / "chall.bin.xz").exists()

    # Call handle_unpack
    unpack_args = parser.parse_args(["unpack", "-w", str(ws)])
    handle_unpack(unpack_args)
    assert target_file.exists()
    assert target_file.read_bytes() == b"A" * 70000


def test_compress_giant_disk_early_skip(tmp_path):
    f = tmp_path / "forensics_disk.raw"
    with open(f, "wb") as fp:
        fp.truncate(300 * 1024 * 1024)  # 300MB sparse file
    res = ChallengeCompressor.compress_file(f, threshold_mb=50)
    assert res.action == "skipped_too_large"
    assert "Giant disk image" in res.reason

