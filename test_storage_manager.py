"""Tests cho StorageManager (spec storage-manager).

Kiểm tra thuần trên cây workspace giả lập trong tmpdir:
- scan_usage: số liệu đúng (os.truncate sparse), breakdown theo đường dẫn,
  largest_files, challenge_count, ended từ mirror Event Window.
- format_report: sort giảm dần, human-readable, glyph ngưỡng !/✗.
- archive_workspace: tar.gz exclude đúng (__pycache__, *.pyc, .pytest_cache,
  *.part, *.tmp, .ctf/watch_state.json), ratio đúng, git_remote push thật
  tới ``git init --bare`` local (không mạng).
- delete_workspace: rename vào thùng rác, không mất dữ liệu.
- suggest_actions: gợi ý chứa tên workspace khi ended quá hạn / vượt ngưỡng.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.services.storage_manager import (
    StorageError,
    StorageManager,
    WorkspaceUsage,
    human_size,
    parse_event_end,
)


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)


def _truncate(path, size):
    """Tạo file sparse kích thước biết trước (không tốn đĩa thật)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def _set_event_window(ws, start_epoch, end_epoch):
    _write(
        os.path.join(ws, "challenges.json"),
        json.dumps({"ctf_info": {"event_window": {
            "start": start_epoch, "end": end_epoch}}}),
    )


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="storage_mgr_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = os.path.join(self.tmp, "CTF")
        os.makedirs(self.base)

    # ------------------------------------------------------------------
    # Cây giả lập chuẩn: 2 workspace có số liệu biết trước.
    #   alpha: attachments 1000 + writeups 200 + solvers 50 + misc 30
    #          (+ __pycache__/junk.pyc 4096 bị bỏ qua) => total 1280
    #   beta : chỉ misc 500
    # ------------------------------------------------------------------
    def make_alpha(self, name="alpha"):
        ws = os.path.join(self.base, name)
        _truncate(os.path.join(ws, "Web", "chall1", "challenge", "app.zip"), 1000)
        _truncate(os.path.join(ws, "Web", "chall1", "writeup", "README.md"), 200)
        _truncate(os.path.join(ws, "Web", "chall1", "solver", "solve.py"), 50)
        _truncate(os.path.join(ws, "notes.txt"), 30)
        _truncate(os.path.join(ws, "Web", "chall1", "__pycache__", "x.pyc"), 4096)
        return ws

    def make_beta(self, name="beta"):
        ws = os.path.join(self.base, name)
        _truncate(os.path.join(ws, "misc", "dump.bin"), 500)
        return ws


