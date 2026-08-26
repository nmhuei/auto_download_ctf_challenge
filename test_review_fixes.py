"""Review round (R1-R6) — test đỏ viết TRƯỚC khi sửa.

R1: watch notices 429 KHÔNG Retry-After → backoff phải SỐNG qua reward
    (không poll lại ~15s ngay tick sau dù message báo "lùi 30s").
R2: 429 CÓ Retry-After → penalty ONE-SHOT, interval cơ sở bất biến.
R3: AuthService tra key URL CHÍNH XÁC trước mọi heuristic; không mượn
    cookie của entry khác platform (host phải khớp).
R4: build_pack idempotent giữa các lần chạy cùng ngày; hậu tố _2 chỉ áp
    cho collision giữa các entry CÙNG lần chạy.
R5: hết raw ``requests.Session()`` ngoài utils/http_client.py.
R6: auto-sync enabled 2 tầng — global = mặc định, workspace
    .ctf/config.json = override; watch đọc được; help text nói đúng.

Chạy: python3 -m pytest test_review_fixes.py -q
"""
import json
import shutil
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------- #
# R1 + R2 — watch notices 429: backoff sống sót, base interval bất biến
# ---------------------------------------------------------------------- #
def _resp(status, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json = MagicMock(return_value={"success": True, "data": []})
    return r


def _mk_watch(tmp_path, resp):
    """WatchService với platform giả kiểu CTFd trả ``resp`` cho /notices."""
    from ctf_downloader.services.watch_service import (
        WatchService, default_auto_sync_config)

    platform = MagicMock()
    platform.ctf_info.platform_type = "ctfd"
    platform.base_url = "https://ctfd.example.com"
    platform.session.get = MagicMock(return_value=resp)

    svc = WatchService(str(tmp_path), once=True, use_live_ui=False)
    svc.platform = platform
    svc.state = svc.state_store.load()
    svc.scheduler.register("notices", 15)
    return svc, default_auto_sync_config()["auto_sync"]


class TestR1NoticesBackoffSurvivesReward:
    def test_429_without_header_backoff_survives_reward(self, tmp_path):
        svc, cfg = _mk_watch(tmp_path, _resp(429, {}))
        before = time.time()
        lines = svc._run_round(cfg)
        delta = svc.scheduler._tasks["notices"]["deadline"] - before
        assert any("429" in ln for ln in lines)
        # Message không bịa "theo Retry-After" khi header vắng mặt
        assert not any("Retry-After" in ln for ln in lines)
        # Backoff ×2 (15→30s, jitter ±20%) phải SỐNG qua reward của tick —
        # reward reset mult KHÔNG được đưa kỳ poll về ~15s.
        assert delta >= 30 * 0.8, (
            f"429 không header bị reward xoá sạch — poll lại sau {delta:.1f}s")

    def test_429_streak_escalates_across_cycles(self, tmp_path):
        svc, cfg = _mk_watch(tmp_path, _resp(429, {}))
        svc._run_round(cfg)
        svc.scheduler._tasks["notices"]["deadline"] = 0.0
        before = time.time()
        svc._run_round(cfg)
        delta = svc.scheduler._tasks["notices"]["deadline"] - before
        assert delta >= 60 * 0.8, (
            f"429 liên tiếp phải backoff ×4 (≈60s), thực tế {delta:.1f}s")


class TestR2RetryAfterOneShot:
    def test_penalty_one_shot_base_interval_immutable(self, tmp_path):
        svc, cfg = _mk_watch(tmp_path, _resp(429, {"Retry-After": "90"}))
        before = time.time()
        svc._run_round(cfg)
        t = svc.scheduler._tasks["notices"]
        # R2: interval CƠ SỞ bất biến — Retry-After là penalty tạm thời,
        # KHÔNG được set_interval đổi base vĩnh viễn.
        assert t["interval"] == 15, (
            f"set_interval đổi base vĩnh viễn ({t['interval']}s)")
        delta = t["deadline"] - before
        assert 90 * 0.8 <= delta <= 90 * 1.2 + 0.5, (
            f"kỳ này phải lùi ~90s theo Retry-After, thực tế {delta:.1f}s")
        # Kỳ kế tiếp quay về base (penalty one-shot đã tiêu)
        before2 = time.time()
        svc.scheduler.postpone("notices")
        delta2 = svc.scheduler._tasks["notices"]["deadline"] - before2
        assert delta2 <= 15 * 1.2 + 0.5

    def test_scheduler_penalty_unit_survives_reward_consumed_once(self):
        from ctf_downloader.services.watch_service import PollScheduler
        s = PollScheduler(jitter=0.0, rng=lambda lo, hi: (lo + hi) / 2)
        s.register("notices", 15)
        s.set_penalty("notices", 90)
        s.reward("notices")            # reward KHÔNG được xoá penalty
        now = time.monotonic()
        dl = s.postpone("notices", now=now)
        assert dl - now == pytest.approx(90, abs=0.5)
        now2 = time.monotonic()        # penalty đã tiêu → về base
        dl2 = s.postpone("notices", now=now2)
        assert dl2 - now2 == pytest.approx(15, abs=0.5)


# ---------------------------------------------------------------------- #
# R3 — AuthService: exact URL-key trước heuristic, không leak chéo host
# ---------------------------------------------------------------------- #
class TestR3AuthUrlKeyExact:
    @staticmethod
    def _patch_cfg(monkeypatch, auth):
        from ctf_downloader.services import auth_service
        monkeypatch.setattr(auth_service, "load_global_config",
                            lambda: {"auth": auth})
        return auth_service.AuthService

    def test_exact_url_key_wins_over_heuristic(self, monkeypatch):
        AS = self._patch_cfg(monkeypatch, {
            "https://ctfA.com": {"cookie": "A_COOKIE"},
            "https://ctfB.com": {"cookie": "B_COOKIE"},
        })
        cookie, token = AS.resolve("https://ctfB.com/")
        assert cookie == "B_COOKIE", (
            "≥2 entry URL mà key đúng tồn tại vẫn trả None")

    def test_no_cross_platform_cookie_leak(self, monkeypatch):
        AS = self._patch_cfg(monkeypatch, {
            "https://ctfA.com": {"cookie": "A_COOKIE"},
        })
        cookie, _token = AS.resolve("https://unrelated-B.net")
        assert cookie is None, (
            "workspace-URL khác platform không được mượn cookie entry duy nhất")

    def test_unique_entry_same_host_variant_key_still_resolves(
            self, monkeypatch):
        AS = self._patch_cfg(monkeypatch, {
            "https://ctfA.com/": {"cookie": "A_COOKIE"},
        })
        cookie, _token = AS.resolve("https://ctfA.com")
        assert cookie == "A_COOKIE"

    def test_dir_workspace_exact_lookup_among_many_entries(
            self, tmp_path, monkeypatch):
        ws = tmp_path / "wsX"
        ws.mkdir()
        (ws / "challenges.json").write_text(json.dumps(
            {"ctf_info": {"url": "https://ctfB.com"}}), encoding="utf-8")
        AS = self._patch_cfg(monkeypatch, {
            "https://ctfA.com": {"cookie": "A_COOKIE"},
            "https://ctfB.com": {"cookie": "B_COOKIE"},
        })
        cookie, _token = AS.resolve(str(ws))
        assert cookie == "B_COOKIE"


# ---------------------------------------------------------------------- #
# R4 — build_pack idempotent giữa các lần chạy; suffix _2 chỉ cùng-lần-chạy
# ---------------------------------------------------------------------- #
def _mk_ws(root: Path, writeup_text: str, with_solver: bool = True) -> None:
    """Workspace 2 bài Web 'Pwn Me' và 'Pwn_Me' — sanitize trùng
    dirname ``Web_Pwn_Me`` (collision thật trong cùng một lần chạy)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "challenges.json").write_text(json.dumps(
        {"ctf_info": {"title": "WSX", "url": "https://x.example"}}),
        encoding="utf-8")

    for name in ("Pwn Me", "Pwn_Me"):
        d = root / "Web" / name
        if d.exists():
            shutil.rmtree(d)      # dựng lại từ đầu — state phản ánh đúng tham số
        (d / "writeup").mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({
            "id": abs(hash(name)) % 10000,
            "name": name,
            "category": "Web",
            "points": 100,
            "status": {
                "schema_version": 2,
                "solve": "solved_by_me",
                "flag": {"value": None, "state": "none"},
                "writeup": "complete",
                "writeup_auto": True,
            },
        }, ensure_ascii=False), encoding="utf-8")
        (d / "writeup" / "README.md").write_text(writeup_text,
                                                 encoding="utf-8")
        if with_solver:
            solver = d / "solver"
            solver.mkdir(exist_ok=True)
            (solver / "solve.py").write_text("print('pwned')\n",
                                             encoding="utf-8")


class TestR4PackIdempotent:
    def test_rerun_same_day_no_stale_duplicates(self, tmp_path):
        from ctf_downloader.services.writeup_exporter import WriteupExporter

        ws = tmp_path / "WSX"
        out = tmp_path / "out"
        _mk_ws(ws, "# v1 PTIT{aaaa_bbbb}", with_solver=True)

        pack1 = WriteupExporter(ws).build_pack(out_dir=out)
        subs1 = sorted(p.name for p in pack1.iterdir() if p.is_dir())
        # Collision CÙNG lần chạy vẫn được hậu tố để không ghi đè nhau
        assert subs1 == ["Web_Pwn_Me", "Web_Pwn_Me_2"]
        assert len(list(pack1.glob("*/solver"))) == 2

        # Re-run cùng ngày: nội dung mới + bỏ solver khỏi workspace
        _mk_ws(ws, "# v2 PATCHED-RUN2 PTIT{cccc_dddd}", with_solver=False)

        pack2 = WriteupExporter(ws).build_pack(out_dir=out)
        subs2 = sorted(p.name for p in pack2.iterdir() if p.is_dir())
        assert subs2 == subs1, (
            f"re-run sinh subdir mới do dir lần-trước: {subs1} → {subs2}")
        # README trong pack là bản MỚI, không còn stale
        for sub in subs2:
            readme = (pack2 / sub / "README.md").read_text(encoding="utf-8")
            assert "PATCHED-RUN2" in readme
        # Solver cũ bị dọn sạch (overwrite idempotent)
        assert list(pack2.glob("*/solver")) == []

        # Zip không chứa subdir trùng lặp cũ+mới
        with zipfile.ZipFile(str(pack2) + ".zip") as z:
            readmes = [n for n in z.namelist()
                       if n.count("/") == 2 and n.endswith("/README.md")]
            dirs = sorted(n.split("/")[1] for n in readmes)
        assert dirs == subs2, f"zip chứa subdir stale/lặp: {dirs}"


# ---------------------------------------------------------------------- #
# R5 — session đi qua create_session, hết raw requests.Session()
# ---------------------------------------------------------------------- #
class TestR5SessionFactoryEverywhere:
    def test_no_raw_session_in_resolver_and_tempmail(self):
        import ctf_downloader
        base = Path(ctf_downloader.__file__).parent
        for rel in ("platforms/ctftime_resolver.py", "utils/tempmail.py"):
            src = (base / rel).read_text(encoding="utf-8")
            assert "requests.Session()" not in src, (
                f"{rel} còn tự tạo requests.Session() thay vì create_session")

    @staticmethod
    def _retry_total(sess, url):
        mr = sess.get_adapter(url).max_retries
        return getattr(mr, "total", 0)

    def test_tempmail_session_comes_from_factory(self):
        from ctf_downloader.utils.tempmail import TempMailClient
        sess = TempMailClient().session
        assert self._retry_total(sess, "https://api.mail.tm") == 3

    def test_ctftime_session_comes_from_factory(self):
        from ctf_downloader.platforms.ctftime_resolver import CTFtimeResolver
        sess = CTFtimeResolver().session
        assert self._retry_total(sess, "https://ctftime.org") == 3
        # UA bắt buộc của CTFtime vẫn được giữ sau khi chuyển factory
        assert sess.headers["User-Agent"].startswith("ctf-downloader/")


# ---------------------------------------------------------------------- #
# R6 — auto-sync enabled: global mặc định, workspace override
# ---------------------------------------------------------------------- #
class TestR6AutoSyncPrecedence:
    @staticmethod
    def _gc(monkeypatch, enabled):
        from ctf_downloader.storage import global_config as gc
        monkeypatch.setattr(gc, "load_global_config",
                            lambda: {"auto_sync": {"enabled": enabled}})

    def test_pure_resolution_two_tiers(self):
        from ctf_downloader.services.watch_service import (
            resolve_auto_sync_enabled)
        # Không workspace config → global là mặc định
        assert resolve_auto_sync_enabled(
            None, {"auto_sync": {"enabled": False}}) is False
        assert resolve_auto_sync_enabled(None, {}) is True
        assert resolve_auto_sync_enabled(None, None) is True
        # Có cả hai → workspace thắng
        assert resolve_auto_sync_enabled(
            {"auto_sync": {"enabled": True}},
            {"auto_sync": {"enabled": False}}) is True
        assert resolve_auto_sync_enabled(
            {"auto_sync": {"enabled": False}},
            {"auto_sync": {"enabled": True}}) is False
        # Workspace có config nhưng thiếu key → rơi xuống global
        assert resolve_auto_sync_enabled(
            {"auto_sync": {}}, {"auto_sync": {"enabled": False}}) is False

    def test_effective_flag_method_two_tiers(self, tmp_path, monkeypatch):
        from ctf_downloader.services.watch_service import WatchService
        self._gc(monkeypatch, False)
        svc = WatchService(str(tmp_path), once=True, use_live_ui=False)
        # Tầng 1: global off, chưa có workspace config → tắt
        assert svc._effective_auto_sync_enabled(svc._resolve_cfg()) is False
        # Tầng 2: workspace override ON thắng global OFF
        svc.cfg_store.save({"version": 1, "auto_sync": {"enabled": True}})
        assert svc._effective_auto_sync_enabled(svc._resolve_cfg()) is True
        # Workspace OFF + global ON → vẫn OFF (workspace thắng)
        self._gc(monkeypatch, True)
        svc.cfg_store.save({"version": 1, "auto_sync": {"enabled": False}})
        assert svc._effective_auto_sync_enabled(svc._resolve_cfg()) is False

    def test_run_gate_blocks_when_global_off(self, tmp_path, monkeypatch):
        from ctf_downloader.services.watch_service import WatchService
        self._gc(monkeypatch, False)
        svc = WatchService(str(tmp_path), once=True, use_live_ui=False)
        with patch.object(WatchService, "_setup_platform") as sp:
            rc = svc.run()
        assert rc == 0
        sp.assert_not_called()
        assert not (tmp_path / ".ctf" / "watch_state.json.lock").exists()

    def test_help_text_states_precedence(self):
        from ctf_downloader.cli_commands import _CONFIG_KEYS
        desc = _CONFIG_KEYS["auto-sync"]["desc"].lower()
        assert "workspace" in desc
        assert "mặc định" in desc
        assert "override" in desc or "ghi đè" in desc
