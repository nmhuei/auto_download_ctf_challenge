"""Open-code batch-3 (DEFERRED_TRIAGE §2 — 2 mục cuối còn lại).

Mục 1 — Hoard ``isdigit()`` route nhầm: target toàn số bị coi là id TRƯỚC
khi thử name-lookup nên challenge tên "1337" không bao giờ tra được theo
tên. Sau fix: ưu tiên tra theo TÊN trước (case-insensitive như cache của
SubmitService), chỉ fallback id khi không có challenge nào mang tên đó;
``--id`` tường minh luôn thắng.

Mục 2 — Auth-key helper chung: quy ước key auth-map (workspace dir thật ->
abs path, ngược lại -> URL) định nghĩa MỘT NƠI ở ``auth_service.auth_key``;
``RegisterService._auth_key`` chỉ delegate. Read-side giữ compat CẢ HAI
quy ước key cũ qua ``AuthService.lookup_auth_entry`` (không migration dữ
liệu user).
"""
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------- #
# Mục 1 — hoard route: tên toàn số ưu tiên trước id
# ---------------------------------------------------------------------- #
def _make_numeric_name_workspace(root):
    """Workspace 2 challenge: "Baby Web" (id 1) và "1337" (id 42)."""
    import pathlib

    root = pathlib.Path(root)
    (root / "Web" / "baby_web").mkdir(parents=True, exist_ok=True)
    (root / "Misc" / "leet").mkdir(parents=True, exist_ok=True)
    chals = [
        {"id": 1, "name": "Baby Web", "category": "Web", "points": 100},
        {"id": 42, "name": "1337", "category": "Misc", "points": 1337},
    ]
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "NumCTF", "url": "https://num.example.com",
                     "platform": "ctfd"},
        "challenges": chals,
    }), encoding="utf-8")
    for c, slug in ((chals[0], "baby_web"), (chals[1], "leet")):
        (root / c["category"] / slug / "metadata.json").write_text(
            json.dumps(c), encoding="utf-8")
    return root