class TestScanUsage(StorageTestBase):
    def test_sizes_and_breakdown_correct(self):
        self.make_alpha()
        self.make_beta()
        usages = {u.name: u for u in StorageManager.scan_usage(self.base)}

        self.assertEqual(set(usages), {"alpha", "beta"})
        a = usages["alpha"]
        self.assertEqual(a.total_bytes, 1280)
        self.assertEqual(a.breakdown["attachments"], 1000)
        self.assertEqual(a.breakdown["writeups"], 200)
        self.assertEqual(a.breakdown["solvers"], 50)
        self.assertEqual(a.breakdown["misc"], 30)
        self.assertEqual(usages["beta"].total_bytes, 500)

    def test_skip_pycache_git_pytest(self):
        ws = self.make_alpha("skipme")
        _truncate(os.path.join(ws, ".git", "objects", "big.pack"), 99999)
        _truncate(os.path.join(ws, ".pytest_cache", "v", "cache.sqlite"), 8888)
        usages = {u.name: u for u in StorageManager.scan_usage(self.base)}
        self.assertEqual(usages["skipme"].total_bytes, 1280)

    def test_largest_files_sorted_top10(self):
        self.make_alpha()
        usage = [u for u in StorageManager.scan_usage(self.base)
                 if u.name == "alpha"][0]
        sizes = [s for _p, s in usage.largest_files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertLessEqual(len(usage.largest_files), 10)
        rels = [p for p, _s in usage.largest_files]
        self.assertEqual(rels[0], "Web/chall1/challenge/app.zip")

    def test_challenge_count(self):
        ws = self.make_alpha("counted")
        # Thêm chall thứ hai trong cùng category
        _truncate(os.path.join(ws, "Pwn", "chall2", "challenge", "bin"), 10)
        usage = [u for u in StorageManager.scan_usage(self.base)
                 if u.name == "counted"][0]
        self.assertEqual(usage.challenge_count, 2)

    def test_ended_from_event_window_mirror(self):
        ws = self.make_alpha("ended_ws")
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        _set_event_window(ws, int((end - dt.timedelta(days=2)).timestamp()),
                          int(end.timestamp()))
        ws_open = self.make_alpha("open_ws")
        future = int((dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(days=3)).timestamp())
        _set_event_window(ws_open, future - 86400, future)

        usages = {u.name: u for u in StorageManager.scan_usage(self.base)}
        self.assertIsNotNone(usages["ended_ws"].ended)
        delta = dt.datetime.now(dt.timezone.utc) - usages["ended_ws"].ended
        self.assertAlmostEqual(delta.days, 30, delta=1)
        self.assertIsNotNone(usages["open_ws"].ended)
        self.assertGreater(usages["open_ws"].ended,
                           dt.datetime.now(dt.timezone.utc))

    def test_no_challenges_json_means_ended_none(self):
        self.make_alpha("bare")
        usage = [u for u in StorageManager.scan_usage(self.base)
                 if u.name == "bare"][0]
        self.assertIsNone(usage.ended)

    def test_empty_or_missing_base(self):
        self.assertEqual(StorageManager.scan_usage(self.base), [])
        self.assertEqual(StorageManager.scan_usage(
            os.path.join(self.tmp, "nope")), [])


class TestFormatReport(StorageTestBase):
    def _usage(self, **kw):
        defaults = dict(
            name="ws", path="/tmp/ws", total_bytes=1024,
            breakdown={"attachments": 700, "writeups": 200,
                       "solvers": 100, "misc": 24},
            largest_files=[], challenge_count=1, ended=None,
        )
        defaults.update(kw)
        return WorkspaceUsage(**defaults)

    def test_sorted_desc_and_human_units(self):
        big = self._usage(name="big", total_bytes=5 * 1024 * 1024,
                          breakdown={"attachments": 5 * 1024 * 1024,
                                     "writeups": 0, "solvers": 0, "misc": 0})
        small = self._usage(name="small", total_bytes=512)
        report = StorageManager.format_report([small, big])
        lines = report.splitlines()
        idx_big = next(i for i, ln in enumerate(lines) if "big" in ln)
        idx_small = next(i for i, ln in enumerate(lines) if "small" in ln)
        self.assertLess(idx_big, idx_small)
        self.assertIn("5.0 MiB", report)
        self.assertIn("512 B", report)  # small workspace human-readable
        self.assertIn("TOTAL", report)

    def test_threshold_glyph_markers(self):
        # ≥2× ngưỡng → ✗ error; 1×–2× → ! warn; đã kết thúc → nhãn "ended".
        over2x = self._usage(name="over2x", total_bytes=2 * 1024 * 1024)
        mid = self._usage(
            name="mid", total_bytes=int(1.5 * 1024 * 1024))
        ended_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
        done = self._usage(name="done", ended=ended_at)
        report = StorageManager.format_report(
            [over2x, mid, done], threshold_mb=1)
        self.assertIn("✗", report, "thiếu glyph ✗ cho workspace ≥2× ngưỡng")
        self.assertIn("!", report, "thiếu glyph ! cho workspace 1-2× ngưỡng")
        self.assertIn("ended", report)

    def test_no_rainbow_on_sizes(self):
        """Số liệu neutral: KHÔNG markup success/warning/error/bold quanh size."""
        big = self._usage(name="big", total_bytes=5 * 1024 * 1024)
        report = StorageManager.format_report([big], threshold_mb=1)
        for tag in ("[success]", "[warning]", "[bold]",
                    "[green]", "[yellow]", "[red]", "[cyan]"):
            self.assertNotIn(tag, report)

    def test_usage_meter_ruby_only_at_or_over_threshold(self):
        # SPEC UI v2 §M1: cột USAGE-meter 8 ô — ramp RUBY (#E5534B→#FF2E63)
        # CHỈ khi ratio = total/threshold ≥1×; dưới ngưỡng vẫn amber.
        over = self._usage(name="over", total_bytes=2 * 1024 * 1024)    # 2×
        under = self._usage(name="under", total_bytes=512 * 1024)       # 0.5×
        report = StorageManager.format_report(
            [under, over], threshold_mb=1, tty=True)
        self.assertIn("USAGE", report)            # cột mới có mặt
        self.assertIn("▰", report)                # meter 8 ô có mặt
        self.assertIn("#ff2e63", report, "thiếu ruby FIRSTBLOOD cho ≥1× ngưỡng")
        self.assertIn("#ffb000", report, "workspace <1× ngưỡng phải amber")

    def test_usage_meter_amber_below_threshold_has_no_ruby(self):
        small = self._usage(name="small", total_bytes=256 * 1024)   # 0.25×
        report = StorageManager.format_report([small], threshold_mb=1, tty=True)
        self.assertNotIn("#ff2e63", report)
        self.assertNotIn("#e5534b", report)

    def test_usage_meter_plain_when_non_tty(self):
        """non-TTY → plain_meter: glyph ▰▱ không màu (ASCII-an-toàn pipe)."""
        report = StorageManager.format_report([self._usage()], threshold_mb=1)
        for hex_ in ("#6b4300", "#ffb000", "#ffe49a", "#e5534b", "#ff2e63"):
            self.assertNotIn(hex_, report)
        self.assertIn("▱", report)
        self.assertNotIn("[dim]", report)

    def test_human_size_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2.0 KiB")
        self.assertEqual(human_size(3 * 1024 * 1024), "3.0 MiB")
        self.assertEqual(human_size(2 * 1024 ** 3), "2.0 GiB")


class TestArchiveWorkspace(StorageTestBase):
    def _git(self, *args, cwd):
        return subprocess.run(["git"] + list(args), cwd=cwd,
                              capture_output=True, text=True, check=True)

    def _make_dirty_tree(self, ws):
        _write(os.path.join(ws, "Web", "c1", "challenge", "app.zip"), b"A" * 5000)
        _write(os.path.join(ws, "Web", "c1", "writeup", "README.md"),
               b"# writeup\n" * 20)
        _write(os.path.join(ws, "Web", "c1", "solver", "solve.py"), b"print(1)")
        # Rác phải bị exclude
        _write(os.path.join(ws, "Web", "c1", "__pycache__", "solve.cpython.pyc"),
               b"\x00" * 3000)
        _write(os.path.join(ws, ".pytest_cache", "CACHEDIR.TAG"), b"junk")
        _write(os.path.join(ws, "dl.bin.part"), b"partial")
        _write(os.path.join(ws, "scratch.tmp"), b"tmp")
        _write(os.path.join(ws, ".ctf", "watch_state.json"), b'{"t": 1}')
        os.makedirs(os.path.join(ws, ".git"), exist_ok=True)
        _write(os.path.join(ws, ".git", "HEAD"), b"ref: refs/heads/main\n")

    def test_archive_creates_tar_with_expected_name_and_content(self):
        ws = self.make_alpha("archme")
        out = os.path.join(self.tmp, "out")
        result = StorageManager.archive_workspace(ws, out)
        arc = result["archive_path"]
        self.assertTrue(os.path.isfile(arc))
        self.assertEqual(
            os.path.basename(arc),
            f"archme_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}"
            ".tar.gz")
        with tarfile.open(arc, "r:gz") as tf:
            names = set(tf.getnames())
        self.assertIn("Web/chall1/challenge/app.zip", names)

    def test_archive_excludes_runtime_junk(self):
        ws = os.path.join(self.base, "dirty")
        self._make_dirty_tree(ws)
        out = os.path.join(self.tmp, "out")
        result = StorageManager.archive_workspace(ws, out)
        with tarfile.open(result["archive_path"], "r:gz") as tf:
            names = tf.getnames()
        joined = "\n".join(names)
        for bad in ("__pycache__/", ".pytest_cache/", ".part", ".tmp",
                    ".ctf/watch_state.json", ".git/"):
            self.assertNotIn(bad, joined, f"exclude hỏng: thấy {bad}")
        for good in ("Web/c1/challenge/app.zip", "Web/c1/writeup/README.md",
                     "Web/c1/solver/solve.py"):
            self.assertIn(good, names)

    def test_original_bytes_counts_only_included_files(self):
        ws = os.path.join(self.base, "dirty")
        self._make_dirty_tree(ws)
        out = os.path.join(self.tmp, "out")
        result = StorageManager.archive_workspace(ws, out)
        included = (5000 + len(b"# writeup\n" * 20) + len(b"print(1)"))
        self.assertEqual(result["original_bytes"], included)
        self.assertGreater(result["archived_bytes"], 0)
        expected_ratio = round(result["archived_bytes"] / included, 4)
        self.assertEqual(result["ratio"], expected_ratio)

    def test_strip_patterns_extra_exclude(self):
        ws = self.make_alpha("stripme")
        out = os.path.join(self.tmp, "out")
        result = StorageManager.archive_workspace(
            ws, out, strip_patterns=["writeup/*"])
        with tarfile.open(result["archive_path"], "r:gz") as tf:
            names = tf.getnames()
        self.assertFalse(any("/writeup/" in n for n in names))
        self.assertTrue(any("/challenge/" in n for n in names))

    def test_strip_patterns_exclude_top_level_dir(self):
        """Pattern ``writeup/`` phải loại cả thư mục ``writeup/`` top-level
        (bug cũ: rel_dir có prefix ``./`` khi root==src nên không match)."""
        ws = os.path.join(self.base, "topstrip")
        _truncate(os.path.join(ws, "writeup", "README.md"), 300)
        _write(os.path.join(ws, "Web", "c1", "challenge", "app.zip"), b"A" * 100)
        out = os.path.join(self.tmp, "out")
        result = StorageManager.archive_workspace(
            ws, out, strip_patterns=["writeup/"])
        with tarfile.open(result["archive_path"], "r:gz") as tf:
            names = tf.getnames()
        self.assertFalse(any("writeup" in n for n in names),
                         f"writeup vẫn còn trong tar: {names}")
        self.assertIn("Web/c1/challenge/app.zip", names)
        self.assertEqual(result["original_bytes"], 100)

    def test_git_remote_push_to_local_bare_repo(self):
        ws = os.path.join(self.base, "gitty")
        self._make_dirty_tree(ws)
        out = os.path.join(self.tmp, "archive_repo")
        bare = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True)
        # Git an toàn trong môi trường test
        env_added = dict(
            GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="t@t",
        )
        old_env = {k: os.environ.get(k) for k in env_added}
        os.environ.update(env_added)
        try:
            result = StorageManager.archive_workspace(
                ws, out, git_remote=f"file://{bare}")
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.assertTrue(os.path.isfile(result["archive_path"]))
        # Repo local đã init + commit
        log = self._git("log", "--oneline", cwd=out).stdout
        self.assertTrue(log.strip(), "repo out_dir phải có commit")
        # Push thành công thật: bare repo nhận được commit
        bare_log = self._git("log", "--oneline", cwd=bare).stdout
        self.assertTrue(bare_log.strip(),
                        "bare remote phải nhận commit sau push")
        self.assertEqual(log.splitlines()[0].split()[1],
                         bare_log.splitlines()[0].split()[1])

    def test_nonexistent_workspace_raises(self):
        with self.assertRaises(StorageError):
            StorageManager.archive_workspace(
                os.path.join(self.tmp, "ghost"))


