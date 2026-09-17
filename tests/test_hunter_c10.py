"""HUNTER CYCLE 10 — vùng chưa hunt: per-platform API response parsing.

Phạm vi: ctf_downloader/platforms/{ctfd,gzctf,rctf,base}.py + ctftime_resolver.py
- Malformed JSON shapes (list/dict sai kiểu, field thiếu/None/sai kiểu)
- Event-times: epoch giây vs mili-giây, 0/-1/null, ISO có/không timezone, cực đại
- Solve attribution: response rỗng/partial, solver_names markdown injection
- rCTF kind envelope lạ/thiếu; leaderboard pagination edges
- GZCTF captcha types lạ, siteKey rỗng, difficulty float/string
- ctftime_resolver: thiếu fields, event đầu không khớp, trùng tên

Quy ước: test PASS với hành vi đúng = documentation; FAIL = bug thật
(tên case tiền tố c10_NN* = bug được tái hiện). Toàn bộ HTTP mock bằng
FakeSession/FakeResponse — KHÔNG mạng thật. Không đụng production code,
không đụng test_hunter_c9.py (cycle 9 đang chạy).
"""
import contextlib
import io
import json
import unittest
from unittest import mock

from ctf_downloader.utils.logger import Logger
from ctf_downloader.platforms.base import (
    EventTimes, PlatformRegisterUnsupported, SolveAttribution,
    epoch_ms, normalize_epoch_to_utc,
)
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.gzctf import (
    GZCTFPlatform, gzctf_probe_captcha, solve_hash_pow,
)
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.platforms.ctftime_resolver import (
    CTFtimeResolver, MATCH_THRESHOLD,
)


_LOGGER_PATCHERS = []


def setUpModule():
    # Im lặng Logger (rich console) — giữ output test sạch.
    for m in ("info", "success", "warning", "error", "step"):
        p = mock.patch.object(Logger, m, lambda *a, **k: None)
        p.start()
        _LOGGER_PATCHERS.append(p)


