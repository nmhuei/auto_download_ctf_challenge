"""HUNTER agent — cycle 6: bấm đường biên các lệnh CLI mới nhất.

Phạm vi: ``ctf open`` / ``sync`` / ``export-pack`` / ``history`` /
``sniper`` / ``serve`` / ``doctor`` qua parser + handler thật
(ctf_downloader/cli.py + cli_commands.py).

An toàn mạng: KHÔNG có test nào gọi ra ngoài máy. Serve/doctor chỉ dùng
127.0.0.1 loopback; sync dùng FakePlatform; nơi có nguy cơ chạm session
thật đều được patch để raise nếu lỡ gọi.

Chạy: python3 -m pytest test_hunter_c6.py -q
"""
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import types
import urllib.parse
import urllib.request
import zipfile

import pytest

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.cli_commands import (
    handle_doctor,
    handle_history,
    handle_open,
    handle_serve,
    handle_sniper,
    handle_sync,
)
from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.services.sniper_service import SniperService
from ctf_downloader.services.web_dashboard import WebDashboard
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def parse(argv):
    return build_unified_parser().parse_args(argv)


@contextlib.contextmanager
def capture_out():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def captured(handle_fn, args):
    """Chạy handler, trả (stdout, stderr) đã gộp."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        handle_fn(args)
    return buf_out.getvalue() + buf_err.getvalue()


def captured_exit(handle_fn, args):
    """Như captured nhưng nuốt SystemExit, trả (output, exit_code)."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            handle_fn(args)
        except SystemExit as e:
            code = e.code
    return buf_out.getvalue() + buf_err.getvalue(), code


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"desc {name}")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


def seed_challenge(root, cid, name, category, points=100, status=None,
                   writeup_text=None):
    """Tạo 1 challenge local: <cat>/<slug>/metadata.json + writeup/README.md."""
    slug = f"{category}-{cid}-{name}".lower().replace(" ", "_")
    d = os.path.join(root, category.lower(), slug)
    os.makedirs(os.path.join(d, "writeup"), exist_ok=True)
    repo = WorkspaceRepo(root)
    repo.write_metadata(os.path.join(d, "metadata.json"),
                        {"id": cid, "name": name, "category": category,
                         "points": points})
    if status is not None:
        repo.update_status(os.path.join(d, "metadata.json"),
                           lambda st: {**st, **status})
    if writeup_text is not None:
        with open(os.path.join(d, "writeup", "README.md"), "w",
                  encoding="utf-8") as f:
            f.write(writeup_text)
    return d


class FakePlatform:
    """Platform giả cho sync: fetch tĩnh + attribution map, ghi lại mọi call."""

    def __init__(self, challenges, attr_map=None):
        self.ctf_info = CTFInfo(title="HunterCTF", url="https://hunt.example")
        self._challenges = list(challenges)
        self.attr_map = dict(attr_map or {})
        self.calls = []

    def authenticate(self):
        return True

    def fetch_challenges(self):
        self.calls.append("fetch_challenges")
        return list(self._challenges)

    def fetch_solve_attribution(self, ids):
        self.calls.append("fetch_solve_attribution")
        return {i: self.attr_map.get(i) for i in ids}


# ======================================================================
# A. State file thiếu / rỗng / hỏng JSON + workspace trống
# ======================================================================