class TestDeleteWorkspace(StorageTestBase):
    def test_delete_renames_to_trash_keeps_data(self):
        ws = self.make_alpha("doomed")
        marker = os.path.join(ws, "notes.txt")
        new_path = StorageManager.delete_workspace(ws)

        self.assertFalse(os.path.exists(ws),
                         "workspace phải rời khỏi vị trí cũ")
        self.assertTrue(os.path.isdir(new_path))
        self.assertIn("_DELETED_", os.path.basename(new_path))
        self.assertTrue(os.path.isfile(marker.replace(ws, new_path)),
                        "dữ liệu phải nguyên vẹn sau rename")
        # Nằm trong _archives cạnh base
        self.assertIn("_archives", new_path)

    def test_delete_missing_raises(self):
        with self.assertRaises(StorageError):
            StorageManager.delete_workspace(os.path.join(self.base, "nope"))


class TestSuggestActions(StorageTestBase):
    def test_suggest_archive_old_ended_workspace(self):
        ws = self.make_alpha("oldctf")
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
        _set_event_window(ws, int((end - dt.timedelta(days=2)).timestamp()),
                          int(end.timestamp()))
        actions = StorageManager.suggest_actions(self.base)
        self.assertTrue(any("oldctf" in a and "archive" in a.lower()
                            for a in actions),
                        f"thiếu gợi ý archive oldctf: {actions}")

    def test_suggest_over_threshold(self):
        ws = self.make_beta("huge")
        _truncate(os.path.join(ws, "misc", "blob.bin"), 2 * 1024 * 1024)
        actions = StorageManager.suggest_actions(self.base, threshold_mb=1)
        self.assertTrue(any("'huge'" in a for a in actions))

    def test_recently_ended_not_suggested(self):
        ws = self.make_alpha("justended")
        end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        _set_event_window(ws, int((end - dt.timedelta(days=1)).timestamp()),
                          int(end.timestamp()))
        actions = StorageManager.suggest_actions(self.base)
        self.assertFalse(any("'justended'" in a for a in actions))

    def test_all_good_message_when_clean(self):
        self.make_beta("tiny")
        actions = StorageManager.suggest_actions(self.base)
        self.assertTrue(any(a.startswith("✔") for a in actions),
                        f"thiếu dòng all-good glyph ✔: {actions}")


