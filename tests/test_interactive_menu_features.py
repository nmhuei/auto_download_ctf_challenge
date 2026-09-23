import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import ctf_downloader.interactive_menu as im


class FakeMenuConsole:
    def __init__(self, inputs=None):
        self.inputs = list(inputs or [])
        self.printed = []
        self.width = 100

    def print(self, *args, **kwargs):
        rendered = []
        for arg in args:
            target = arg
            while hasattr(target, "renderable"):
                target = target.renderable
            if hasattr(target, "plain"):
                rendered.append(target.plain)
            elif hasattr(target, "columns"):
                for col in getattr(target, "columns", []):
                    rendered.append(str(col.header))
                    for cell in getattr(col, "_cells", []):
                        rendered.append(getattr(cell, "plain", str(cell)))
            else:
                rendered.append(str(target))
        self.printed.append(" ".join(rendered))

    def input(self, prompt=""):
        if hasattr(prompt, "plain"):
            self.printed.append(prompt.plain)
        else:
            self.printed.append(str(prompt))
        if not self.inputs:
            raise EOFError("No more fake inputs")
        return self.inputs.pop(0)


def create_dummy_workspace(temp_dir: str) -> Path:
    ws = Path(temp_dir) / "CTF_Test"
    ch1 = ws / "Web" / "chall-1"
    (ch1 / "challenge").mkdir(parents=True)
    (ch1 / "script").mkdir()
    (ch1 / "solver").mkdir()
    (ch1 / "challenge" / "index.js").write_text("console.log(1)", encoding="utf-8")
    (ch1 / "README.md").write_text("# Chall 1\nFind the flag", encoding="utf-8")
    (ch1 / "metadata.json").write_text(json.dumps({
        "id": 101, "name": "Web Challenge", "category": "Web", "points": 150,
        "connection_info": "http://example.com",
    }), encoding="utf-8")

    ch2 = ws / "Crypto" / "chall-2"
    (ch2 / "challenge").mkdir(parents=True)
    (ch2 / "challenge" / "enc.py").write_text("print(2)", encoding="utf-8")
    (ch2 / "README.md").write_text("# Chall 2\nMath puzzle", encoding="utf-8")
    (ch2 / "metadata.json").write_text(json.dumps({
        "id": 102, "name": "Crypto Challenge", "category": "Crypto", "points": 200,
        "connection_info": "", "solved_by_me": True,
    }), encoding="utf-8")

    return ws


