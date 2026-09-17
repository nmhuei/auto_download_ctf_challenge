"""P1-6 — Tests cho Notes + Tags trên challenge (memory của người chơi CTF).

Phạm vi:
  - set/remove note (inline content + xoá) qua StatusService.set_note
  - tag add/remove + validate reject ("Hard!" sai định dạng [a-z0-9-] ≤24)
  - status filter --label (AND) và --search (tên + note)
  - resolve ambiguous: partial match nhiều kết quả → liệt kê, KHÔNG chọn âm thầm

Mock workspace tmp — không đụng network.
"""
import contextlib
import io
import json
import pathlib
import shutil
import tempfile
import unittest

from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.services.status_service import (
    AmbiguousChallengeError,
    ChallengeNotFoundError,
    StatusService,
)


def _make_workspace(d: str) -> pathlib.Path:
    """Workspace giả lập: 3 challenge Web trùng tiền tố tên để test ambiguous."""
    root = pathlib.Path(d) / "ws_tags_notes"
    for slug in ("ssti_playground", "ssti_advanced", "pwn_intro"):
        (root / "Web" / slug).mkdir(parents=True, exist_ok=True)

    chals = [
        {"id": 1, "name": "SSTI Playground", "category": "Web", "points": 100},
        {"id": 2, "name": "SSTI Advanced", "category": "Web", "points": 200},
        {"id": 3, "name": "Pwn Intro", "category": "Pwn", "points": 150},
    ]
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "TagsCTF", "url": "https://tags.example.com",
                     "platform": "gzctf"},
        "challenges": chals,
    }), encoding="utf-8")
    slugs = ["ssti_playground", "ssti_advanced", "pwn_intro"]
    for c, slug in zip(chals, slugs):
        folder = root / "Web" / slug
        (folder / "metadata.json").write_text(json.dumps(c), encoding="utf-8")
    return root


class TagsNotesCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="p16_tags_")
        self.root = _make_workspace(self._tmp)
        self.repo = WorkspaceRepo(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _meta_path(self, name: str) -> pathlib.Path:
        return self.root / "Web" / name / "metadata.json"

    def _status(self, name: str) -> dict:
        return self.repo.read_status(self._meta_path(name))

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------
    def test_set_note_inline_content(self):
        ok = StatusService.set_note(self.repo, "SSTI Playground",
                                    text="Đã thử SSTI, bị chặn WAF {{.")
        self.assertTrue(ok)
        self.assertEqual(self._status("ssti_playground").get("notes"),
                         "Đã thử SSTI, bị chặn WAF {{.")

    def test_set_note_by_id_and_partial(self):
        # exact id
        self.assertTrue(StatusService.set_note(self.repo, "2", text="by id"))
        self.assertEqual(self._status("ssti_advanced").get("notes"), "by id")
        # substring khớp đúng 1 ("pwn intro" -> Pwn Intro)
        self.assertTrue(StatusService.set_note(self.repo, "pwn intro", text="ret2libc"))
        self.assertEqual(self._status("pwn_intro").get("notes"), "ret2libc")

    def test_remove_note(self):
        StatusService.set_note(self.repo, "1", text="temp note")
        self.assertTrue(self._status("ssti_playground").get("notes"))
        ok = StatusService.set_note(self.repo, "1", remove=True)
        self.assertTrue(ok)
        self.assertEqual(self._status("ssti_playground").get("notes"), "")

    def test_note_not_found(self):
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err), \
             contextlib.redirect_stdout(buf_err):
            ok = StatusService.set_note(self.repo, "no_such_chall", text="x")
        self.assertFalse(ok)
        self.assertNotEqual(self._status("ssti_playground").get("notes"), "x")

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    def test_tag_add(self):
        ok, rejected = StatusService.update_tags(
            self.repo, "SSTI Playground", ["hard", "todo"])
        self.assertTrue(ok)
        self.assertEqual(rejected, [])
        labels = self._status("ssti_playground").get("labels")
        self.assertEqual(labels, ["hard", "todo"])

    def test_tag_add_uppercase_normalized_no_duplicate(self):
        ok, _ = StatusService.update_tags(self.repo, "1", ["Hard", "HARD"])
        self.assertTrue(ok)
        self.assertEqual(self._status("ssti_playground").get("labels"), ["hard"])

    def test_tag_remove(self):
        StatusService.update_tags(self.repo, "1", ["hard", "todo"])
        ok, _ = StatusService.update_tags(self.repo, "1", ["todo"], remove=True)
        self.assertTrue(ok)
        self.assertEqual(self._status("ssti_playground").get("labels"), ["hard"])

    def test_tag_validate_reject_hard_bang(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ok, rejected = StatusService.update_tags(
                self.repo, "1", ["ok-tag", "Hard!"])
        self.assertFalse(ok)
        self.assertIn("Hard!", rejected)
        # Bị từ chối toàn bộ → KHÔNG ghi nửa vời tag hợp lệ.
        self.assertEqual(self._status("ssti_playground").get("labels"), [])

    def test_tag_validate_reject_too_long_and_empty(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ok, rejected = StatusService.update_tags(self.repo, "1", ["a" * 25])
        self.assertFalse(ok)
        self.assertEqual(rejected, ["a" * 25])
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ok, rejected = StatusService.update_tags(self.repo, "1", ["   "])
        self.assertFalse(ok)

    # ------------------------------------------------------------------ #
    # Filter --label (AND) & --search
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_capture(repo, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            StatusService.render_tree(repo, **kw)
        return buf.getvalue()

    def _tag_fixture(self):
        StatusService.set_note(self.repo, "1", text="đã thử SSTI, bị chặn")
        StatusService.update_tags(self.repo, "1", ["hard", "todo"])
        StatusService.update_tags(self.repo, "3", ["hard"])

    def test_filter_label_and(self):
        self._tag_fixture()
        out = self._render_capture(self.repo, filter_labels=["hard"])
        self.assertIn("SSTI Playground", out)
        self.assertIn("Pwn Intro", out)
        self.assertNotIn("SSTI Advanced", out)

        # AND: chỉ challenge mang CẢ hard + todo
        out_and = self._render_capture(self.repo, filter_labels=["hard", "todo"])
        self.assertIn("SSTI Playground", out_and)
        self.assertNotIn("Pwn Intro", out_and)
        self.assertNotIn("SSTI Advanced", out_and)

    def test_filter_search_name_and_notes(self):
        self._tag_fixture()
        # search theo note
        out = self._render_capture(self.repo, search="bị chặn")
        self.assertIn("SSTI Playground", out)
        self.assertNotIn("Pwn Intro", out)
        self.assertNotIn("SSTI Advanced", out)
        # search theo tên
        out_name = self._render_capture(self.repo, search="pwn")
        self.assertIn("Pwn Intro", out_name)
        self.assertNotIn("SSTI Playground", out_name)

    def test_render_labels_and_note_icons(self):
        self._tag_fixture()
        out = self._render_capture(self.repo)
        self.assertIn("#hard,todo", out)
        # PHOSPHOR redesign: labels → "#tag" muted sau tên; note là cột cuối
        # của bảng challenge (vẫn giữ ngoặc kép).
        self.assertIn('"đã thử SSTI, bị chặn"', out)

    def test_render_without_notes_labels_unchanged_lines(self):
        # Không note/label → không in dòng 📝/🏷️ nào (smoke output giữ nguyên).
        out = self._render_capture(self.repo)
        self.assertNotIn("📝 \"", out)
        self.assertNotIn("🏷️ ", out)

    # ------------------------------------------------------------------ #
    # Resolve ambiguous
    # ------------------------------------------------------------------ #
    def test_resolve_ambiguous_lists_matches(self):
        matches = []
        try:
            StatusService.resolve_challenge(self.repo, "ssti")  # khớp 2 challenge
            self.fail("Expected AmbiguousChallengeError")
        except AmbiguousChallengeError as e:
            matches = e.matches
        self.assertEqual(len(matches), 2)
        names = {m.get("name") for m in matches}
        self.assertEqual(names, {"SSTI Playground", "SSTI Advanced"})

    def test_resolve_not_found_raises(self):
        with self.assertRaises(ChallengeNotFoundError):
            StatusService.resolve_challenge(self.repo, "zzz_none")

    def test_update_tags_on_ambiguous_does_not_write(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            ok, _ = StatusService.update_tags(self.repo, "ssti", ["hard"])
        self.assertFalse(ok)
        self.assertEqual(self._status("ssti_playground").get("labels"), [])
        self.assertEqual(self._status("ssti_advanced").get("labels"), [])


if __name__ == "__main__":
    unittest.main()