class TestParseEventEnd(unittest.TestCase):
    def test_epoch_ms_gzctf_style(self):
        """epoch-ms (GZCTF/rCTF) phải parse đúng, không overflow → None."""
        from ctf_downloader.platforms.base import normalize_epoch_to_utc
        value = 1756000000000
        self.assertEqual(parse_event_end(value),
                         normalize_epoch_to_utc(value))
        expected = dt.datetime.fromtimestamp(
            1756000000, tz=dt.timezone.utc)
        self.assertEqual(parse_event_end(value), expected)

    def test_bool_is_none(self):
        """True/False là bool — không được thành epoch 1970."""
        for bad in (True, False):
            self.assertIsNone(parse_event_end(bad))

    def test_garbage_still_none(self):
        for bad in (None, "not-a-date", "", -5, object()):
            self.assertIsNone(parse_event_end(bad))

    def test_iso_and_epoch_seconds_unchanged(self):
        end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.assertEqual(
            parse_event_end(int(end.timestamp())), end)
        self.assertEqual(
            parse_event_end(end.strftime("%Y-%m-%dT%H:%M:%SZ")),
            end)


class TestExportWorkspace(unittest.TestCase):
    """P1-5: export_workspace zip strip-secrets."""

    REAL_FLAG = "PTITCTF{sup3r_s3cr3t}"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="storage_export_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = os.path.join(self.tmp, "CTF")
        self.ws = os.path.join(self.base, "gamma")
        self._make_ws(self.ws)

    def _make_ws(self, ws):
        _write(os.path.join(ws, "challenges.json"), json.dumps({
            "ctf_info": {
                "event_window": {"start": 1756000000, "end": 1756086400},
                "flag_format": r"^PTITCTF\{.+\}$",
            },
        }))
        # flag.txt phải bị loại khỏi zip
        _write(os.path.join(ws, "Web", "chall1", "challenge", "handout.zip"), b"PK")
        _write(os.path.join(ws, "Web", "chall1", "flag.txt"),
               f"FLAG={self.REAL_FLAG}\n")
        # README chứa flag thật (2 lần) + placeholder FLAG{...} giữ nguyên
        _write(os.path.join(ws, "Web", "chall1", "writeup", "README.md"),
               f"# Writeup\nFlag la {self.REAL_FLAG}\n"
               f"Lai la {self.REAL_FLAG} nua.\nPlaceholder FLAG{{...}} giu nguyen.\n")
        # metadata.json có submitted_flag -> bị xoá key
        _write(os.path.join(ws, "Web", "chall1", "metadata.json"), json.dumps({
            "name": "chall1",
            "submitted_flag": self.REAL_FLAG,
            "status": "solved",
        }))
        # submit_history.json -> bị loại nguyên file
        _write(os.path.join(ws, "Web", "chall1", "submit_history.json"), json.dumps(
            {"entries": [{"flag": self.REAL_FLAG, "correct": True}]}))
        # runtime junk -> excluded như archive
        _write(os.path.join(ws, "Web", "chall1", "__pycache__", "x.pyc"), b"junk")

    def _read_zip(self, zip_path):
        entries = {}
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                entries[name] = zf.read(name)
        return entries

    def test_strip_secrets_removes_all_traces(self):
        result = StorageManager.export_workspace(self.ws)
        self.assertEqual(
            set(result), {"zip_path", "files_count", "stripped_count"})
        entries = self._read_zip(result["zip_path"])

        # flag.txt + submit_history.json biến mất; junk excluded
        self.assertNotIn("Web/chall1/flag.txt", entries)
        self.assertNotIn("Web/chall1/submit_history.json", entries)
        self.assertNotIn("Web/chall1/__pycache__/x.pyc", entries)

        # README: không còn flag thật, có [REDACTED], placeholder giữ nguyên
        readme = entries["Web/chall1/writeup/README.md"].decode("utf-8")
        self.assertNotIn(self.REAL_FLAG, readme)
        self.assertEqual(readme.count("[REDACTED]"), 2)
        self.assertIn("FLAG{...}", readme)

        # metadata.json sạch submitted_flag, giữ key khác
        meta = json.loads(entries["Web/chall1/metadata.json"].decode("utf-8"))
        self.assertNotIn("submitted_flag", meta)
        self.assertEqual(meta["name"], "chall1")
        self.assertEqual(meta["status"], "solved")

        # Không leak flag ở bất kỳ entry nào còn lại
        blob = b"\n".join(entries.values())
        self.assertNotIn(self.REAL_FLAG.encode(), blob)

        # stripped_count: 2 (README) + 1 (metadata) + 1 (flag.txt) + 1 (history)
        self.assertEqual(result["stripped_count"], 5)
        # files_count khớp số entry thực tế trong zip
        self.assertEqual(result["files_count"], len(entries))

    def test_workspace_original_untouched(self):
        StorageManager.export_workspace(self.ws)
        self.assertEqual(
            open(os.path.join(self.ws, "Web", "chall1", "flag.txt")).read(),
            f"FLAG={self.REAL_FLAG}\n")
        readme = open(
            os.path.join(self.ws, "Web", "chall1", "writeup", "README.md")
        ).read()
        self.assertIn(self.REAL_FLAG, readme)
        meta = json.load(open(os.path.join(self.ws, "Web", "chall1", "metadata.json")))
        self.assertEqual(meta["submitted_flag"], self.REAL_FLAG)

    def test_strip_secrets_false_keeps_everything(self):
        result = StorageManager.export_workspace(self.ws, strip_secrets=False)
        self.assertEqual(result["stripped_count"], 0)
        entries = self._read_zip(result["zip_path"])
        self.assertIn("Web/chall1/flag.txt", entries)
        self.assertIn("Web/chall1/submit_history.json", entries)
        readme = entries["Web/chall1/writeup/README.md"].decode("utf-8")
        self.assertIn(self.REAL_FLAG, readme)
        self.assertNotIn("[REDACTED]", readme)
        meta = json.loads(entries["Web/chall1/metadata.json"].decode("utf-8"))
        self.assertEqual(meta["submitted_flag"], self.REAL_FLAG)

    def test_default_name_and_location_and_out_path_dir(self):
        result = StorageManager.export_workspace(self.ws)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        expected = os.path.join(
            self.base, "_archives", f"gamma_export_{stamp}.zip")
        self.assertEqual(result["zip_path"], expected)
        self.assertTrue(os.path.isfile(expected))

        out_dir = os.path.join(self.tmp, "elsewhere")
        os.makedirs(out_dir)
        result2 = StorageManager.export_workspace(self.ws, out_path=out_dir)
        self.assertEqual(os.path.dirname(result2["zip_path"]), out_dir)
        self.assertTrue(os.path.isfile(result2["zip_path"]))

    def test_flag_format_from_challenges_json_used(self):
        # Flag KHÔNG khớp regex generic (không có {...}) — chỉ bắt được nhờ
        # ctf_info.flag_format trong challenges.json.
        ws2 = os.path.join(self.base, "delta")
        _write(os.path.join(ws2, "challenges.json"), json.dumps(
            {"ctf_info": {"flag_format": r"SECRET_[a-z]+"}}))
        _write(os.path.join(ws2, "notes", "README.md"),
               "The flag is SECRET_hunter2 ok\n")
        result = StorageManager.export_workspace(ws2)
        with zipfile.ZipFile(result["zip_path"]) as zf:
            text = zf.read("notes/README.md").decode("utf-8")
        self.assertNotIn("SECRET_hunter2", text)
        self.assertIn("[REDACTED]", text)
        self.assertGreaterEqual(result["stripped_count"], 1)

    def test_missing_workspace_raises(self):
        with self.assertRaises(StorageError):
            StorageManager.export_workspace(
                os.path.join(self.base, "nope"))


