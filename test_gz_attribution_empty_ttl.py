"""Regression for successful-but-empty GZCTF attribution cache."""

from ctf_downloader.platforms.gzctf import GZCTFPlatform


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/api/game/42/scoreboard"):
            return Response(200, {
                "items": [{
                    "id": 11,
                    "name": "teamX",
                    "solvedChallenges": [],
                }]
            })
        if url.endswith("/api/team/11"):
            return Response(200, {
                "members": [{"userName": "me"}],
            })
        raise AssertionError(f"unexpected GET {url}")


def test_successful_empty_attribution_is_cached_for_ttl():
    session = Session()
    platform = GZCTFPlatform(
        "https://gz.test/games/42/challenges", session
    )
    platform.ctf_info.user_name = "me"
    platform.ctf_info.team_name = "teamX"

    first = platform.fetch_solve_attribution([5])
    assert first == {}
    assert platform._solve_attr_cache == {}
    assert platform._solve_attr_ts is not None
    calls_after_first = list(session.calls)
    assert len(calls_after_first) == 2

    # A clean empty result is still a successful snapshot. Before the missing
    # return-net-clean fix, empty cache + None return meant no timestamp was
    # committed and every caller immediately hit scoreboard/team again.
    second = platform.fetch_solve_attribution([5])
    assert second == {}
    assert session.calls == calls_after_first