def test_main_menu_enter_selects_last_action(monkeypatch):
    con = FakeMenuConsole(inputs=["", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._last_action = "4"

        called = []
        monkeypatch.setattr(app, "_menu_solver", lambda: called.append("solver"))
        app.run()

        assert "solver" in called


def test_main_menu_aliases_and_cleaning(monkeypatch):
    con = FakeMenuConsole(inputs=[" [4] ", " q "])
    monkeypatch.setattr(im, "_menu_console", lambda: con)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._last_action = None

        called = []
        monkeypatch.setattr(app, "_menu_solver", lambda: called.append("solver"))
        app.run()

        assert "solver" in called
        assert app._last_action == "4"


def test_menu_view_challenge_detail_lists_and_selects_by_index(monkeypatch):
    # Select challenge 1 by index, then return (choice 0)
    con = FakeMenuConsole(inputs=["1", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_view_challenge_detail()

        output = "\n".join(con.printed)
        assert "Web Challenge" in output
        assert "Crypto Challenge" in output
        assert "Challenge Description (README.md)" in output
        assert "Attachments" in output
        assert "enc.py" in output


def test_menu_submit_flag_lists_and_submits(monkeypatch):
    con = FakeMenuConsole(inputs=["1", "FLAG{test_flag_123}"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    submitted = []

    class MockSubmitter:
        def __init__(self, *args, **kwargs):
            pass

        def submit_single_flag(self, challenge_id, challenge_name, flag_value):
            submitted.append((challenge_id, challenge_name, flag_value))

    monkeypatch.setattr(im, "FlagSubmitter", MockSubmitter)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_submit_flag()

        assert len(submitted) == 1
        assert submitted[0][0] == 102 or submitted[0][0] == 101
        assert submitted[0][2] == "FLAG{test_flag_123}"


def test_menu_solver_renders_and_returns(monkeypatch):
    con = FakeMenuConsole(inputs=["0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_solver()

        output = "\n".join(con.printed)
        assert "SUPERBQA" in output


def test_resolve_challenge_selection_explicit_prefixes():
    challs = [
        {"id": 10, "name": "Web A"},
        {"id": 20, "name": "Crypto B"},
    ]
    # Explicit #id
    target, err = im._resolve_challenge_selection(challs, "#10")
    assert target == challs[0]
    assert err is None

    # Explicit id:
    target, err = im._resolve_challenge_selection(challs, "id: 20")
    assert target == challs[1]
    assert err is None

    # Missing ID
    target, err = im._resolve_challenge_selection(challs, "#999")
    assert target is None
    assert "No challenge found with ID #999" in err


def test_resolve_challenge_selection_numeric_collision():
    challs = [
        {"id": 2, "name": "First Chall with ID 2"},
        {"id": 100, "name": "Second Chall with ID 100"},
    ]
    # Index 2 is "Second Chall", but exact ID 2 is "First Chall" -> Ambiguity detected!
    target, err = im._resolve_challenge_selection(challs, "2")
    assert target is None
    assert "is ambiguous" in err
    assert "#2" in err

    # Explicit #2 resolves to First Chall
    target, err = im._resolve_challenge_selection(challs, "#2")
    assert target == challs[0]
    assert err is None

    # Input 1 resolves to index 1 (First Chall) since ID 1 doesn't exist
    target, err = im._resolve_challenge_selection(challs, "1")
    assert target == challs[0]
    assert err is None


def test_resolve_challenge_selection_name_matching():
    challs = [
        {"id": 1, "name": "Crypto RSA"},
        {"id": 2, "name": "Crypto ECC"},
        {"id": 3, "name": "rsa"},
        {"id": 4, "name": None},
    ]
    # Exact name match takes precedence over substring
    target, err = im._resolve_challenge_selection(challs, "rsa")
    assert target == challs[2]
    assert err is None

    # Substring match with unique candidate
    target, err = im._resolve_challenge_selection(challs, "ecc")
    assert target == challs[1]
    assert err is None

    # Substring match with multiple candidates
    target, err = im._resolve_challenge_selection(challs, "Crypto")
    assert target is None
    assert "Found 2 challenges matching" in err

    # Missing/None name doesn't crash
    target, err = im._resolve_challenge_selection(challs, "something_else")
    assert target is None
    assert "No challenge found" in err


def test_menu_view_challenge_detail_survives_missing_and_corrupt_files(monkeypatch):
    con = FakeMenuConsole(inputs=["1", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    with tempfile.TemporaryDirectory() as temp:
        ws = Path(temp) / "CTF_Corrupt"
        ch = ws / "Pwn" / "broken"
        (ch / "challenge").mkdir(parents=True)
        # Create non-utf8 README.md
        with open(ch / "README.md", "wb") as f:
            f.write(b"\xff\xfe\xfaInvalid\x80\x81Binary")
        (ch / "metadata.json").write_text(json.dumps({
            "id": 999, "name": "Broken Chall", "category": "Pwn",
        }), encoding="utf-8")

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        # Should not crash on invalid utf8 or missing folder
        app._menu_view_challenge_detail()
        output = "\n".join(con.printed)
        assert "Broken Chall" in output


def test_menu_solver_background_spawn_and_banner(monkeypatch):
    from ctf_downloader.services.solver_service import SolverService

    # User inputs: option '1', then the selected display ID.  BQA EATING
    # returns immediately after handing work to the daemon.
    con = FakeMenuConsole(inputs=["1", "1", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)
    monkeypatch.setattr(im, "_prompt", lambda prompt: con.input(prompt))

    spawned = []
    monkeypatch.setattr(
        SolverService,
        "spawn_background",
        lambda self, ids, **kwargs: spawned.append(ids) or {"success": True, "message": f"Daemon started for {ids}"}
    )

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_solver()
        assert len(spawned) == 1
        assert spawned[0] == "1"
        output = "\n".join(con.printed)
        assert "Daemon started for 1" in output
        assert "Attach Live Radar now?" not in output
        assert output.count("[1] BQA EATING") == 2


def test_menu_solver_bqa_eating_labels_and_help_alias(monkeypatch):
    from ctf_downloader.services.solver_service import SolverService

    # 1. Test selecting option 'bqa' alias instead of '1'
    con = FakeMenuConsole(inputs=["bqa", "2"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)
    monkeypatch.setattr(im, "_prompt", lambda prompt: con.input(prompt))

    spawned = []
    monkeypatch.setattr(
        SolverService,
        "spawn_background",
        lambda self, ids, **kwargs: spawned.append(ids) or {"success": True, "message": f"Daemon started for {ids}"}
    )

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_solver()
        assert len(spawned) == 1
        assert spawned[0] == "2"
        output = "\n".join(con.printed)
        assert "[1] BQA EATING" in output
        assert "[2] SUPERBQA EATING" in output
        assert "[7] Help" in output


def test_menu_solver_superbqa_uses_one_worker_per_category(monkeypatch):
    from ctf_downloader.services.solver_service import SolverService

    con = FakeMenuConsole(inputs=["2", "n", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)
    monkeypatch.setattr(im, "_prompt", lambda prompt: con.input(prompt))
    monkeypatch.setattr(im.Confirm, "ask", lambda *args, **kwargs: True)

    calls = []
    monkeypatch.setattr(
        SolverService,
        "spawn_background",
        lambda self, ids, **kwargs: calls.append((ids, kwargs)) or {
            "success": True,
            "message": "SuperBQA daemon started",
        },
    )

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        crypto_meta = ws / "Crypto" / "chall-2" / "metadata.json"
        metadata = json.loads(crypto_meta.read_text(encoding="utf-8"))
        metadata.pop("solved_by_me", None)
        crypto_meta.write_text(json.dumps(metadata), encoding="utf-8")
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_solver()

    assert calls == [("1,2", {"workers": 2, "per_category": True})]


def test_menu_solver_superbqa_queues_only_platform_unsolved_without_local_flag(monkeypatch):
    """Solve all must not relaunch platform-solved or hoarded-flag challenges."""
    from ctf_downloader.services.solver_service import SolverService

    con = FakeMenuConsole(inputs=["2", "n", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)
    monkeypatch.setattr(im, "_prompt", lambda prompt: con.input(prompt))
    monkeypatch.setattr(im.Confirm, "ask", lambda *args, **kwargs: True)

    calls = []
    monkeypatch.setattr(
        SolverService,
        "spawn_background",
        lambda self, ids, **kwargs: calls.append((ids, kwargs)) or {
            "success": True, "message": "SuperBQA daemon started",
        },
    )

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        # Challenge #2 is platform-solved in the shared fixture.
        (ws / "Web" / "chall-1" / "flag.txt").write_text("CTF{already_hoarded}", encoding="utf-8")
        ready = ws / "Pwn" / "ready"
        (ready / "challenge").mkdir(parents=True)
        (ready / "challenge" / "main.py").write_text("print(1)", encoding="utf-8")
        (ready / "metadata.json").write_text(json.dumps({
            "id": 103, "name": "Ready Pwn", "category": "Pwn",
        }), encoding="utf-8")

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._menu_solver()

    assert calls == [("2", {"workers": 1, "per_category": True})]


def test_menu_solver_superbqa_recovers_stale_worker_before_selecting_ready_jobs(monkeypatch):
    """A dead PID is failed first, then its challenge is eligible for Solve all."""
    from ctf_downloader.services.solver_service import SolverService

    con = FakeMenuConsole(inputs=["2", "n", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)
    monkeypatch.setattr(im, "_prompt", lambda prompt: con.input(prompt))
    monkeypatch.setattr(im.Confirm, "ask", lambda *args, **kwargs: True)
    calls = []
    monkeypatch.setattr(
        SolverService,
        "spawn_background",
        lambda self, ids, **kwargs: calls.append((ids, kwargs)) or {
            "success": True, "message": "SuperBQA daemon started",
        },
    )

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        (ws / "Web" / "chall-1" / "script" / "worker-state.json").write_text(
            json.dumps({"state": "running", "pid": 99999999}), encoding="utf-8"
        )
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._menu_solver()

    assert calls == [("2", {"workers": 1, "per_category": True})]


def test_menu_solver_live_radar_opens_directly(monkeypatch):
    # Option 3 is now a direct Live Radar view; it has no active-worker
    # listing or follow-up selection prompt.
    con = FakeMenuConsole(inputs=["3"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    class FakeLive:
        def __init__(self, *args, **kwargs):
            self.updated = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def update(self, *_args, **_kwargs):
            self.updated = True
            raise KeyboardInterrupt

    monkeypatch.setattr(im, "Live", FakeLive, raising=False)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        # Should enter option 3 and detach from the live view on Ctrl+C.
        app._menu_solver()
        output = "\n".join(con.printed)
        assert "[3] Live Radar" in output
        assert "Live Radar" in output
        assert "No active BQA workers running" not in output


def test_menu_solver_active_agy_workers_with_running_job(monkeypatch):
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    con = FakeMenuConsole(inputs=["3"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    class FakeLive:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def update(self, *_args, **_kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(im, "Live", FakeLive, raising=False)

    from ctf_downloader.services.solver_service import SolverService
    monkeypatch.setattr(SolverService, "_pid_is_alive", lambda *args, **kwargs: True)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        # Create a challenge with running worker
        chal = ws / "Crypto" / "hackel"
        (chal / "challenge").mkdir(parents=True)
        (chal / "script").mkdir(parents=True)
        (chal / "metadata.json").write_text(json.dumps({
            "id": 99, "name": "Hackel", "category": "Crypto", "connection_info": "",
        }), encoding="utf-8")
        (chal / "script" / "worker-state.json").write_text(json.dumps({
            "state": "running", "phase": "recon", "pid": 999999,
            "started_at": now_iso, "heartbeat_at": now_iso, "message": "Analyzing challenge",
        }), encoding="utf-8")

        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}

        app._menu_solver()
        output = "\n".join(con.printed)
        assert "[3] Live Radar" in output
        assert "No active BQA workers running" not in output
        assert "Hackel" in output
        assert "recon" in output


def test_menu_configure_auth_sanitization_and_clearing(monkeypatch):
    """Verify _menu_configure_auth sanitizes cookie and calls AuthService.save_auth & delete_auth."""
    from ctf_downloader.services.auth_service import AuthService

    saved_calls = []
    deleted_calls = []

    monkeypatch.setattr(AuthService, "save_auth", lambda ws=None, url=None, cookie=None, token=None, **kw: saved_calls.append((ws, cookie, token)))
    monkeypatch.setattr(AuthService, "delete_auth", lambda ws=None, url=None, **kw: deleted_calls.append(ws))
    monkeypatch.setattr(im, "_pause", lambda: None)

    with tempfile.TemporaryDirectory() as temp:
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = temp
        app.cookie = app.token = None
        app.config = {}

        # 1. Set Cookie with leading/trailing junk and header name
        con = FakeMenuConsole(inputs=["1", "Cookie: session=secret_cookie_val; path=/"])
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        app._menu_configure_auth()

        assert app.cookie == "session=secret_cookie_val; path=/"
        assert len(saved_calls) == 1
        assert saved_calls[0][1] == "session=secret_cookie_val; path=/"

        # 2. Clear credentials (choice 3)
        con = FakeMenuConsole(inputs=["3"])
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        app._menu_configure_auth()

        assert app.cookie is None
        assert app.token is None
        assert len(deleted_calls) == 1
        assert deleted_calls[0] == temp


def test_menu_header_auth_pointer(monkeypatch):
    """Header warning must direct to [1] -> [3] when unauthenticated."""
    with tempfile.TemporaryDirectory() as temp:
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = temp
        app.cookie = app.token = None
        app.config = {}
        app._suppress_next_brand = True

        con = FakeMenuConsole()
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        app._print_header()

        output = "\n".join(con.printed)
        assert "! auth not configured · use [1] -> [3]" in output


def test_menu_solver_empty_workspace_guidance(monkeypatch):
    """When no challenges are downloaded, _menu_solver renders an informative guidance panel."""
    monkeypatch.setattr(im, "_pause", lambda: None)
    with tempfile.TemporaryDirectory() as temp:
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = temp
        app.cookie = app.token = None
        app.config = {}

        # User chooses '0' to exit the guidance panel
        con = FakeMenuConsole(inputs=["0"])
        monkeypatch.setattr(im, "_menu_console", lambda: con)
        app._menu_solver()

        output = "\n".join(con.printed)
        assert "Chưa có bài thi nào" in output
        assert "Hub [1]" in output


def test_main_menu_action_5_invokes_ranking(monkeypatch):
    """Option 5 in main menu invokes _menu_ranking."""
    con = FakeMenuConsole(inputs=["5", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._last_action = None

        ranking_called = []
        monkeypatch.setattr(app, "_menu_ranking", lambda: ranking_called.append(True))
        app.run()

        assert len(ranking_called) == 1
        assert app._last_action == "5"


def test_menu_ranking_displays_and_exits(monkeypatch):
    """_menu_ranking fetches standings and prints menu options, exiting on 0."""
    monkeypatch.setattr(im, "_pause", lambda: None)
    con = FakeMenuConsole(inputs=["0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)

    class DummyRankService:
        def __init__(self, *args, **kwargs):
            self.top_n = None

        def display_and_update(self, top_n=15, update_docs=True):
            self.top_n = top_n
            return {"standings": [{"pos": 1, "team": "PWNers", "score": 1337}]}

    svc_calls = []

    def mock_rank_ctor(*args, **kwargs):
        svc = DummyRankService(*args, **kwargs)
        svc_calls.append(svc)
        return svc

    monkeypatch.setattr("ctf_downloader.services.rank_service.RankService", mock_rank_ctor)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = "dummy_cookie"
        app.token = "dummy_token"
        app.config = {}

        app._menu_ranking()

        assert len(svc_calls) == 1
        assert svc_calls[0].top_n == 15
        output = "\n".join(con.printed)
        assert "LIVE SCOREBOARD & RANKING" in output.upper()


def test_main_menu_action_g_shows_cli_guidance(monkeypatch):
    """Pressing G in main menu displays CLI workflow tips and returns."""
    con = FakeMenuConsole(inputs=["g", "0"])
    monkeypatch.setattr(im, "_menu_console", lambda: con)
    monkeypatch.setattr(im, "_pause", lambda: None)

    with tempfile.TemporaryDirectory() as temp:
        ws = create_dummy_workspace(temp)
        app = im.CTFInteractiveConsole.__new__(im.CTFInteractiveConsole)
        app.workspace_path = str(ws)
        app.cookie = app.token = None
        app.config = {}
        app._last_action = None

        app.run()

        output = "\n".join(con.printed)
        assert "ctf git push" in output
        assert "ctf git sync" in output


def test_update_menu_theme_recolors_rank_console():
    """Theme switch must dynamically recolor the rank service console."""
    from ctf_downloader.services import rank_service as rs
    im._update_menu_theme("dracula")
    try:
        assert str(rs._get_rank_console().get_style("accent")) == "#bd93f9"
        assert str(rs._rank_console.get_style("accent")) == "#bd93f9"
    finally:
        im._update_menu_theme("exodia")