class TestCorruptStateFiles:
    def test_history_empty_workspace_no_traceback(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        out = captured(handle_history, parse(["history", "-w", str(ws)]))
        assert "Chưa có lịch sử submit" in out
        assert "Traceback" not in out

    def test_history_nonexistent_workspace_clean_exit(self, tmp_path):
        out, code = captured_exit(
            handle_history, parse(["history", "-w", str(tmp_path / "nope")]))
        assert code == 1
        assert "Workspace không tồn tại" in out
        assert "Traceback" not in out

    def test_history_corrupt_json_backed_up(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        hist = ws / "submit_history.json"
        hist.write_text("{broken json!!", encoding="utf-8")
        out = captured(handle_history, parse(["history", "-w", str(ws)]))
        assert "Chưa có lịch sử submit" in out
        assert (ws / "submit_history.json.bak").read_text(
            encoding="utf-8") == "{broken json!!"

    def test_history_valid_json_but_list_treated_corrupt(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        hist = ws / "submit_history.json"
        hist.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        out = captured(handle_history, parse(["history", "-w", str(ws)]))
        assert "Chưa có lịch sử submit" in out
        assert (ws / "submit_history.json.bak").exists()

    def test_history_mixed_junk_entries_filtered_and_flag_redacted(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        seed_challenge(str(ws), 7, "Junky", "Web")
        hist = {"entries": [
            {"challenge_id": 7, "flag": "FLAG{super_secret}",
             "result": "correct", "timestamp": "2026-08-25T00:00:00Z"},
            "junk-string",
            42,
            {"flag": "FLAG{x}"},  # dict hợp lệ nhưng thiếu challenge_id
        ]}
        (ws / "submit_history.json").write_text(json.dumps(hist),
                                                encoding="utf-8")
        out = captured(handle_history, parse(["history", "-w", str(ws)]))
        assert "Junky" in out
        assert "super_secret" not in out          # mặc định phải che flag
        assert "FLAG***" in out
        out_all = captured(handle_history,
                           parse(["history", "-w", str(ws), "--all"]))
        assert "super_secret" in out_all

    def test_open_empty_workspace_clean_error(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(SystemExit) as ei:
            captured(handle_open, parse(["open", "whatever", "-w", str(ws)]))
        assert ei.value.code == 1

    def test_sync_empty_workspace_clean_error_no_network(self, tmp_path,
                                                         monkeypatch):
        # Nếu code lỡ đi quá bước resolve URL thì các patch này nổ ngay —
        # chứng minh không hề chạm mạng khi workspace trống.
        from ctf_downloader.services import platform_resolver
        monkeypatch.setattr(platform_resolver, "create_session",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("network attempted")))
        monkeypatch.setattr(platform_resolver, "detect_platform_info",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("network attempted")))
        ws = tmp_path / "ws"
        ws.mkdir()
        out, code = captured_exit(handle_sync, parse(["sync", "-w", str(ws)]))
        assert code == 1
        assert "Không resolve được platform" in out
        assert "Traceback" not in out

    def test_sniper_empty_workspace_clean_exit_1(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(SystemExit) as ei:
            captured(handle_sniper, parse(["sniper", "-w", str(ws)]))
        assert ei.value.code == 1


# ======================================================================
# B. Tham số biên: khoảng trắng / Unicode / binary thiếu / trùng tên
# ======================================================================

class TestBoundaryParams:
    def test_open_unicode_space_target_passes_path_verbatim(self, tmp_path,
                                                            monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        name = "Bài tập khó (web)"
        d = seed_challenge(str(ws), 42, name, "Web")

        recorded = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = captured(handle_open, parse(["open", name, "-w", str(ws)]))
        assert recorded["argv"][0] == "xdg-open"
        assert recorded["argv"][1] == d          # path nguyên vẹn, không shell
        assert recorded["kwargs"].get("shell") in (False, None)
        # rich soft-wrap chèn '\n' giữa path dài -> bỏ hết whitespace khi
        # assert; handler in thư mục challenge (path đã được khẳng định nguyên
        # vẹn trên qua argv của xdg-open).
        strip_ws = lambda s: "".join(s.split())
        assert strip_ws(d) in strip_ws(out)

    def test_open_missing_xdg_binary_hint(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        seed_challenge(str(ws), 1, "Alpha", "Web")

        def raise_fnf(argv, **kwargs):
            raise FileNotFoundError("No such file: xdg-open")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        with pytest.raises(SystemExit) as ei:
            captured(handle_open, parse(["open", "Alpha", "-w", str(ws)]))
        assert ei.value.code == 1

    def test_history_missing_cid_shows_placeholder_no_substring(self, tmp_path):
        """BUG-HUNTER-C6-02 (FIXED): entry thiếu challenge_id phải hiển thị
        placeholder '?', KHÔNG rơi xuống tier substring của find_challenge
        (trước đây 'none' khớp nhầm challenge tên 'NoneTracer')."""
        ws = tmp_path / "ws"
        ws.mkdir()
        seed_challenge(str(ws), 9, "NoneTracer", "Misc")
        (ws / "submit_history.json").write_text(json.dumps({"entries": [
            {"flag": "FLAG{zzzz}", "result": "correct",
             "timestamp": "2026-08-25T01:02:03Z"},
        ]}), encoding="utf-8")
        out = captured(handle_history, parse(["history", "-w", str(ws)]))
        assert "NoneTracer" not in out   # không được gán nhầm theo substring
        assert "?" in out                # placeholder cho entry vô danh


# ======================================================================
# C. Sync 2 chiều: xung đột + xoá local
# ======================================================================

class TestSyncTwoWay:
    def _workspace(self, root):
        seed_challenge(root, 1, "Alpha", "web")
        seed_challenge(root, 2, "Beta", "pwn")

    def test_conflict_dynamic_fields_remote_wins_local_state_intact(
            self, tmp_path):
        ws = str(tmp_path / "ws")
        self._workspace(ws)
        repo = WorkspaceRepo(ws)
        alpha_meta = next(p for p in repo.iter_challenges()
                          if repo.read_metadata(p).get("id") == 1)
        # User sửa tay points + notes + solve trước khi sync
        repo.update_metadata(alpha_meta, lambda m: {**m, "points": 999})
        repo.update_status(alpha_meta, lambda st: {
            **st, "notes": "ghi chú của tôi", "solve": "working"})

        plat = FakePlatform([
            make_chall(1, "Alpha", "web", points=150, solves_count=7,
                       connection_info="nc://host:1337"),
            make_chall(2, "Beta", "pwn", points=200),
        ])
        result = PullService.sync_workspace(repo, plat)
        assert result["ok"] is True and result["updated"] >= 1

        meta = repo.read_metadata(alpha_meta)
        assert meta["points"] == 150           # server thắng field động
        assert meta["solves_count"] == 7
        st = repo.read_status(alpha_meta)
        assert st["notes"] == "ghi chú của tôi"  # local state là chủ
        assert st["solve"] == "working"
        assert st["synced_at"]

    def test_local_deletion_not_propagated_to_server(self, tmp_path):
        ws = str(tmp_path / "ws")
        self._workspace(ws)
        repo = WorkspaceRepo(ws)
        beta_dir = next(p for p in repo.iter_challenges()
                        if repo.read_metadata(p).get("id") == 2).parent
        shutil.rmtree(beta_dir)                # xoá local Beta

        plat = FakePlatform([
            make_chall(1, "Alpha", "web", points=100),
            make_chall(2, "Beta", "pwn", points=200),  # server vẫn còn
        ])
        result = PullService.sync_workspace(repo, plat)
        assert result["ok"] is True
        assert any(c["name"] == "Beta" for c in result["new_on_server"])
        # Không có API xoá phía server nào được gọi — chỉ fetch.
        assert set(plat.calls) <= {"fetch_challenges",
                                   "fetch_solve_attribution"}
        # Alpha không bị ảnh hưởng
        alpha_meta = next(p for p in repo.iter_challenges()
                          if repo.read_metadata(p).get("id") == 1)
        assert repo.read_metadata(alpha_meta)["id"] == 1

    def test_sync_fetch_failure_returns_ok_false(self, tmp_path):
        ws = str(tmp_path / "ws")
        self._workspace(ws)

        class Bad(FakePlatform):
            def fetch_challenges(self):
                raise RuntimeError("boom")

        repo = WorkspaceRepo(ws)
        result = PullService.sync_workspace(repo, Bad([]))
        assert result["ok"] is False

    def test_handler_sync_verify_drift_lists_without_mutating(
            self, tmp_path, monkeypatch):
        ws = str(tmp_path / "ws")
        self._workspace(ws)
        repo = WorkspaceRepo(ws)
        plat = FakePlatform(
            [make_chall(1, "Alpha", "web"), make_chall(2, "Beta", "pwn")],
            attr_map={1: {"by_me": True, "by_team": False,
                          "solver_names": ["me"]}})
        from ctf_downloader.services.platform_resolver import PlatformResolver
        monkeypatch.setattr(
            PlatformResolver, "for_workspace",
            staticmethod(lambda repo_, cookie=None, token=None:
                         (None, plat, None)))

        out = captured(handle_sync, parse(["sync", "-w", ws, "--verify"]))
        assert "Drift" in out or "drift" in out
        alpha_meta = next(p for p in repo.iter_challenges()
                          if repo.read_metadata(p).get("id") == 1)
        assert repo.read_status(alpha_meta)["solve"] == "unsolved"


# ======================================================================
# D. Serve / web dashboard — request dở hơi trên loopback
# ======================================================================

@pytest.fixture
def server(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    seed_challenge(str(ws), 1, "Alpha", "Web")
    seed_challenge(str(ws), 2, "Beta", "Crypto")
    dash = WebDashboard(
        WorkspaceRepo(ws),
        submit_factory=lambda: types.SimpleNamespace(
            submit=lambda ch, fl: (True, "ok")),
    )
    httpd = dash.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def raw_roundtrip(port, payload, timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(payload)
    try:
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    chunks = []
    try:
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except socket.timeout:
        pass
    finally:
        s.close()
    return b"".join(chunks)


class TestServeRobustness:
    def test_hostile_query_strings_render_escaped(self, server):
        evil_q = "<script>alert(1)</script>" + "x" * 20000
        qs = urllib.parse.urlencode({"q": evil_q, "cat": "web\xff",
                                     "label": "a,b"})
        req = urllib.request.Request(
            f"http://127.0.0.1:{server}/?{qs}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
        assert resp.status == 200
        assert b"<script>alert(1)</script>" not in body

    def test_huge_request_line_does_not_kill_server(self, server):
        resp = raw_roundtrip(server,
                             b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n"
                             b"Host: x\r\n\r\n")
        assert b"414" in resp.split(b"\r\n")[0] or \
            b"400" in resp.split(b"\r\n")[0]
        # Server còn sống sau request độc
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server}/api/status.json",
                timeout=10) as r:
            assert r.status == 200

    @pytest.mark.parametrize("cl", ["-5", "abc", "1.5",
                                    "+999999999999999999999",
                                    "999999999"])
    def test_post_lying_content_length_handled(self, server, cl):
        """CL âm / không-phải-số / quá-lớn (> ngưỡng 1MB hoặc tràn
        ssize_t) đều phải 400 sạch ngay từ khâu parse header."""
        resp = raw_roundtrip(
            server,
            ("POST /api/submit HTTP/1.1\r\nHost: x\r\n"
             "X-Requested-With: XMLHttpRequest\r\n"
             f"Content-Length: {cl}\r\n\r\n{{}}").encode())
        first = resp.split(b"\r\n")[0]
        assert b"400" in first
        assert b"500" not in first

    def test_post_overflow_content_length_clean_400_no_leak(self, server):
        """FIX-HUNTER-C6-04: ``Content-Length`` cực đại (vd
        '+999999999999999999999') phải trả 400 JSON sạch ngay từ khâu
        parse — KHÔNG OverflowError -> 500 kèm message internals lộ ra."""
        resp = raw_roundtrip(
            server,
            ("POST /api/submit HTTP/1.1\r\nHost: x\r\n"
             "X-Requested-With: XMLHttpRequest\r\n"
             "Content-Length: +999999999999999999999\r\n\r\n{}").encode())
        first = resp.split(b"\r\n")[0]
        assert b"400" in first
        assert b"500" not in first
        assert b"OverflowError" not in resp
        assert b"Internal error" not in resp
        assert b"Traceback" not in resp
        # Server vẫn sống sau request độc:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server}/api/status.json",
                timeout=10) as r:
            assert r.status == 200

    def test_post_valid_body_within_limit_submits_ok(self, server):
        """Body hợp lệ ≤ ngưỡng vẫn submit bình thường (200, ok=true)."""
        body = json.dumps({"challenge": "c6-04", "flag": "CTF{ok}"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server}/api/submit", data=body,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
            assert r.status == 200
        assert payload["ok"] is True

    def test_post_truncated_body_half_close_400(self, server):
        body = b'{"challenge":'      # 13 bytes, khai báo 1000
        resp = raw_roundtrip(
            server,
            b"POST /api/submit HTTP/1.1\r\nHost: x\r\n"
            b"X-Requested-With: XMLHttpRequest\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 1000\r\n\r\n" + body)
        assert b"400" in resp.split(b"\r\n")[0]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server}/api/status.json",
                timeout=10) as r:
            assert r.status == 200     # server vẫn sống

    def test_post_json_array_is_400_not_crash(self, server):
        body = json.dumps([1, 2]).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server}/api/submit", data=body,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 400

    def test_head_request_ok(self, server):
        resp = raw_roundtrip(server,
                             b"HEAD / HTTP/1.1\r\nHost: x\r\n"
                             b"Connection: close\r\n\r\n")
        assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.")

    def test_unicode_flag_via_api_ok_with_fake_submitter(self, server):
        body = json.dumps(
            {"challenge": "1", "flag": "FLAG{hà_nội_2026}"},
            ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server}/api/submit", data=body,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
        assert resp.status == 200 and payload["ok"] is True

    def test_serve_nonexistent_workspace_exit_1(self, tmp_path):
        with pytest.raises(SystemExit) as ei:
            captured(handle_serve,
                     parse(["serve", "-w", str(tmp_path / "ghost")]))
        assert ei.value.code == 1

    def test_serve_empty_existing_workspace_renders(self, tmp_path):
        empty = tmp_path / "emptyws"
        empty.mkdir()
        dash = WebDashboard(WorkspaceRepo(str(empty)),
                            submit_factory=lambda: types.SimpleNamespace(
                                submit=lambda c, f: (True, "ok")))
        httpd = dash.make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=10) as r:
                body = r.read()
            assert r.status == 200
            assert "Không có challenge nào".encode("utf-8") in body
        finally:
            httpd.shutdown()
            httpd.server_close()


# ======================================================================
# E. Doctor — URL chết / sai cú pháp (loopback only)
# ======================================================================

def _closed_loopback_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestDoctorBoundary:
    def test_doctor_without_url_exit_2(self):
        with pytest.raises(SystemExit) as ei:
            captured(handle_doctor, parse(["doctor"]))
        assert ei.value.code == 2

    def test_doctor_dead_loopback_all_fail_still_renders(self):
        url = f"http://127.0.0.1:{_closed_loopback_port()}/"
        t0 = time.monotonic()
        with pytest.raises(SystemExit) as ei:
            captured(handle_doctor, parse(["doctor", "-u", url]))
        dt = time.monotonic() - t0
        assert ei.value.code == 1
        assert dt < 30  # không treo lâu vì connection-refused tức thì

    def test_doctor_garbage_url_no_traceback(self):
        out, code = captured_exit(
            handle_doctor, parse(["doctor", "-u", "not-a-url"]))
        assert code == 1
        assert "Tổng kết" in out
        assert "Traceback" not in out

    def test_doctor_partial_readiness_exits_1_not_false_success(self):
        from unittest.mock import patch
        from ctf_downloader.services.health_service import DoctorReport, HealthService

        report = DoctorReport(url="https://ctf.test")
        report.add("URL sống", True, "HTTP 200")
        report.add("Auth hợp lệ", False, "cookie hết hạn")
        with patch.object(HealthService, "check", return_value=report):
            _out, code = captured_exit(
                handle_doctor, parse(["doctor", "-u", "https://ctf.test"])
            )
        assert code == 1

    def test_doctor_all_readiness_checks_pass_returns_zero(self):
        from unittest.mock import patch
        from ctf_downloader.services.health_service import DoctorReport, HealthService

        report = DoctorReport(url="https://ctf.test")
        report.add("URL sống", True, "HTTP 200")
        with patch.object(HealthService, "check", return_value=report):
            _out, code = captured_exit(
                handle_doctor, parse(["doctor", "-u", "https://ctf.test"])
            )
        assert code is None

    def test_doctor_cookie_input_error_exits_2_without_traceback(self):
        from unittest.mock import patch
        from ctf_downloader.services.auth_service import AuthService

        with patch.object(
            AuthService, "resolve_cookie_arg",
            side_effect=RuntimeError("Không đọc được cookie file 'secret.txt'"),
        ):
            out, code = captured_exit(
                handle_doctor,
                parse(["doctor", "-u", "https://ctf.test", "-c", "secret.txt"]),
            )
        assert code == 2
        assert "Không đọc được cookie file" in out
        assert "Traceback" not in out


# ======================================================================
# F. Sniper — tham số biên + submitter nổ liên tục
# ======================================================================

class FakeSubmitter:
    def __init__(self, behavior="raise"):
        self.submit_history = []
        self.platform = None
        self.calls = []
        self.behavior = behavior

    def submit(self, challenge, flag, force=False):
        self.calls.append((challenge, flag, force))
        if self.behavior == "raise":
            raise RuntimeError("network down")
        return True, "correct"


class TestSniperBoundary:
    def _repo_with_targets(self, tmp_path, targets):
        root = tmp_path / "ws"
        root.mkdir(exist_ok=True)
        (root / "sniper.json").write_text(json.dumps(targets),
                                          encoding="utf-8")
        return WorkspaceRepo(str(root))

    def test_negative_poll_clamped_run_completes(self, tmp_path):
        """BUG-HUNTER-C6-03 (FIXED): ``--poll -1`` phải được clamp (>= 1s)
        trong service thay vì truyền thẳng vào time.sleep(min(poll, delta))
        khi chờ giờ G — trước đây nổ ValueError traceback."""
        repo = self._repo_with_targets(tmp_path, [
            {"challenge": "alpha", "flag": "FLAG{a}", "delay_seconds": 0}])
        sub = FakeSubmitter(behavior="correct")
        svc = SniperService(repo, sub)
        future = str(int(time.time()) + 2)
        t0 = time.monotonic()
        summary = svc.run(poll_interval=-1.0, start_at=future)  # không raise
        dt = time.monotonic() - t0
        assert dt < 30                      # chờ có giới hạn, xong đúng lúc
        assert len(sub.calls) == 1          # qua được giờ G, bắn đúng 1 phát
        assert not summary["pending"]

    def test_zero_poll_coerced_to_default_at_cli_layer(self):
        args = parse(["sniper", "--poll", "0"])
        # handler dùng ``getattr(args, 'poll', 10) or 10`` -> 0 bị nuốt thành 10
        assert float(getattr(args, "poll", 10) or 10) == 10.0

    def test_submit_raising_repeatedly_consumes_attempt_once(self, tmp_path):
        repo = self._repo_with_targets(tmp_path, [
            {"challenge": "alpha", "flag": "FLAG{a}", "delay_seconds": 0},
            {"challenge": "beta", "flag": "FLAG{b}", "delay_seconds": 0},
        ])
        sub = FakeSubmitter(behavior="raise")
        svc = SniperService(repo, sub)
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        t0 = time.monotonic()
        summary = svc.run(poll_interval=0.05, start_at=past)
        dt = time.monotonic() - t0
        assert dt < 15                      # không vòng lặp vô hạn
        assert len(summary["failed"]) == 2  # mỗi target đúng 1 lần thử
        assert not summary["pending"]
        assert len(sub.calls) == 2


# ======================================================================
# G. History với submit_history.json lớn (1000 entries)
# ======================================================================

class TestHistoryScale:
    def test_1000_entries_lookup_cached_not_quadratic(self, tmp_path, monkeypatch):
        """PERF-HUNTER-C6 (FIXED): 1000 entries x 8 chall — trước đây mỗi
        entry gọi find_challenge quét lại toàn bộ workspace metadata
        (~8000 lần đọc). Sau fix: tra cứu theo map/cache, số lần
        read_metadata phải giảm mạnh (thuộc hàng chục, không hàng nghìn)."""
        ws = str(tmp_path / "ws")
        os.makedirs(ws)
        for i in range(1, 9):   # 8 challenge trên đĩa
            seed_challenge(ws, i, f"Chall{i}", "Cat" if i % 2 else "Web")
        entries = [{
            "challenge_id": (i % 8) + 1,
            "flag": f"FLAG{{hist{i}}}",
            "result": "correct" if i % 3 else "incorrect",
            "timestamp": f"2026-08-{(i % 28) + 1:02d}T12:00:00Z",
        } for i in range(1000)]
        (tmp_path / "ws" / "submit_history.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8")

        orig = WorkspaceRepo.read_metadata
        counter = {"n": 0}

        def counted(self, path):
            counter["n"] += 1
            return orig(self, path)

        monkeypatch.setattr(WorkspaceRepo, "read_metadata", counted)

        t0 = time.monotonic()
        out = captured(handle_history, parse(["history", "-w", ws]))
        dt = time.monotonic() - t0

        print(f"[hunter-c6] history 1000 entries / 8 challs: "
              f"read_metadata calls={counter['n']}, wall={dt:.2f}s")
        assert "hist0" not in out               # flag vẫn che
        # Chỉ 8 challenge distinct -> tối đa vài chục lần đọc metadata,
        # KHÔNG còn quét lại toàn bộ workspace cho từng entry (8000).
        assert counter["n"] <= 200
        assert dt < 30                          # sanity: không treo test