def tearDownModule():
    for p in _LOGGER_PATCHERS:
        p.stop()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        if text:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""
        self.headers = headers or {}
        self.url = "https://fake.test/"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Session giả: map (method, url-substring) -> FakeResponse (longest match)."""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.calls = []
        self.cookies = {}
        self.headers = {}

    def _handle(self, method, url, **kw):
        self.calls.append((method, url))
        best, best_len = None, -1
        for m, sub, resp in self.routes:
            if m == method and sub in url and len(sub) > best_len:
                best, best_len = resp, len(sub)
        return best or FakeResponse(404, text="not found")

    def get(self, url, timeout=None, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, timeout=None, **kw):
        return self._handle("POST", url, **kw)


JSON_CT = {"content-type": "application/json"}

# ---------------------------------------------------------------------------
# Fixtures dựng sẵn
# ---------------------------------------------------------------------------

def ctfd_session(challs_data, *, detail_data=None, team_solves=None,
                 solves_is_string=False):
    """CTFd session: list + detail + optional team solves."""
    routes = [
        ("GET", "/api/v1/challenges",
         FakeResponse(200, {"success": True, "data": challs_data},
                      headers=JSON_CT)),
        ("GET", "/api/v1/challenges/",
         FakeResponse(200, {"success": True,
                            "data": detail_data or {"description": "d",
                                                    "files": []}})),
    ]
    if team_solves is not None:
        payload = ("weird-string" if solves_is_string else team_solves)
        routes.append(("GET", "/api/v1/teams/me/solves",
                       FakeResponse(200, {"success": True, "data": payload})))
    return FakeSession(routes)


def gzctf_session(details_challenges, *, scoreboard=None, single=None,
                  game_id=42):
    routes = [
        ("GET", f"/api/game/{game_id}/details",
         FakeResponse(200, {"challenges": details_challenges})),
        ("GET", f"/api/game/{game_id}/scoreboard",
         FakeResponse(200, scoreboard if scoreboard is not None
                      else {"items": []})),
        ("GET", f"/api/game/{game_id}/challenges/",
         FakeResponse(200, single or {"content": "body", "type": "Static"})),
    ]
    return FakeSession(routes)


def gzctf_platform(session, game_id=42):
    return GZCTFPlatform(f"https://gz.test/games/{game_id}/challenges", session)


# ---------------------------------------------------------------------------
# 1. Malformed JSON shapes — challenges parsing
# ---------------------------------------------------------------------------

class TestC10_01_CategoryTitleNullPoisonsList(unittest.TestCase):
    """BUG C10-01 (M): field category/title = null -> .strip() trên None
    văng AttributeError; outer try nuốt -> TOÀN BỘ list challenges mất
    (0 challenge thay vì bỏ qua 1 phần tử hỏng).
    - ctf_downloader/platforms/ctfd.py:267  (category.strip())
    - ctf_downloader/platforms/rctf.py:140  (category.strip())
    - ctf_downloader/platforms/gzctf.py:403 (title.strip())
    Fix đề xuất: `(item.get("category") or "Misc").strip()` /
    `(item.get("title") or f"Challenge_{cid}")`.strip().
    """

    def test_c10_01a_ctfd_category_null_keeps_other_challs(self):
        s = ctfd_session([
            {"id": 1, "name": "Good", "category": "Web"},
            {"id": 2, "name": "Bad", "category": None},   # null
        ])
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(len(out), 2, "1 chall category=null xoá sạch list")
        self.assertEqual(out[0].category, "Web")
        self.assertEqual(out[1].category, "Misc")

    def test_c10_01b_rctf_category_null_keeps_other_challs(self):
        s = FakeSession([
            ("GET", "/api/v1/challs",
             FakeResponse(200, {"kind": "goodChallenges", "data": [
                 {"id": "c1", "name": "Good", "category": "web"},
                 {"id": "c2", "name": "Bad", "category": None},
             ]})),
        ])
        out = RCTFPlatform("https://r.test", s).fetch_challenges()
        self.assertEqual(len(out), 2, "1 chall category=null xoá sạch list")
        self.assertEqual(out[1].category, "Misc")

    def test_c10_01c_gzctf_title_null_keeps_other_challs(self):
        s = gzctf_session({"Web": [
            {"id": 7, "title": "Ok", "score": 100, "type": "Static"},
            {"id": 8, "title": None, "score": 100, "type": "Static"},
        ]})
        out = gzctf_platform(s).fetch_challenges()
        self.assertEqual(len(out), 2, "1 chall title=null xoá sạch list")


class TestMalformedShapesDegrade(unittest.TestCase):
    """Các shape sai khác phải degrade đẹp (không raise, không mất toàn bộ)."""

    def test_ctfd_data_is_dict_not_list(self):
        s = ctfd_session.__wrapped__ if False else FakeSession([
            ("GET", "/api/v1/challenges",
             FakeResponse(200, {"success": True, "data": {"1": {}}},
                          headers=JSON_CT)),
        ])
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(out, [])  # degrade: [] + log lỗi, không raise

    def test_rctf_data_is_dict_not_list(self):
        s = FakeSession([
            ("GET", "/api/v1/challs",
             FakeResponse(200, {"kind": "goodChallenges",
                                "data": {"oops": 1}})),
        ])
        out = RCTFPlatform("https://r.test", s).fetch_challenges()
        self.assertEqual(out, [])

    def test_gzctf_challenges_values_not_lists(self):
        # categories dict nhưng value là string -> sum(len)/iter degrade về []
        s = gzctf_session({"Web": "not-a-list"})
        out = gzctf_platform(s).fetch_challenges()
        self.assertEqual(out, [])

    def test_ctfd_challenge_missing_id_still_imported(self):
        s = ctfd_session([{"name": "NoID", "category": "Pwn"}])
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].id)   # không crash, id None (documented)

    def test_rctf_and_gzctf_missing_id_no_crash(self):
        s = FakeSession([("GET", "/api/v1/challs",
                          FakeResponse(200, {"kind": "goodChallenges",
                                             "data": [{"name": "N"}]}))])
        out = RCTFPlatform("https://r.test", s).fetch_challenges()
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].id)

        g = gzctf_session({"Web": [{"title": "NoID", "score": 1}]})
        outg = gzctf_platform(g).fetch_challenges()
        self.assertEqual(len(outg), 1)
        self.assertIsNone(outg[0].id)

    def test_points_none_and_score_object_passthrough(self):
        s = ctfd_session([{"id": 1, "name": "A", "category": "Web",
                           "value": None}])
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].points)  # parser không crash; None đi thẳng

        g = gzctf_session({"Web": [{"id": 3, "title": "B",
                                    "score": {"bad": 1}}]})
        outg = gzctf_platform(g).fetch_challenges()
        self.assertEqual(len(outg), 1)
        self.assertEqual(outg[0].points, {"bad": 1})

    def test_ctfd_tags_and_hints_are_strings(self):
        # tags/hints là string -> iterate theo KÝ TỰ (garbage) nhưng không crash
        s = ctfd_session([{"id": 1, "name": "A", "category": "Web",
                           "tags": "web"}],
                         detail_data={"description": "", "files": [],
                                      "hints": "free hint"})
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(len(out), 1)          # không crash
        self.assertEqual(out[0].tags, ["w", "e", "b"])  # garbage documented
        self.assertTrue(all(h.get("content") for h in out[0].hints))

    def test_ctfd_team_solves_payload_is_string(self):
        # solves data là string -> inner try nuốt, list chính vẫn tải được
        s = ctfd_session([{"id": 1, "name": "A", "category": "Web"}],
                         team_solves=[{"challenge_id": 1}],
                         solves_is_string=True)
        out = CTFdPlatform("https://ctf.test", s).fetch_challenges()
        self.assertEqual(len(out), 1)          # degrade đẹp, không mất list


# ---------------------------------------------------------------------------
# 2. Event times — normalize_epoch_to_utc + end-to-end 3 platform
# ---------------------------------------------------------------------------

class TestNormalizeEpoch(unittest.TestCase):
    def test_seconds_vs_ms_heuristic(self):
        sec = 1756160000                     # 2025-08-26 epoch GIÂY
        ms = 1756160000000                   # cùng mốc, MILI-GIÂY (GZCTF/rCTF)
        self.assertEqual(normalize_epoch_to_utc(sec).timestamp(), sec)
        self.assertEqual(normalize_epoch_to_utc(ms).timestamp(), sec)
        self.assertEqual(normalize_epoch_to_utc(str(ms)).timestamp(), sec)
        self.assertEqual(normalize_epoch_to_utc(str(sec)).timestamp(), sec)

    def test_zero_negative_null_bool(self):
        for v in (0, -1, -1756160000000, None, "null", "NULL", "", True, False):
            self.assertIsNone(normalize_epoch_to_utc(v), repr(v))

    def test_iso_strings(self):
        self.assertEqual(
            normalize_epoch_to_utc("2025-08-26T00:00:00Z").timestamp(),
            1756166400)
        self.assertEqual(
            normalize_epoch_to_utc("2025-08-26T02:00:00+02:00").timestamp(),
            1756166400)
        # naive / space-separator / date-only -> giả định UTC
        self.assertEqual(
            normalize_epoch_to_utc("2025-08-26 00:00:00").timestamp(),
            1756166400)
        self.assertEqual(
            normalize_epoch_to_utc("2025-08-26").timestamp(), 1756166400)

    def test_extreme_future(self):
        dt = normalize_epoch_to_utc(253402300799000)   # ms -> năm 9999
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 9999)
        self.assertIsNone(normalize_epoch_to_utc(int(1e18)))  # overflow -> None
        self.assertIsNone(normalize_epoch_to_utc(float("nan")))

    def test_boundary_1e11_era_ambiguous(self):
        # 1e11 vừa = giây (5138 AD) vừa = ms (1973) -> heuristic chọn ms,
        # năm < 2000 -> None (không bao giờ tự đặt lịch sai thế kỷ).
        self.assertIsNone(normalize_epoch_to_utc(int(1e11)))

    def test_epoch_ms_quirks(self):
        self.assertEqual(epoch_ms(1756160000), 1756160000000)
        self.assertIsNone(epoch_ms("garbage"))
        self.assertEqual(epoch_ms(0), 0)      # quirk: 0 -> 0 (callers dùng truthy guard)
        self.assertEqual(epoch_ms(True), 1000)  # quirk documented


class TestPlatformEventTimesEndToEnd(unittest.TestCase):
    def test_gzctf_ms_with_end_negative(self):
        s = FakeSession([("GET", "/api/game/42",
                          FakeResponse(200, {"start": 1756160000000,
                                             "end": -1}))])
        et = gzctf_platform(s).fetch_event_times()
        self.assertIsInstance(et, EventTimes)
        self.assertEqual(et.start_utc.timestamp(), 1756160000)
        self.assertIsNone(et.end_utc)          # -1 = chưa đặt lịch
        self.assertEqual(et.confidence, "high")

    def test_gzctf_all_unset_returns_none(self):
        s = FakeSession([("GET", "/api/game/42",
                          FakeResponse(200, {"start": 0, "end": None}))])
        self.assertIsNone(gzctf_platform(s).fetch_event_times())

    def test_ctfd_window_init_seconds(self):
        html = ('<script>window.init = {"csrfNonce":"abc",'
                '"start":"1756160000","end":null};</script>')
        s = FakeSession([("GET", "/challenges",
                          FakeResponse(200, text=html))])
        et = CTFdPlatform("https://ctf.test", s).fetch_event_times()
        self.assertIsInstance(et, EventTimes)
        self.assertEqual(et.start_utc.timestamp(), 1756160000)
        self.assertIsNone(et.end_utc)
        self.assertEqual(et.confidence, "medium")

    def test_ctfd_negative_string_start_ignored(self):
        html = '<script>window.init = {"start":"-1","end":"-1"};</script>'
        s = FakeSession([("GET", "/challenges",
                          FakeResponse(200, text=html))])
        self.assertIsNone(CTFdPlatform("https://ctf.test", s)
                          .fetch_event_times())

    def test_rctf_config_api_ms(self):
        s = FakeSession([
            ("GET", "/api/v1/integrations/client/config",
             FakeResponse(200, {"data": {"startTime": 1756160000000,
                                         "endTime": None}})),
        ])
        et = RCTFPlatform("https://r.test", s).fetch_event_times()
        self.assertEqual(et.start_utc.timestamp(), 1756160000)
        self.assertIsNone(et.end_utc)
        self.assertEqual(et.source, "rctf:/api/v1/integrations/client/config")

    def test_rctf_meta_tag_fallback_html_escaped(self):
        cfg = '{"startTime": 1756160000000, "endTime": 1756246400000}'
        escaped = cfg.replace('"', "&quot;")
        html = f'<meta name="rctf-config" content="{escaped}">'
        s = FakeSession([
            ("GET", "/api/v1/integrations/client/config",
             FakeResponse(404, text="no")),
            ("GET", "https://r.test", FakeResponse(200, text=html)),
        ])
        et = RCTFPlatform("https://r.test", s).fetch_event_times()
        self.assertEqual(et.start_utc.timestamp(), 1756160000)
        self.assertEqual(et.end_utc.timestamp(), 1756246400)
        self.assertEqual(et.confidence, "medium")


# ---------------------------------------------------------------------------
# 3. Solve attribution
# ---------------------------------------------------------------------------

class TestC10_02_GZCTFAttributionRaises(unittest.TestCase):
    """BUG C10-02 (M): fetch_solve_attribution RAISE trên scoreboard payload
    dị (items chứa phần tử không phải dict / solvedChallenges là string),
    vi phạm hợp đồng base.py:197-203 «KHÔNG BAO GIỜ raise» và docstring
    gzctf.py:732 («mọi exception → trả phần đã có»).
    Nguyên nhân: vòng lặp items ở gzctf.py:688-693 nằm NGOÀI try của request
    (gzctf.py:676-683); fetch_solve_attribution (gzctf.py:730-738) cũng không bọc.
    Đối chứng: consumer cùng payload trong fetch_challenges (gzctf.py:373-396)
    có try riêng nên chỉ mất solved-set — hai đường xử lý lệch chuẩn.
    Fix đề xuất: bọc `self._fetch_all_attribution(cache)` trong try/except
    (hoặc isinstance(item, dict) guard trong vòng lặp).
    """

    def _platform(self, scoreboard):
        s = FakeSession([("GET", "/api/game/42/scoreboard",
                          FakeResponse(200, scoreboard))])
        p = gzctf_platform(s)
        p.ctf_info.user_name = "me"
        p.ctf_info.team_name = "teamX"
        return p

    def test_c10_02a_items_list_of_strings_never_raises(self):
        p = self._platform({"items": ["team-a"]})
        try:
            out = p.fetch_solve_attribution([1, 2])
            self.assertIsInstance(out, dict)   # hợp đồng: trả dict
        except Exception as e:
            self.fail(f"fetch_solve_attribution raise {type(e).__name__}: {e}")

    def test_c10_02b_solvedchallenges_string_never_raises(self):
        p = self._platform({"items": [
            {"id": 11, "name": "teamX", "solvedChallenges": "not-a-list"}]})
        try:
            out = p.fetch_solve_attribution([1])
            self.assertIsInstance(out, dict)
        except Exception as e:
            self.fail(f"fetch_solve_attribution raise {type(e).__name__}: {e}")

    def test_contrast_fetch_challenges_same_shape_survives(self):
        # Đối chứng: cùng shape dị qua fetch_challenges thì sống (try riêng).
        sb = {"items": [{"id": 11, "name": "teamX",
                         "solvedChallenges": "not-a-list"}]}
        s = gzctf_session({"Web": [{"id": 7, "title": "A", "score": 10}]},
                          scoreboard=sb)
        out = gzctf_platform(s).fetch_challenges()
        self.assertEqual(len(out), 1)


class TestAttributionEmptyPartial(unittest.TestCase):
    def test_gzctf_empty_scoreboard_falls_back_details(self):
        routes = [
            ("GET", "/api/game/42/scoreboard", FakeResponse(200, {"items": []})),
            ("GET", "/api/game/42/details",
             FakeResponse(200, {"challenges": {
                 "Web": [{"id": 5, "solvedByMe": True}]}})),
        ]
        p = gzctf_platform(FakeSession(routes))
        p.ctf_info.team_name = "teamX"
        out = p.fetch_solve_attribution([5])
        self.assertTrue(out[5].by_team)     # fallback details hoạt động

    def test_gzctf_membership_unverified_drops_team_match(self):
        # Team trùng tên NHƯNG không xác minh được membership (team API lỗi
        # cả 2 lần) -> bỏ đội, KHÔNG tự nhận solve (fail-safe đúng).
        routes = [
            ("GET", "/api/game/42/scoreboard",
             FakeResponse(200, {"items": [
                 {"id": 11, "name": "teamX",
                  "solvedChallenges": [{"id": 5, "userName": "other"}]}]})),
            ("GET", "/api/team/11", FakeResponse(500, text="err")),
        ]
        p = gzctf_platform(FakeSession(routes))
        p.ctf_info.user_name = "me"
        p.ctf_info.team_name = "teamX"
        out = p.fetch_solve_attribution([5])
        self.assertEqual(out, {})           # fail-safe, không mạo danh

    def test_ctfd_empty_and_partial_responses(self):
        s = FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"success": True,
                                "data": {"id": 99, "name": "me"}})),
            ("GET", "/api/v1/teams/me", FakeResponse(401, text="no")),  # users mode
            ("GET", "/api/v1/users/me/solves",
             FakeResponse(200, {"success": True, "data": [
                 {"challenge_id": 3, "date": 1756160000},        # không có user
                 {"challenge": {"id": 4}, "user": {"id": 99, "name": "me"},
                  "date": 1756160100},
             ]})),
        ])
        out = CTFdPlatform("https://ctf.test", s).fetch_solve_attribution([3, 4, 9])
        self.assertTrue(out[3].by_me)       # users mode: mọi solve là của mình
        self.assertEqual(out[3].solver_names, [])
        self.assertTrue(out[4].by_me)
        self.assertEqual(out[4].solver_names, ["me"])
        self.assertNotIn(9, out)            # partial: id lạ không bị bịa
        self.assertLessEqual(out[4].solved_at, 1756160100000)

    def test_rctf_empty_users_me(self):
        s = FakeSession([("GET", "/api/v1/users/me",
                          FakeResponse(200, {"kind": "goodUserData",
                                             "data": None}))])
        out = RCTFPlatform("https://r.test", s).fetch_solve_attribution([1])
        self.assertEqual(out, {})

    def test_rctf_partial_solves_then_public_names_and_first_blood(self):
        s = FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"kind": "goodUserData", "data": {
                 "name": "me",
                 "solves": [{"chalId": 3, "createdAt": 1756160000000}]}})),
            ("GET", "/api/v1/challs/3/solves",
             FakeResponse(200, {"kind": "goodSolves", "data": [
                 {"user": {"name": "me"}, "ts": 1756160000000},
                 {"user": {"name": "friend"}, "ts": 1756160500000},
             ]})),
        ])
        out = RCTFPlatform("https://r.test", s).fetch_solve_attribution([3])
        attr = out[3]
        self.assertTrue(attr.by_me and attr.by_team)
        self.assertEqual(attr.solver_names, ["me", "friend"])
        self.assertTrue(attr.first_blood)   # sớm nhất == solve của mình
        self.assertEqual(attr.solved_at, 1756160000000)

    def test_rctf_challenge_id_null_chain_skips_row(self):
        # Quirk (L): chalId=null -> rơi vào challengeId; nếu key tồn tại nhưng
        # null thì dict.get trả None (default không dùng) -> bỏ qua dù `id` có.
        s = FakeSession([("GET", "/api/v1/users/me",
                          FakeResponse(200, {"kind": "goodUserData", "data": {
                              "name": "me",
                              "solves": [{"challengeId": None, "id": 7}]}}))])
        out = RCTFPlatform("https://r.test", s).fetch_solve_attribution([7])
        self.assertNotIn(7, out)            # documented quirk, không crash


class TestSolverNamesMarkdownInjection(unittest.TestCase):
    """solver_names từ server chảy thẳng vào bảng drift (cli_commands.py:831,
    pull_service.py:892) qua Logger.print_table (logger.py:49-55) KHÔNG escape
    rich markup — họ hàng C6-01."""

    def test_platform_layer_preserves_raw_markdown(self):
        s = FakeSession([
            ("GET", "/api/v1/users/me",
             FakeResponse(200, {"kind": "goodUserData", "data": {
                 "name": "victim",
                 "solves": [{"chalId": 3}]}})),   # phải có solve trong cache
            ("GET", "/api/v1/challs/3/solves",
             FakeResponse(200, {"kind": "goodSolves", "data": [
                 {"name": "[click](https://evil) pwn"}]})),
        ])
        out = RCTFPlatform("https://r.test", s).fetch_solve_attribution([3])
        # Platform layer lưu raw — trách nhiệm escape thuộc tầng hiển thị.
        self.assertEqual(out[3].solver_names, ["[click](https://evil) pwn"])

    def test_print_table_interprets_markup_in_cells(self):
        # ĐÃ VÁ (fix hunter cycle-10 tại logger.print_table): cell dữ liệu
        # server được escape → tag style HỢP LỆ KHÔNG còn bị tiêu hoá mà hiện
        # NGUYÊN VĂN như text thường (không injection/bleed, không đổi style).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            Logger.print_table("T", ["Solvers"], [["[dim]ghost[/]"]])
        rendered = buf.getvalue()
        self.assertIn("[dim]ghost[/]", rendered)  # nguyên văn — markup đã escape
        # Tag không hợp lệ cũng KHÔNG crash — text hiển thị đầy đủ.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            Logger.print_table("T", ["Solvers"], [["[[not-a-tag]]pwn"]])
        self.assertIn("[[not-a-tag]]pwn", buf2.getvalue())


# ---------------------------------------------------------------------------
# 4. rCTF kind envelope + leaderboard edges
# ---------------------------------------------------------------------------

class TestC10_03_RCTFUnknownKindMarkedCorrect(unittest.TestCase):
    """BUG C10-03 (M): submit_flag rCTF nhánh else (rctf.py:224-227) coi MỌI
    kind lạ kèm HTTP 200 là ĐÚNG (last_verdict='correct', return True) — kể cả
    kind='bad*' chưa biết hay body {} không có kind. Verdict sai làm INDEX/
    trạng thái đánh dấu solved oan.
    Fix đề xuất: chỉ chấp nhận kind.startswith('good') (hoặc whitelist
    goodFlag/goodSubmission) khi HTTP 200; còn lại -> verdict 'unknown'.
    """

    def _submit(self, body, status=200):
        s = FakeSession([("POST", "/api/v1/challs/c1/submit",
                          FakeResponse(status, body))])
        return RCTFPlatform("https://r.test", s).submit_flag("c1", "flag{x}")

    def test_c10_03a_unknown_bad_kind_http200_must_not_be_correct(self):
        correct, msg = self._submit({"kind": "badTotallyUnknown", "message": "?"})
        self.assertFalse(correct, f"kind lạ + HTTP200 bị tính ĐÚNG: {msg}")
        self.assertNotEqual(RCTFPlatform.last_verdict.fget, "correct")

    def test_c10_03b_missing_kind_http200_must_not_be_correct(self):
        correct, msg = self._submit({})
        self.assertFalse(correct, f"thiếu kind + HTTP200 bị tính ĐÚNG: {msg}")


class TestRCTFKnownKindsAndLeaderboard(unittest.TestCase):
    def _submit(self, body, status=200):
        s = FakeSession([("POST", "/api/v1/challs/c1/submit",
                          FakeResponse(status, body))])
        return RCTFPlatform("https://r.test", s).submit_flag("c1", "flag{x}")

    def test_known_kind_mapping(self):
        self.assertEqual(self._submit({"kind": "goodFlag"})[0], True)
        self.assertEqual(self._submit({"kind": "alreadySolved"})[0], True)
        self.assertEqual(self._submit({"kind": "badFlag"})[0], False)
        self.assertEqual(self._submit({"kind": "badRateLimit"})[0], False)
        rc, _ = self._submit({"kind": "badChallenge"}, status=404)
        self.assertFalse(rc)
        # kind lạ + HTTP 4xx -> False (đúng), chỉ nhánh HTTP200 mới nguy hiểm
        rc, _ = self._submit({"kind": "weird"}, status=400)
        self.assertFalse(rc)

    def test_leaderboard_short_page_stops(self):
        rows = [{"name": f"t{i}", "score": i} for i in range(30)]
        s = FakeSession([("GET", "/api/v1/leaderboard/now",
                          FakeResponse(200, {"kind": "goodLeaderboard",
                                             "data": {"leaderboard": rows,
                                                      "total": 30}}))])
        res = RCTFPlatform("https://r.test", s).fetch_scoreboard()
        self.assertEqual(res["total_teams"], 30)
        self.assertEqual(len(res["standings"]), 30)

    def test_leaderboard_pagination_cap_ten_pages(self):
        # Server luôn trả đủ 100 dòng/page, không khai báo total -> chặn 10 trang.
        page = [{"name": f"t{i}", "score": 1} for i in range(100)]
        offsets_seen = []

        def handler(url, **kw):
            offsets_seen.append(kw.get("params", {}).get("offset"))
            return FakeResponse(200, {"kind": "goodLeaderboard",
                                      "data": {"leaderboard": page}})
        sess = FakeSession()
        sess.get = handler
        res = RCTFPlatform("https://r.test", sess).fetch_scoreboard()
        self.assertEqual(res["total_teams"], 1000)
        self.assertEqual(offsets_seen, [0, 100, 200, 300, 400, 500,
                                        600, 700, 800, 900])

    def test_leaderboard_bad_entry_mid_page_degrades(self):
        rows = [{"name": "ok", "score": 1}, "not-a-dict"]
        s = FakeSession([("GET", "/api/v1/leaderboard/now",
                          FakeResponse(200, {"kind": "goodLeaderboard",
                                             "data": {"leaderboard": rows}}))])
        res = RCTFPlatform("https://r.test", s).fetch_scoreboard()
        self.assertIsInstance(res["standings"], list)   # không raise (mất cả trang)


# ---------------------------------------------------------------------------
# 5. GZCTF captcha
# ---------------------------------------------------------------------------

def probe_platform(config, captcha, powchal=None):
    routes = [("GET", "/api/config", FakeResponse(200, config)),
              ("GET", "/api/captcha", FakeResponse(200, captcha))]
    if powchal is not None:
        routes.append(("GET", "/api/captcha/PowChallenge",
                       FakeResponse(200, powchal)))

    class P:
        origin = "https://gz.test"
        session = FakeSession(routes)
    return P()


class TestGZCTFCaptchaTypes(unittest.TestCase):
    def test_unknown_type_safe_denies(self):
        with self.assertRaises(PlatformRegisterUnsupported):
            gzctf_probe_captcha(probe_platform({}, {"type": "WeirdAuth",
                                                    "siteKey": ""}))

    def test_hashpow_with_empty_sitekey_returns_task(self):
        task = gzctf_probe_captcha(probe_platform(
            {}, {"type": "HashPow", "siteKey": ""},
            powchal={"id": "aabbccddeeff", "difficulty": 8}))
        self.assertEqual(task["challenge_id"], "aabbccddeeff")
        self.assertEqual(task["difficulty"], 8)

    def test_type_none_but_sitekey_set_denies(self):
        with self.assertRaises(PlatformRegisterUnsupported):
            gzctf_probe_captcha(probe_platform({}, {"type": "None",
                                                    "siteKey": "k"}))

    def test_turnstile_via_sitekey_field_in_config_denies(self):
        with self.assertRaises(PlatformRegisterUnsupported):
            gzctf_probe_captcha(probe_platform(
                {"TurnstileSiteKey": "0xabc"}, {"type": "None",
                                                "siteKey": ""}))

    def test_solve_hash_pow_small_difficulty(self):
        ans = solve_hash_pow("aabbccddeeff", 8, max_iter=1_000_000)
        self.assertIsNotNone(ans)
        self.assertEqual(len(ans), 16)                    # 8 bytes hex
        int(ans, 16)                                      # hợp lệ hex
        self.assertIsNone(solve_hash_pow("aabbccddeeff", 128, max_iter=200))
        self.assertEqual(solve_hash_pow("zz-not-hex", 1, max_iter=1000), "0000000000000000")


class TestC10_04_CaptchaDifficultyNonNumeric(unittest.TestCase):
    """BUG C10-04 (L): PowChallenge.difficulty là string không số
    (`int(chal.get('difficulty') or 0)` tại gzctf.py:174-175) ném ValueError
    THÔ ra ngoài gzctf_probe_captcha thay vì PlatformRegisterUnsupported /
    thông báo sạch — hợp đồng register bị rò rỉ exception loại lạ.
    Fix đề xuất: helper `_as_int(v)` try/except -> 0, hoặc bọc cụm PowChallenge
    trong try/except chuyển thành PlatformRegisterUnsupported."""

    def test_difficulty_alpha_string_does_not_leak_valueerror(self):
        try:
            gzctf_probe_captcha(probe_platform(
                {}, {"type": "HashPow", "siteKey": ""},
                powchal={"id": "aabbccddeeff", "difficulty": "easy"}))
        except PlatformRegisterUnsupported:
            pass                                  # từ chối sạch = OK
        except Exception as e:
            self.fail(f"difficulty 'easy' rò rỉ {type(e).__name__}: {e}")

    def test_difficulty_numeric_forms_accepted(self):
        task = gzctf_probe_captcha(probe_platform(
            {}, {"type": "HashPow", "siteKey": ""},
            powchal={"id": "aabbccddeeff", "difficulty": "4"}))
        self.assertEqual(task["difficulty"], 4)          # numeric string OK

        task2 = gzctf_probe_captcha(probe_platform(
            {}, {"type": "HashPow", "siteKey": ""},
            powchal={"id": "aabbccddeeff", "difficulty": 2.9}))
        self.assertEqual(task2["difficulty"], 2)         # float -> truncate (documented)

        task3 = gzctf_probe_captcha(probe_platform(
            {}, {"type": "HashPow", "siteKey": ""},
            powchal={"id": "aabbccddeeff", "difficulty": None}))
        self.assertEqual(task3["difficulty"], 0)         # thiếu -> 0 (đi tiếp)


# ---------------------------------------------------------------------------
# 6. ctftime_resolver
# ---------------------------------------------------------------------------

def make_resolver(events):
    resp = FakeResponse(200, events)
    sess = FakeSession([("GET", "/events/", resp)])
    return CTFtimeResolver(session=sess)


class TestCTFtimeResolverMatching(unittest.TestCase):
    EV_A = {"id": 1, "title": "Sunshine CTF 2025", "url": "https://sunshinectf.org",
            "start": 1756160000, "finish": 1756246400}
    EV_B = {"id": 2, "title": "Kalmarunionen CTF 2025",
            "url": "https://kalm.nu", "start": 1756160000,
            "finish": 1756246400}

    def test_events_missing_fields_degrades_empty(self):
        r = make_resolver([{}, {"title": None}, "junk", 42])
        times, cands = r.resolve_event_times("Sunshine CTF")
        self.assertIsNone(times)
        self.assertEqual(cands, [])

    def test_clear_winner_single_candidate(self):
        r = make_resolver([self.EV_B, self.EV_A])   # event ĐẦU không khớp
        times, cands = r.resolve_event_times("sunshine ctf finals")
        self.assertIsNotNone(times)
        self.assertEqual(cands[0][1]["id"], 1)       # chọn theo SCORE, không theo thứ tự
        self.assertEqual(times.confidence, "medium")
        self.assertEqual(times.source, "ctftime:1")
        self.assertEqual(times.start_utc.timestamp(), 1756160000)

    def test_two_similar_titles_ambiguous(self):
        e1 = dict(self.EV_A, id=1, title="Alpha CTF 2025")
        e2 = dict(self.EV_A, id=2, title="Alpha CTF Quals 2025")
        r = make_resolver([e1, e2])
        times, cands = r.resolve_event_times("Alpha CTF")
        self.assertIsNone(times)                      # gap < 0.15 -> hỏi user
        self.assertEqual(len(cands), 2)

    def test_identical_titles_many_candidates_capped(self):
        many = [dict(self.EV_A, id=i, title="Same Name CTF") for i in range(8)]
        r = make_resolver(many)
        times, cands = r.resolve_event_times("Same Name")
        self.assertIsNone(times)
        self.assertLessEqual(len(cands), 5)           # MAX_CANDIDATES

    def test_url_domain_absolute_match_beats_title(self):
        odd = {"id": 9, "title": "Completely Different Thing",
               "url": "https://sunshinectf.org",
               "start": 1756160000, "finish": 1756246400}
        r = make_resolver([odd, self.EV_B])
        times, cands = r.resolve_event_times("nothing alike here",
                                             url_hint="https://sunshinectf.org/x")
        self.assertIsNotNone(times)                   # domain match -> 1.0
        self.assertEqual(cands[0][1]["id"], 9)

    def test_winner_without_times_returns_none_times(self):
        ghost = {"id": 3, "title": "Ghost CTF"}       # thiếu start/finish
        r = make_resolver([ghost, self.EV_B])
        times, cands = r.resolve_event_times("Ghost CTF")
        self.assertIsNone(times)                      # không bịa window rỗng
        self.assertEqual(len(cands), 1)

    def test_fetch_window_envelopes(self):
        self.assertEqual(make_resolver([{"a": 1}, "x", 3]).fetch_window()[0],
                         {"a": 1})
        r = make_resolver({"results": [{"b": 2}], "meta": 1})
        self.assertEqual(r.fetch_window(), [{"b": 2}])
        self.assertEqual(make_resolver({"nope": 1}).fetch_window(), [])

    def test_get_event_non_numeric_id_raises(self):
        # Quirk (L): _get_json never-raise nhưng get_event gọi int(id) ngoài
        # bảo vệ -> ValueError ném thẳng. Caller hiện chỉ truyền id số.
        r = make_resolver([])
        with self.assertRaises(ValueError):
            r.get_event("not-a-number")


if __name__ == "__main__":
    unittest.main(verbosity=1)