class TestHoardNumericNameRouting(unittest.TestCase):
    """``ctf hoard <target> <FLAG>`` — route target đúng khi tên toàn số."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="b3_hoard_")
        self.root = _make_numeric_name_workspace(self._tmp)
        self._patches = [
            patch("ctf_downloader.services.submit_service.create_session"),
            patch("ctf_downloader.services.submit_service."
                  "PlatformDetector.detect_platform",
                  return_value=MagicMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _parse(self, argv):
        from ctf_downloader.cli import build_unified_parser
        return build_unified_parser().parse_args(argv)

    def _run(self, argv):
        from ctf_downloader.cli_commands import handle_hoard
        handle_hoard(self._parse(argv + ["-w", str(self.root)]))

    def _status_of(self, rel):
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo
        meta = os.path.join(str(self.root), rel, "metadata.json")
        return WorkspaceRepo(str(self.root)).read_status(meta)

    def test_numeric_named_challenge_resolves_by_name(self):
        # "1337" là TÊN của challenge id 42 — phải hoard vào ĐÚNG bài đó,
        # không bị isdigit() route nhầm sang id 1337 rồi báo không tìm thấy.
        self._run(["hoard", "1337", "FLAG{name}"])
        st = self._status_of(os.path.join("Misc", "leet"))
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{name}")
        self.assertEqual(st["solve"], "working")
        # Challenge kia không bị đụng tới.
        untouched = self._status_of(os.path.join("Web", "baby_web"))
        self.assertNotEqual(untouched["flag"].get("state"), "hoarded")

    def test_digit_target_without_name_match_still_uses_id(self):
        # Target "1" trùng key id (không có challenge nào tên "1") → giữ
        # nguyên hành vi cũ: tra theo id.
        self._run(["hoard", "1", "FLAG{byid}"])
        st = self._status_of(os.path.join("Web", "baby_web"))
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{byid}")

    def test_explicit_id_flag_wins_over_numeric_name_collision(self):
        # --id tường minh: dù target "1337" khớp tên challenge id 42,
        # người dùng chỉ định id 1 → phải ghi vào id 1.
        self._run(["hoard", "1337", "--id", "1", "-f", "FLAG{explicit}"])
        st = self._status_of(os.path.join("Web", "baby_web"))
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{explicit}")
        leet = self._status_of(os.path.join("Misc", "leet"))
        self.assertNotEqual(leet["flag"].get("state"), "hoarded")

    def test_explicit_name_flag_with_digits_resolves_by_name(self):
        # -n "1337" cũng phải tra theo tên (cùng quy ước với target vị trí).
        self._run(["hoard", "-n", "1337", "-f", "FLAG{byname}"])
        st = self._status_of(os.path.join("Misc", "leet"))
        self.assertEqual(st["flag"]["state"], "hoarded")
        self.assertEqual(st["flag"]["value"], "FLAG{byname}")


# ---------------------------------------------------------------------- #
# Mục 2 — helper key auth dùng chung register/auth
# ---------------------------------------------------------------------- #
class TestSharedAuthKeyHelper(unittest.TestCase):
    """Quy ước key auth-map đơn nguồn: auth_service.auth_key."""

    # ---- write-side: quy tắc chuẩn ----
    def test_auth_key_rule_matrix(self):
        from ctf_downloader.services.auth_service import auth_key

        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "real_ws")
            os.mkdir(ws)
            # Workspace là dir thật -> abs path, bất kể url.
            self.assertEqual(auth_key(ws, "https://x.com"),
                             os.path.abspath(ws))
            # Workspace ảo/mất -> URL, bỏ '/' cuối.
            self.assertEqual(auth_key(os.path.join(d, "ghost"),
                                     "https://x.com/"), "https://x.com")
            self.assertEqual(auth_key(None, "https://x.com"), "https://x.com")
            # Không đủ dữ liệu -> None (caller tự xử lý).
            self.assertIsNone(auth_key(None, None))

    def test_register_auth_key_delegates_to_shared_helper(self):
        # Drift guard: delegate của register phải ra ĐÚNG kết quả helper chung
        # trên mọi nhánh quy ước.
        from ctf_downloader.services.auth_service import auth_key
        from ctf_downloader.services.register_service import RegisterService

        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "ws_dir")
            os.mkdir(ws)
            cases = [
                (ws, "https://x.com"),
                (os.path.join(d, "missing"), "https://x.com/"),
                (None, "https://x.com"),
            ]
            for workspace, url in cases:
                self.assertEqual(
                    RegisterService._auth_key(workspace, url),
                    auth_key(workspace, url),
                    f"drift quy ước key tại workspace={workspace!r}")

    # ---- read-side: compat CẢ HAI quy ước key cũ ----
    def _resolve_with_auth(self, auth, workspace, **kw):
        from ctf_downloader.services import auth_service

        with patch.object(auth_service, "load_global_config",
                          lambda: {"auth": auth}):
            return auth_service.AuthService.resolve(workspace, **kw)

    def test_resolve_reads_workspace_key_even_if_dir_vanished(self):
        # Quy ước 1: entry ghi dưới key abs-workspace KHI DIR CÒN TỒN TẠI —
        # dir xoá sau đó vẫn phải đọc được (probe vô điều kiện, không migration).
        from ctf_downloader.services.auth_service import auth_key

        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "gone_ws")
            os.mkdir(ws)
            key = auth_key(ws, "https://c.com")       # abs path khi dir còn
            shutil.rmtree(ws)
            cookie, token = self._resolve_with_auth(
                {key: {"cookie": "OLD_WS_COOKIE", "token": "tk"}}, ws)
        self.assertEqual(cookie, "OLD_WS_COOKIE")
        self.assertEqual(token, "tk")

    def test_resolve_falls_back_to_url_keyed_convention(self):
        # Quy ước 2: entry do register lưu khi --workspace không phải dir
        # thật (key = URL) — workspace dạng URL có dấu '/' cuối vẫn khớp.
        cookie, token = self._resolve_with_auth(
            {"https://ctfB.com": {"cookie": "URL_KEYED", "token": None}},
            "https://ctfB.com/")
        self.assertEqual(cookie, "URL_KEYED")
        self.assertIsNone(token)


if __name__ == "__main__":
    unittest.main()