class TestFormatReportWidthAndRule(unittest.TestCase):
    """codex-r2 P1: width bảng thống nhất với AppHeader (80), bỏ rule ngang
    full-width ngoài whitelist PHOSPHOR, khoảng thở sau heading."""

    def _usage(self, name, total_bytes=1024):
        return WorkspaceUsage(
            name=name, path=f"/tmp/{name}", total_bytes=total_bytes,
            breakdown={"attachments": 0, "writeups": 0,
                       "solvers": 0, "misc": total_bytes},
            largest_files=[], challenge_count=3, ended=None,
        )

    def test_report_width_never_exceeds_80(self):
        # Tên dài nhất thực tế từng đẩy bảng ra 86–90 cột.
        long_names = [
            "TallDwarf_Hosting_Hostile_Takeover",
            "2026_haruulzangi_CTF_Extended_Edition_X",
            "ws",
        ]
        usages = [self._usage(n, i * 1024 * 1024)
                  for i, n in enumerate(long_names)]
        report = StorageManager.format_report(usages)
        for ln in report.splitlines():
            visible = StorageManager._visible_len(ln)
            self.assertLessEqual(visible, 80,
                                 f"dòng vượt 80 cột ({visible}): {ln!r}")

    def test_long_name_truncated_with_ellipsis(self):
        report = StorageManager.format_report(
            [self._usage("TallDwarf_Hosting_Hostile_Takeover")])
        self.assertNotIn("TallDwarf_Hosting_Hostile_Takeover", report)
        self.assertIn("…", report)

    def test_no_full_width_rule_line(self):
        report = StorageManager.format_report([self._usage("alpha")])
        for ln in report.splitlines():
            self.assertFalse(set(ln) == {"-"},
                             f"còn đường ngang full-width: {ln!r}")
        self.assertNotIn("----", report)

    def test_breathing_room_after_heading(self):
        report = StorageManager.format_report([self._usage("alpha")])
        lines = report.splitlines()
        self.assertTrue(lines[0].startswith("["))
        # dòng thứ hai là dòng trống (khoảng thở) trước header bảng
        self.assertEqual(lines[1], "")

    def test_columns_still_aligned_after_squeeze(self):
        import re as _re
        usages = [self._usage("TallDwarf_Hosting_Hostile_Takeover",
                              total_bytes=3 * 1024 * 1024),
                  self._usage("tiny", total_bytes=512)]
        report = StorageManager.format_report(usages)
        # mọi dòng số liệu có cùng vị trí đơn vị TOTAL (right-align giữ
        # nguyên) — so trên text đã bỏ markup rich
        def _plain(ln):
            return _re.sub(r"\[[^\]]*\]", "", ln)
        totals = {_plain(ln).find(" MiB") for ln in report.splitlines()
                  if " MiB" in _plain(ln) and "ngưỡng" not in ln}
        self.assertEqual(len(totals), 1)


if __name__ == "__main__":
    unittest.main()